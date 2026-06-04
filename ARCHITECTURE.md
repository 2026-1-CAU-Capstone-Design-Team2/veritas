# Veritas — 아키텍처 개요

> AI 에이전트가 프로젝트 구조를 빠르게 파악하기 위한 지도입니다.
> 기능별 상세/변경 이력은 루트 [`README.md`](README.md), 디렉터리별 세부는 각 폴더의 `README.md` 참고.

Veritas는 **로컬 LLM(llama-server) 기반의 리서치 어시스턴트**입니다. 핵심 기능 4가지:
- **AutoSurvey** — 계획 → 수집 → 요약 → gap 분석 → 재계획 반복으로 웹을 조사해 보고서 생성
- **RAG 채팅** — AutoSurvey가 만든 마크다운 산출물을 ChromaDB에 인덱싱해 근거 기반 답변
- **스키마 기반 툴 채팅** — LLM이 프롬프트/스키마만 보고 어떤 tool을 쓸지 결정
- **Proactive 보조** — Native editor(ghostwriting) / 외부 Windows 문서 앱(suggestion card)에서 사용자 입력을 관찰하며 *언제·무엇을·어떻게 제안할지*를 결정. 초기 bandit 시도는 보수화 문제로 폐기되고, **rule-based pipeline** (anchor → CandidateFactory → RuleEvaluator → UserAdaptationMemory) 으로 재구현됨. 상세는 [`PROACTIVE_RULE.md`](PROACTIVE_RULE.md) + [`services/proactive/README.md`](services/proactive/README.md)

설계 원칙:
- **의도 판단은 LLM(프롬프트·스키마)의 몫, 코드는 실행 경계만 강제한다.** (키워드 기반 라우팅 금지)
- **Proactive 분기: hard-coded 어휘 키워드 features 금지.** "근거"/"출처" 같은 단어 기반 heuristic은 도메인/언어 일반화를 깨므로 영구 금지 (회귀 가드 테스트로 보장). 어휘 정보가 필요하면 학습된 신호 (RAG retrieval 등) 로만.

---

## 큰 그림: 3개의 진입점, 공유 코어

```
 [CLI]                  [Desktop GUI]              [HTTP API]
 main.py                frontend/  ──HTTP──▶  api/  (FastAPI)
   │                        │                   │
   └────────────┬───────────┴───────────────────┘
                ▼
   공유 코어:  agent/ · workflows/ · tools/ · services/
                │
   인프라:     llm/ · storage/ · db/ · core/
                │
   상태:       runs/<workspace>/ (파일) · SQLite · ChromaDB
```

- `main.py` — CLI 단독 실행 (AutoSurvey phase별 실행 + 터미널 채팅)
- `frontend/` — PySide6 데스크톱 앱. 직접 코어를 호출하지 않고 **HTTP로 `api/`를 호출**
- `api/` — FastAPI 서버. `AgentRuntime` 싱글톤이 LLM·tool registry·workflow·chat agent를 들고 있음

---

## 빌드 / 실행 / 테스트

```cmd
:: 의존성 (Windows + conda)
:: 테스트/실행 모두 conda env "agent"의 Python 사용
C:/Users/<user>/.conda/envs/agent/python.exe -m unittest discover tests

:: 데스크톱 앱 실행 (llama-server 자동 관리)
python launcher.py

:: 디버그 옵션
python launcher.py --console-logs           ::: 모든 API 자식 stdout을 console로
python launcher.py --screen-debug           ::: [screen_debug] 줄만 console (스크린 파이프라인)
python launcher.py --proactive-debug        ::: [proactive] 줄만 console (proactive 결정 추적)

:: 환경변수로 한 줄 토글도 가능
set VERITAS_PROACTIVE_LOG=1                 ::: --proactive-debug와 동일 효과
```

**테스트 컨벤션**:
- `tests/` 디렉토리, `unittest` 기반 (pytest 미사용)
- 파일명 `test_<topic>.py`, 클래스명 `<Topic>Tests(unittest.TestCase)`
- 전체 실행: `python -m unittest discover tests`. 특정 파일: `python -m unittest tests.test_proactive_evaluator -v`
- 픽스처는 dataclass/factory 함수로 (예: `_anchor()`, `_signals(**overrides)`)
- 외부 의존성(LLM, FastAPI)은 callable injection으로 모의 (예: `ghostwrite_iter=fake_iter` 직접 주입)

---

## Threading 모델

| Component | Thread / Lock | 비고 |
|---|---|---|
| FastAPI route handlers | `def`(스레드풀) 또는 `async def`(이벤트 루프) | 긴 작업은 **plain def** — 이벤트 루프 안 막음 |
| `AgentRuntime` 싱글톤 | `_workspace_lock: RLock` | workspace 전환 시 모든 rebuild 동기화 |
| `ProactiveOrchestrator` | per-workspace 인스턴스 | observe/feedback은 단일 caller 가정 (HTTP 요청 1개당 1번) |
| ProactiveOrchestrator 내부 | `_anchor_reject_lock`, `_tracks_lock` | in-memory dict 보호. fine-grained |
| `UserAdaptationMemory` | `RLock` | apply_feedback / save / get_state_snapshot 동기화 |
| `TimeoutMonitor`, `NullOutcomeMonitor` | daemon thread, 2~5s 폴링 | orchestrator.close() 에서 join (2초 ceiling) |
| `ScreenMonitor` 폴러 | daemon thread (in ChatAgent) | workspace 전환 시 stop → swap → restart |

**규칙**:
- in-memory state mutation은 항상 자체 lock 보유 (`_anchor_reject_state` 변경은 `_anchor_reject_lock` 안에서)
- JSONL append는 lock 없이 OS 단위로 atomic (POSIX 작은 write 보장)
- `policy_state.json` / `user_adaptation.json`은 `write → fsync → os.replace`로 atomic
- `_load_or_init`은 이미 lock 보유 상태이므로 `_gc_locked_internal`처럼 lock 미획득 helper를 별도 분리

---

## 계층 (Layers)

```
표현(Presentation)   frontend/                 PySide6 UI, 컨트롤러, HTTP 클라이언트
경계(API)            api/                      FastAPI 라우터 + api 서비스 + 리포지토리
오케스트레이션        agent/  workflows/         대화 루프 / 결정론적 조사 파이프라인
                     api/services/agent_runtime.py  (서버용 공유 런타임 싱글톤)
역량(Capability)     tools/                    호출 가능한 단위 기능 + ToolRegistry
도메인 서비스         services/                 RAG, 산출물 저장소, 화면 캡처 등 상태/로직 소유
인프라(Infra)        llm/  storage/  db/        LLM 클라이언트 / 벡터DB / SQLite
공유(Shared)         core/                     프롬프트, 공용 데이터 모델
상태(State)          runs/  +  SQLite  +  ChromaDB
```

핵심 어휘 (출처: `tools/README.md`):
> **Tool** = 하나의 실행 가능한 capability · **Workflow** = tool들을 묶은 결정론적 파이프라인
> **Service** = 상태/비즈니스 로직 소유자 · **Agent** = LLM과 tool registry를 잇는 대화 루프

---

## 디렉터리 맵

| 디렉터리 | 계층 | 핵심 파일 | 역할 |
|---|---|---|---|
| `main.py` | 진입점 | — | CLI 진입점: 인자 파싱 → LLM/registry/workflow 와이어링 → phase 실행 또는 채팅 |
| `agent/` | 오케스트레이션 | `chat_agent.py` | `ChatAgent`: 멀티턴 채팅 루프, 채팅 히스토리, 스키마 기반 tool 호출, 화면-개입 루프 |
| `workflows/` | 오케스트레이션 | `autosurvey_workflow.py` | `AutoSurveyWorkflow`: 계획→수집→요약→gap→재계획→최종보고서 결정론적 파이프라인. `progress_callback`으로 진행 이벤트 emit |
| `tools/` | 역량 | `tool.py`(BaseTool/ToolResult), `registry.py`, `loader.py`, `llm_tooling.py` | 각 `*_tool/` 하위에 `tool_schema.json` + `BaseTool` 구현. AutoSurvey 내부 tool(web_search·fetch_webpage·term_grounding·query_plan·document_summarize·final_report)과 chat 노출 tool(current_time·rag·table_query·autosurvey·screen_context) |
| `services/` | 도메인 서비스 | `rag_service.py`, `run_store_tool_funcs/`, `fetch_webpage_tool_funcs/`, `screen_tool_funcs/`, **`proactive/`** | 상태/로직 소유자. RAG 인덱싱·검색, 워크스페이스 산출물 저장(`RunStoreService`/`path_manager`/`record_serializer`), Crawl4AI 페이지 수집, 화면 OCR/UIA 캡처, **proactive 결정 파이프라인** (아래 §"Proactive 서브시스템" 참조) |
| `llm/` | 인프라 | `llama_server_llm.py` | `LLMClient`: OpenAI 호환 llama-server 클라이언트. `ask`/`ask_json`/`iter_ask`(스트리밍)/`embed`. 시작 시 `/props`로 `n_ctx` 자동 감지 |
| `storage/` | 인프라 | `vector_store.py` | `VectorStore`: ChromaDB `PersistentClient` 래퍼. 워크스페이스별 `runs/<id>/chromadb/` |
| `db/` | 인프라 | `db.py`, `schema.py`, `workspace_sync.py` | 로컬 SQLite (`%LOCALAPPDATA%/VERITAS/veritas.db`). 워크스페이스/문서/활동로그/app_state 테이블. `workspace_sync`가 `runs/` 디스크와 DB 동기화·삭제 |
| `core/` | 공유 | `prompts/`(디렉토리: `autosurvey.py`·`chat.py`·`cleanup.py`·`draft.py`·`editor.py`·`verify.py`·**`proactive.py`**), `models.py` | 모든 LLM 프롬프트 copy를 한 곳에 모음. 새 prompt가 필요하면 *반드시* 여기 추가 — 도메인 코드 안에 inline 금지. 공용 데이터 모델(`DocRecord`) |
| `api/` | API 경계 | `api.py`(FastAPI app), `api_routes/`, `services/`, `repositories/` | `api_routes/*` = 기능별 라우터(research·workspaces·documents·draft_chat·document_assist·feedback·screen_monitoring·dashboard·verify·settings). `services/*` = 라우터 뒤 로직, 특히 `agent_runtime.py`가 LLM/registry/workflow/chat_agent를 들고 있는 **싱글톤**. `repositories/state_repository.py` = 인메모리/DB 상태 접근 |
| `frontend/` | 표현 | `api_common.py`(ApiClient), `controllers/`, `ui/` | PySide6 데스크톱. `controllers/agent_controller.py`=HTTP 래퍼, `job_manager.py`=비동기 작업 뮤텍스, `chat_bus.py`=채팅 스트리밍 버스. `ui/pages/*`=화면별 페이지, `ui/windows/`=플로팅 보조창, `ui/main_window.py`=셸+전역 스타일시트, `components/`=재사용 위젯 |
| `runs/` | 상태(파일) | — | 워크스페이스별 산출물. `corpus/raw_html·raw_text/`, `summary/`(doc_*.md, batch_*.md, index.json, plan.json …), `chromadb/`, `final.md`, `screen_context/` |

> 참고: `api/services/`와 `db/`에 비슷한 이름(`dashboard_service` 등)이 일부 중복 존재 — `api/services/`가 현행 경로, `db/`의 동명 파일은 레거시 흔적일 수 있음.

---

## 핵심 데이터 흐름

### 1) AutoSurvey 조사 파이프라인 (`workflows/autosurvey_workflow.py`)
```
term_grounding → query_plan(initial) → scout collect → summarize
   → gap 분석 → query_plan(replan) → [collect → summarize → replan] 반복
   → final_report
```
각 단계는 `tools/`의 tool을 `ToolRegistry`로 호출. tool은 `LLMClient`(llm/)와 `RunStoreService`(services/)를 사용해 `runs/<workspace>/`에 산출물 기록. `progress_callback`으로 단계별 이벤트를 emit → API의 진행률 버퍼로 흐름.

### 2) RAG 채팅
```
AutoSurvey 산출물(summary/*.md) → RAGService.index_autosurvey_output()
   → VectorStore(ChromaDB)에 임베딩 저장
사용자 질문 → ChatAgent → rag tool → RAGService.retrieve()/answer() → 근거 기반 답변
```

### 3) 데스크톱 → API 요청 (대표: 조사 실행)
```
frontend ui/pages/research_page.py
   → controllers/agent_controller.py (HTTP)
   → api/api_routes/research.py
   → api/services/research_service.py
   → api/services/agent_runtime.py (AgentRuntime 싱글톤)
   → workflows/AutoSurveyWorkflow.run_all()
진행 상황은 agent_runtime의 ring buffer → GET /research/progress
   → frontend ResearchProgressPoller(QThread)가 폴링해 UI 갱신
```
긴 작업을 하는 FastAPI 핸들러는 `async def`가 아니라 **plain `def`** — FastAPI 스레드풀에서 돌려 이벤트 루프를 막지 않음.

### 4) Proactive 보조 (rule-based)
```
[Native editor 입력 / 외부 앱 screen capture]
   → frontend editor_window._fire_suggestion() (debounce 800ms 후)
       또는 ScreenContextService 캡처 루프
   → POST /api/v1/proactive/observe              (단순 wrap: 기존 /editor/suggest)
   → api/services/proactive_service.observe()
   → ProactiveOrchestrator.observe()  ─── 다음 5단계 ───
       1. _DocumentTrack: 롤링 텔레메트리 (idle, edit_volume, churn)
       2. _extract_anchor: ActiveAnchor (cursor + paragraph + confidence)
       3. _anchor_reject_state 확인 (in-memory) → cooldown이면 즉시 null
       4. CandidateFactory.build_candidates → ≤3개 ProactiveTask
       5. RuleEvaluator: hard gates + 0..1 rubric score
          score < adjusted_threshold → NullPrediction
          score ≥ threshold → ProactiveTask + context bundle
   → 응답: {decisionId, prediction: "task" | "null", task?, ...}

[ProactiveTask 결정 시]
   → POST /api/v1/proactive/generate/stream(decisionId)
   → ProactiveGenerator.stream → ChatAgent.iter_ghostwrite / iter_editor_assist
       (native+next_sentence retry 시: 이전 거절된 텍스트 + 단락 전체를
        명시적 [현재 문단 전체] 블록으로 prompt body에 삽입)
   → SSE delta 스트리밍 → 편집기 ghost / 외부 카드 렌더링

[사용자 반응]
   → TAB/copy=accept / ESC/거절=reject / 다시=retry / 위치 다름=wrong_anchor
   → POST /api/v1/proactive/feedback(decisionId, action, metadata)
   → UserAdaptationMemory.apply_feedback()
       threshold_offset 누적 (±0.20 clamp)
       per-anchor reject ladder 갱신 (in-memory)
       3번 reject at same anchor → 180s anchor cooldown
```
자세한 알고리즘 / 게이트 / 영구화 규칙은 [`PROACTIVE_RULE.md`](PROACTIVE_RULE.md)와 [`services/proactive/README.md`](services/proactive/README.md).

### 5) 검증 (Verify) + Cross-check (내부↔외부 교차 검증)
```
frontend verify_page.py "검증 시작"
   → POST /api/v1/verify/jobs
   → api/services/verify_service.create_verify_job()
   → AgentRuntime.run_verification()
   → services/verification/service.py VerificationService.run()
       task 1. sections    — final.md 섹션별 근거 문장 배치 (외부 문서만)
       task 2. reliability — 문서별 신뢰도 (높음/중간/낮음, 외부 문서만)
       task 3. consensus   — 외부 출처 간 합의/충돌 (BM25+임베딩+그래프)
       task 4. crosscheck  — 외부 문서 key_points ↔ 로컬 문서 문장 비교
                              (knowledge/sources.json 의 로컬 출처 사용)
                              → numeric_mismatch / contradicts flag 산출
   → VerificationPersistence.persist() → runs/<ws>/verification/*.json

결과 표시 (frontend verify_page.py):
   GET /api/v1/verify/summary
   → verify_view.crosscheck_overview() 가 flag의 claim id를
     내부 주장(텍스트+파일 경로) / 외부 주장(텍스트+도메인) 쌍으로 풀어서 반환
   → 페이지 구성 (위에서 아래로):
       [요약 타일] → [보고서 흐름 구조: 접힘, 헤더에 "✓ 섹션 N개" 배지]
       → [자료별 검증 결과: 접힘, 헤더에 등급별 카운트 배지]
       → [Cross-check 결과: 항상 펼침 — 불일치 건별로 내부/외부 주장 + 출처]
```
crosscheck 의 claim 비교는 token overlap + 수치 비교의 순수 알고리즘 (LLM 호출 없음).
로컬 문서 본문은 외부로 전송되지 않으며, 비교는 전부 로컬에서 수행된다.
crosscheck flag 는 초안(Draft) 생성 시 `KnowledgePackBuilder._load_conflict_notes()` 가
"Cross-check Notes" 로 읽어 LLM 컨텍스트에도 주입된다.

### 6) 문서 요약 인용 팝업 (Citation Source-Preview)
```
frontend ui/pages/document_page.py (요약 뷰 = QTextBrowser)
   ← apply_markdown(linkify_citations(final.md))
       # bracketed [doc_NNN] + bare doc_NNN/doc-NNN/docNNN 모두 →
       #   [[doc_NNN]](veritas-citation:doc_NNN?claim=<enc>)
       #   (라벨은 항상 [doc_NNN], 축약형 docN은 zero-pad [doc_00N])
       # code fence / inline code / 기존 링크 target / URL·path 내부는 제외
   사용자가 인용 클릭 → anchorClicked(QUrl)
   → frontend/citation_links.parse_citation_url() → (doc_id, claim)
   → controllers/agent_controller.get_document_citation()   (HTTP, off-thread)
   → GET /api/v1/documents/{ws}/citations/{docId}?claim=...
   → api/services/document_citation_service.get_citation()
       1. doc_id/workspace 정규화 (digit-only, path-traversal 차단)
       2. summary/index.json 에서 title/url/domain 조회
       3. clean_md/<id>.md 읽어 paragraph/sentence 분할
       4. claim ↔ 문장 결정론적 lexical scoring
          (exact substring → 숫자/토큰 overlap) → best 후보 + confidence
          **LLM 호출 없음, 추가 영속 artifact 없음**
   → CitationPopup (Qt.Popup, 외부 클릭 시 자동 닫힘)
       출처 메타데이터 + 하이라이트된 원문 문단 표시
```
링크화는 순수 presentation concern(`final.md` 원문 무수정), 매칭은 클릭 시점 read-only.
웹 출처(`clean_md`)만 대상 — proactive/verification 파이프라인과 분리된 독립 capability.

---

## Proactive 서브시스템 (`services/proactive/`)

Bandit (Action-Centered Engage + LinUCB) 시도가 cold-start에서 빠르게 보수화되는 문제로 폐기되고 **rule-based deterministic pipeline**으로 재구현된 결과. Bandit 코드는 `legacy_bandit/`에 frozen, **production 절대 import 금지** (회귀 가드 테스트가 검사).

### 파일 책임 (16 modules)

| 파일 | 책임 |
|---|---|
| `models.py` | `ProactiveObservation` (observe-tick 입력 shape) + `Surface` Literal |
| `proposal_models.py` | `ProactiveTask` / `NullPrediction` / `SurfaceCapabilities` (출력 shape + 카탈로그) |
| `anchors.py` | `ActiveAnchor` 추출 + confidence bands (native_cursor 0.95+, uia_caret 0.75~0.95, ocr 0.20~0.65) |
| `features.py` | primitive 정규화 (idle, churn, paragraph_len, ...). **어휘 키워드 features 금지** |
| `candidates.py` | `build_candidates`: anchor confidence + signal gate별 ≤3 후보. LLM 호출 없음 |
| `evaluator.py` | hard gates + 0..1 rubric score + `adjusted_threshold`. `BASE_SHOW_THRESHOLD=0.50` |
| `adaptation.py` | `UserAdaptationMemory`: threshold_offset, anchor_cooldowns(external만), task_type suppression(external만) — `user_adaptation.json` 영구화 |
| `context_selector.py` | `ContextBundle` 빌더 — anchor-relative만 (whole-doc 검색 금지) |
| `generator.py` | `ProactiveTask + ContextBundle` → SSE 라우터. native_retry 시 explicit context block 삽입 |
| `policy_store.py` | `ProactiveStore`: per-workspace JSONL 로그 (decisions / feedback / updates / null_outcomes / pending_timeouts) |
| `timeout_monitor.py` | render 타임아웃 sweeper (20s ghost / 30s inline_diff / 45s external card) |
| `null_outcome_monitor.py` | NullPrediction의 30s horizon 후 TN/FN proxy 분류 |
| `orchestrator.py` | 메인 컨트롤러: observe / record_feedback / explain / reset + **in-memory `_anchor_reject_state` ladder** |
| `screen_bridge.py` | 기존 ChatAgent screen pipeline과 proactive observe 연결 (shadow learning) |
| `telemetry.py` | console (`--proactive-debug`) + per-workspace `proactive.log` |
| `reward.py` | `canonicalize_feedback`: surface별 raw action → canonical name |

### 핵심 영구 규칙 (회귀 가드 테스트로 보장)

1. **Hard-coded 어휘 키워드 features 금지** — `_EVIDENCE_KEYWORDS` / `_FACTUAL_KEYWORDS` 등은 회귀 시 `tests/test_proactive_features.py::NoKeywordModulesTests`에서 fail.
2. **Bandit production import 금지** — `tests/test_proactive_api.py::test_orchestrator_module_does_not_import_bandit`가 검사.
3. **Prompt copy는 `core/prompts/proactive.py`에만** — generator는 router 역할만, prompt 문자열 정의 없음.
4. **JSONL/JSON에 raw document text 누출 금지** — `decisions.jsonl`은 char counts + anchor hash만. `tests/test_proactive_api.py::test_decisions_jsonl_contains_no_raw_text`가 sentinel 검사.

### Native Reject Ladder (orchestrator in-memory)

원칙: **anchor별 독립**. 한 anchor에서 reject가 누적돼도 다른 anchor에는 무영향.

| Reject 횟수 (같은 anchor) | 다음 observe 동작 | LLM이 받는 context |
|---|---|---|
| 0 | `iter_ghostwrite` 기본 ghost | 편집기 raw prefix |
| 1 | `editor_assist` + retry lead-in + 변경 지시 | `[직전 N문장]` 블록 + 이전 거절 텍스트 명시 |
| 2 | (1) + "방향/주제 자체를 다르게" 강화 지시 | `[현재 문단 전체]` 블록 + 이전 거절 텍스트 |
| 3 | 그 anchor에 대해 **180초 cooldown** | — |

타 anchor로 이동하면 fresh ladder. 같은 anchor로 복귀하면 cooldown 유지 (accept만 ladder를 클리어).

### Env Overrides (운영자용)

| Var | 기본 | 역할 |
|---|---|---|
| `VERITAS_PROACTIVE_LOG` | 0 | proactive 줄을 stdout에 출력 |
| `VERITAS_PROACTIVE_ANCHOR_COOLDOWN_S` | 60 | adaptation의 external anchor cooldown |
| `VERITAS_PROACTIVE_SUPPRESSION_REJECTS` | 5 | external task_type suppression 발동 횟수 |
| `VERITAS_PROACTIVE_SUPPRESSION_S` | 300 | suppression 지속 |
| `VERITAS_PROACTIVE_SCREEN` | 1 | screen pipeline → proactive 라우팅 ON/OFF |

### Telemetry 라인 포맷 레퍼런스

```
[proactive][decision] pd_<16hex> <surface> task=<TaskType> anchor=<anc_id>
  conf=<0..1> scope=<ContextScope> render=<RenderMode>
  score=<0..1> threshold=<0..1|BLOCKED> candidates=<N>
  idle=<sec>s churn=<0..1> recent_neg=<0..1>

[proactive][decision] pd_<id> <surface> prediction=null
  reason=<reason_code> candidates=<N> gates=[<reason_a>,<reason_b>,...]
  best_score=<0..1>? threshold=<0..1|BLOCKED(cooldown_or_suppression)>?

[proactive][feedback] pd_<id> <surface> <canonical> task=<TaskType|->
  anchor=<anc_id|->  Δthr=<±0.NNN>?  cooldown_set=<key>?  suppressed_until=<iso>?

[proactive][null_outcome] pd_<id> <tn_proxy|fn_proxy|unknown>
  vol=<chars> churn=<0..1> idle=<sec>s

[proactive][admin] <free-form, 예: "policy reset workspace=<id>">
```

reason 코드 모음: `low_anchor_confidence` / `no_candidate` / `all_candidates_gated` /
`score_below_threshold` / `blocked_by_cooldown_or_suppression` / `anchor_reject_cooldown`.

gate 코드 모음: `anchor_missing` / `anchor_confidence_too_low` / `off_anchor_target` /
`surface_render_unsupported` / `context_insufficient` / `active_typing_not_stable` /
`cooldown_same_anchor_task` / `same_task_recently_rejected` /
`no_relevant_source_for_strong_evidence_task` / `per_anchor_3_reject_cooldown`.

### JSONL/JSON 스키마 예시

**`decisions.jsonl`** — 모든 observe (task/null) 한 줄씩:
```json
{
  "decision_id": "pd_abcd1234ef567890",
  "timestamp": "2026-05-28T05:30:00.000Z",
  "workspace_id": "World_Model-3",
  "surface": "native_editor",
  "prediction": "task",
  "task_type": "next_sentence",
  "anchor_id_hash": "anc_<16hex>",
  "anchor_confidence": 0.95,
  "anchor_source": "native_cursor",
  "context_scope": "cursor_previous_sentences",
  "render_mode": "native_ghost",
  "candidate_count": 1,
  "evaluator_score": 0.789,
  "evaluator_breakdown": {
    "anchor_confidence": 0.95, "need_signal": 0.7, "context_sufficiency": 1.0,
    "task_fit": 0.85, "source_support": 1.0, "interruption_risk": 0.2,
    "recent_negative_rate": 0.0
  },
  "threshold": 0.500,
  "gate_reasons": [],
  "primitive": { "idle_sec": 0.0, "paragraph_len": 60, ... },
  "context_meta": { "scope": "...", "char_counts": {...} },
  "raw_text_saved": false
}
```

**`user_adaptation.json`** — 워크스페이스별 단일 문서:
```json
{
  "workspace_id": "World_Model-3",
  "version": 1,
  "updated_at": "2026-05-28T05:32:00Z",
  "global_stats": {
    "accept_ema": 0.0, "reject_ema": 0.0, "retry_ema": 0.0,
    "timeout_ema": 0.0, "recent_negative_rate": 0.0
  },
  "task_type_stats": {
    "next_sentence": {
      "accept": 3, "reject": 5, "retry": 1, "timeout": 0, "wrong_anchor": 0,
      "recent_reject_iso": [],
      "suppressed_until": null
    }
  },
  "anchor_cooldowns": {
    "<anc_id>|paragraph_rewrite": {
      "cooldown_until": "2026-05-28T05:35:00Z",
      "reason": "reject"
    }
  },
  "threshold_offset": 0.075,
  "last_intervention_at": "2026-05-28T05:30:00Z",
  "last_feedback_at": "2026-05-28T05:31:30Z",
  "prompt_style_flags": { "prefer_shorter": true, "recent_retry_count": 2 }
}
```

**불변식**:
- `decisions.jsonl` / `feedback.jsonl` / `updates.jsonl`은 **append-only** — 마이그레이션 도구가 새 필드 추가 시 backward-compat 필수
- `user_adaptation.json`은 atomic-write 단일 문서. load 시 `recent_negative_rate`와 모든 `accept_ema/...` 리셋, `_gc_locked_internal` 호출
- `raw_text_saved: false`는 절대 `true`가 되면 안 됨 — 회귀 테스트가 sentinel 검사

### Walkthrough: 새 ProactiveTask type 추가

말로만 "표 보고 추가하라"가 아니라 실제 순서:

```
1. proposal_models.py
   └─ TaskType Literal에 "my_new_task" 추가
2. core/prompts/proactive.py
   └─ LEAD_IN_EXTERNAL["my_new_task"] = "[과업] ...\n[원문]\n"
   └─ LEAD_IN_NATIVE["my_new_task"]   = "..."  (네이티브 inline-diff용)
3. candidates.py
   └─ _maybe_my_new_task(anchor, signals, surface) -> Optional[ProactiveTask]
       └─ confidence/길이/신호 게이트 작성
       └─ render = _native_or_external(surface, native=..., external=...)
       └─ return ProactiveTask(task_type="my_new_task", target_anchor_id=anchor.anchor_id, ...)
   └─ build_candidates의 builder tuple에 추가
4. context_selector.py
   └─ ContextScope Literal에 새 scope 필요시 추가
   └─ materialize_context에 새 scope의 text_parts 빌더 추가
5. evaluator.py
   └─ _need_signal: if task_type == "my_new_task": return ...
   └─ _task_fit:    if task_type == "my_new_task": return ...
   └─ _source_support / _interruption_risk 필요 시
6. generator.py
   └─ _ASSIST_ACTION["my_new_task"] = "rewrite" 또는 적절한 action
   └─ (next_sentence 같이 ghostwrite로 가는 게 아니면 _compose_body에 빌더 추가)
7. tests/test_proactive_candidates.py
   └─ test_<my_new_task>_emits_when_conditions_met
   └─ test_<my_new_task>_does_not_emit_when_<gate>
8. services/proactive/README.md §3.1 표에 행 추가, ARCHITECTURE.md 표 §"File 책임"에는 변경 없음
9. python -m unittest discover tests 통과 확인
```

검증 가드: candidate.target_anchor_id == anchor.anchor_id (off_anchor 게이트), surface.supports(render_mode) (unsupported 게이트), context 필드가 anchor에 존재 (context_insufficient 게이트). 모두 [`evaluator.py:check_hard_gates`](services/proactive/evaluator.py)가 잡음.

### 디버깅: 콘솔 로그 해석 치트시트

```
score=0.789 threshold=0.500 candidates=1     → 정상 task 결정 (PASS)
prediction=null reason=score_below_threshold → 후보는 있는데 점수 부족
prediction=null reason=anchor_reject_cooldown → 같은 anchor에서 3번 reject → 180초 cooldown
threshold=BLOCKED(cooldown_or_suppression)    → adaptation의 +inf sentinel
gates=[off_anchor_target,context_insufficient] → 모든 후보가 hard gate에서 떨어짐

idle=0.0s in native_editor                    → 정상 (debounce 트리거 → 항상 0)
idle=15.0s in external_app                    → 사용자가 paused 상태
recent_neg=0.67                               → 누적 reject EMA (재시작 시 0으로 리셋)

[proactive][feedback] ... accept Δthr=-0.015                  → 정상 학습
[proactive][feedback] ... reject Δthr=+0.030 cooldown_set=... → external만 cooldown set
[proactive][feedback] ... reject Δthr=+0.030                  → native (cooldown은 in-memory ladder가 처리)
```

추가 진단:
```cmd
curl http://127.0.0.1:8000/api/v1/proactive/snapshot?workspaceId=<ws>
curl http://127.0.0.1:8000/api/v1/proactive/explain/pd_<id>
curl -X POST "http://127.0.0.1:8000/api/v1/proactive/reset?workspaceId=<ws>"
```

---

## 상태(State)는 어디에 사는가

| 저장소 | 위치 | 내용 |
|---|---|---|
| 워크스페이스 산출물 | `runs/<workspace>/` (또는 `--output-dir`) | 원본 HTML/텍스트, 문서·배치 요약, plan/grounding/index JSON, `final.md` |
| 벡터 인덱스 | `runs/<workspace>/chromadb/` | RAG용 임베딩 (ChromaDB SQLite, 웹 + 로컬 문서) |
| 로컬 문서 색인 | `runs/<workspace>/local/`, `knowledge/` | 로컬 파일 manifest(`manifest.json`), 추출 텍스트(`extracted_md/`), 표 프로필, 출처 목록(`sources.json`) |
| 검증 산출물 | `runs/<workspace>/verification/` | sections/reliability/consensus 결과 + `crosscheck.json` (내부↔외부 claim 비교: claims/relations/flags) |
| **Proactive 적응 상태** | **`runs/<workspace>/proactive_policy/`** | `user_adaptation.json`(threshold/cooldown/suppression), `decisions.jsonl` (gate_reasons + score + char_counts, **raw text 없음**), `feedback.jsonl`, `updates.jsonl`, `null_outcomes.jsonl`, `pending_timeouts.jsonl`, `proactive.log` |
| 앱 메타데이터 | `%LOCALAPPDATA%/VERITAS/veritas.db` | 워크스페이스 목록, 문서, 활동 로그, `app_state`(현재 워크스페이스 등) |
| 서버 런타임 상태 | 인메모리 (`AgentRuntime` 싱글톤) | 현재 워크스페이스의 LLM/registry/workflow/chat_agent, 조사 진행 ring buffer, **per-workspace `ProactiveOrchestrator`** (포함: `_anchor_reject_state` ladder, decision cache 512개 FIFO, render-timeout + null-outcome sweeper threads) |

워크스페이스 = `runs/` 아래 폴더 하나. `db/workspace_sync.py`가 디스크 폴더와 SQLite 행을 부팅 시 동기화하고 사용자 삭제를 처리.

---

## 코드 변경 시 어디를 봐야 하나

| 하고 싶은 일 | 보는 곳 |
|---|---|
| 새 tool 추가 | `tools/<new_tool>/`에 `tool_schema.json`+`BaseTool` → `tools/loader.py`에 등록 (절차는 `tools/README.md`) |
| 조사 파이프라인 단계 수정 | `workflows/autosurvey_workflow.py` |
| 프롬프트 수정 | `core/prompts/*.py` 중 해당 영역 파일 (autosurvey/chat/draft/editor/verify/proactive). **코드에 프롬프트 인라인 금지** |
| 새 API 엔드포인트 | `api/api_routes/`에 라우터 + `api/services/`에 로직 |
| 데스크톱 화면 추가/수정 | `frontend/ui/pages/` + `controllers/agent_controller.py`에 HTTP 호출. 접을 수 있는 카드가 필요하면 `frontend/components/cards.py`의 `CollapsibleCard` (헤더 상태 배지 = `set_status()`) |
| 검증 task 추가/수정 | 알고리즘 → `services/verification/` (sections·reliability·consensus·crosscheck). UI 변환 → `api/services/verify_view.py`. 화면 → `frontend/ui/pages/verify_page.py` |
| 내부↔외부 교차 검증(crosscheck) 수정 | 비교 알고리즘 → `services/verification/crosscheck/pipeline.py`. API 응답 → `verify_view.crosscheck_overview()`. 패널 → `verify_page.py:_CrosscheckPanel` |
| LLM 호출 방식 변경 | `llm/llama_server_llm.py` (로컬) / `llm/openai_chat_llm.py` (OpenAI, AutoSurvey 전용) |
| 영속 데이터 스키마 변경 | 파일 산출물 → `services/run_store_tool_funcs/`, SQLite → `db/schema.py` |
| **Proactive 후보 type 추가** | `proposal_models.py`의 `TaskType` Literal + `candidates.py`에 `_maybe_*` 빌더 + `evaluator.py`의 `_need_signal`/`_task_fit` 분기 + `core/prompts/proactive.py`의 lead-in. README.md §3 업데이트 |
| **Proactive 게이트 조정** | hard gate → `evaluator.py:check_hard_gates`. score 계수 → `evaluator.py:ScoreBreakdown.total`. threshold → `BASE_SHOW_THRESHOLD` 또는 `adjusted_threshold`. native reject ladder 상수 → `orchestrator.py:NATIVE_ANCHOR_REJECT_LIMIT` / `_COOLDOWN_S` |
| **Proactive 영구화 layout 변경** | `policy_store.py`의 경로 + `adaptation.py`의 dataclass. 회귀 가드: raw text 누출 / bandit import 금지 테스트 깨지지 않게 주의 |
| final.md 인용 팝업(citation source-preview) | 매칭 알고리즘 → `api/services/document_citation_service.py`(pure 헬퍼). route → `api/api_routes/documents.py`(얇게). 링크화/URL 파싱 → `frontend/citation_links.py`(Qt-free pure). UI/팝업 → `frontend/ui/pages/document_page.py`(`CitationPopup`) |

피해야 할 패턴:
- `chat_agent.py`의 키워드/정규식 라우터, user 메시지 단어 기반 tool 강제 호출 — 의도 판단은 프롬프트·스키마로.
- **Proactive 분기에서 hard-coded 어휘 키워드 추가** ("근거"/"출처"/"통계" 같은 도메인 단어 리스트로 feature 산출) — 회귀 가드 테스트(`NoKeywordModulesTests`)가 fail.
- **`services/proactive/legacy_bandit/`의 코드를 production에서 import** — 동일하게 가드 테스트가 fail.
- **`services/proactive/generator.py`에 prompt 문자열 인라인** — 모든 prompt copy는 `core/prompts/proactive.py`로.
- **Proactive 영구화 JSONL/JSON에 raw document text 저장** — 영구화는 char counts + anchor hash만.

---

## 변경 이력 (Implementation Log)

> Codex `INSTRUCTION.md` 기반 구현 결과를 diff 중심으로 기록한다.
> 항목별 형식: **기능 / 변경 파일 / 엔지니어링 결정 / 테스트**.

### 2026-06-04 — final.md Citation Link Popup

**기능**: 요약 뷰(`DocumentPage`)에서 `final.md`의 `[doc_NNN]` 인용 마커를 클릭 가능한 링크로 렌더링하고, 클릭 시 해당 `clean_md/<id>.md` 원문에서 근거 문장을 찾아 하이라이트한 팝업으로 보여준다. 팝업은 다른 곳을 클릭하면 자동으로 닫힌다. **추가 LLM 호출 없음, 추가 영속 artifact 없음.**

**변경 파일**
- (new) `api/services/document_citation_service.py` — `get_citation()` + 순수 매칭 헬퍼(`normalize_text`/`tokenize`/`split_paragraphs`/`split_sentences`/`score_sentence`/`match_claim_in_source`). 파일 I/O는 얇게, 스코어링은 전부 side-effect-free pure 함수.
- (edit) `api/api_routes/documents.py` — `GET /api/v1/documents/{workspaceId}/citations/{docId}?claim=` 얇은 route wrapper 추가.
- (edit) `frontend/controllers/agent_controller.py` — `get_document_citation()` HTTP wrapper.
- (new) `frontend/citation_links.py` — Qt-free 순수 헬퍼: `linkify_citations()` / `extract_claim_from_line()` / `parse_citation_url()`.
- (edit) `frontend/ui/pages/document_page.py` — 요약 뷰어 `QTextEdit`→`QTextBrowser`(anchorClicked), 렌더 직전 linkify, 비동기 인용 조회, `CitationPopup`(Qt.Popup).
- (new) `tests/test_document_citations.py` — 서비스 매칭 + 링크화 14 케이스.

**엔지니어링 결정**
- *No new persisted span*: `final.md` 생성 단계에 sentence span을 저장하지 않고, 클릭 시점에 claim을 API로 보내 결정론적 lexical matching으로 원문을 찾는다. → AutoSurvey 파이프라인(`final_report_tool`/`document_*_tool`) 무변경, OpenAI/local 양쪽 추가비용 0.
- *마커 형식 정규화(presentation layer)*: bracketed `[doc_NNN]`와 bare `doc_NNN`/`doc-NNN`/`docNNN`를 **모두** 링크화하되, 표시 라벨은 항상 `[doc_NNN]`로 정규화한다(nested-bracket `[[doc_NNN]](href)`). 원문이 bare였는지 bracketed였는지 사용자가 신경 쓰지 않게 한다. (※ 초기 구현은 bare를 제외했으나, 실데이터에 bare-only 보고서가 존재해 일부 인용이 클릭 불가 → 사용자 테스트 피드백으로 정정. 표 셀의 bare 인용도 inline 링크로 렌더되며 표 구조는 유지됨.)
- *Path traversal 차단 2중*: `doc_id`는 digit-only 정규식(`^(?:doc[_-]?)?(\d+)$`) 통과만 허용하고, 최종 경로를 재-resolve 하여 `clean_md/` 직속인지 확인. `workspace_id`에 `/ \ ..` 포함 시 거부.
- *링크 스킴*: `veritas-citation:doc_NNN?claim=<percent-encoded>` — doc_id는 path, claim은 **단일** query param. HTML 속성에서 `&`→`&amp;` escape로 깨지는 문제를 피하려 멀티 param을 쓰지 않음. 클릭 핸들러는 `QUrl.toString(FullyEncoded)`를 Qt-free pure parser에 넘겨 1회만 디코드.
- *레이어 경계 준수*: frontend는 `runs/`를 직접 읽지 않고 `AgentController → HTTP → api/services`만 사용. route는 얇게, 파일읽기/매칭/메타조립은 service. linkify/parse는 Qt-free pure 모듈로 분리해 PySide 없이 단위테스트.
- *비차단 UI*: 인용 조회는 `JobManager.run_detached`로 off-thread 실행. 팝업은 `Qt.Popup`이라 외부 클릭 시 Qt가 자동 close(수동 focus 추적 불필요).
- *Pill vs hyperlink*: Qt rich text가 inline `<a>`의 border-radius/배경을 안정적으로 렌더하지 못해, INSTRUCTION의 fallback인 파란 하이퍼링크 스타일 채택.
- *Low-confidence UX*: 매칭 점수가 낮아도 crash 없이 "가장 가까운 원문 후보"로 best 후보를 표시(confidence=`low` 경고). 원문 자체가 없으면 "원문 위치를 확정하지 못했습니다".

**테스트**
- `python -m unittest tests.test_document_citations -v` → **14 passed**.
- 전체 `python -m unittest discover tests` → 본 기능과 무관한 **기존 실패 13건만 잔존**(RAG/OpenAI 모듈; 예: `RAGService._format_recent_history` 부재). 본 변경 전부 stash 시 동일하게 재현되어 회귀 아님을 확인.
- 실데이터 스모크(`runs/World_Model`, doc_000): 인용 → exact 문장 `high`(score 0.62) 매칭 + 메타데이터 정상.

### 2026-06-04 (follow-up) — Citation UX: 대괄호 라벨 + bare marker 링크화

**배경(사용자 테스트 피드백)**: (1) 링크화 시 화면에서 대괄호가 사라져 `doc_000`만 보였다. (2) `final.md`에 bare `doc_NNN`만 쓴 보고서가 있어 일부 인용이 클릭 불가였다(실데이터 `World_Model/final.md` = bracketed 0 / bare 15).

**변경 파일**
- (edit) `frontend/citation_links.py` — bracketed + bare(`doc_000`/`doc-000`/`docNNN`) 모두 링크화, 라벨은 nested-bracket `[[doc_NNN]]`로 정규화. inline-protected 분할을 도입해 inline code / 기존 md 링크·이미지 / autolink / URL·path 내부 마커는 제외(기존엔 fenced code만 제외했음). `extract_claim_from_line`이 bare 마커도 제거.
- (edit) `core/prompts/autosurvey.py` `FINAL_PROMPT` — "body·표 셀 어디서나 bracketed `[doc_NNN]`만, bare 금지" 로 규칙 보강 + "substantive claim에만 인용" 일반 원칙. **keyword 리스트 없음, LLM call 수 불변.**
- (edit) `tests/test_document_citations.py` — bracket 보존(escaped)+렌더 라벨, bare linkify·정규화, inline/fence/링크-target/URL·path 제외, bare claim stripping 케이스 추가(총 19).
- (edit) `ARCHITECTURE.md` — 위 "bare 제외" 결정 정정.

**엔지니어링 결정**
- *Bracket label (nested, not escaped)*: 라벨은 nested-bracket `[[doc_NNN]](href)`로 생성 → `[doc_NNN]` 텍스트 링크로 렌더. **escaped `[\[doc_NNN\]]`는 금지** — 요약 렌더러의 `markdown_view._extract_math`가 `\[ … \]`를 LaTeX display math로 인식해 라벨을 수식으로 삼켜버려(밑줄→subscript, 앞뒤 빈 줄 삽입) 링크가 깨진다. 실제 렌더 경로(`linkify → _extract_math → _normalize_for_qt → markdown`)로 회귀 테스트(`test_renders_through_document_math_pipeline`) 추가.
- *정규화*: 어떤 spelling이든 digit만 추출해 canonical `doc_NNN` href + `[doc_NNN]` 라벨 → 클릭 대상이 service의 기존 `_normalize_doc_id` 허용 형식과 일치(서비스 무변경).
- *오탐 차단*: bare 마커는 look-around(`(?<![\w./:-])…(?![\w-])`) + protected-span 분할로 `clean_md/doc_000.md`·`http://…/doc_000`·inline code·기존 링크 target을 건드리지 않음.
- *Over-citation 비목표*: 외부 API cleanup에 keyword 기반 paragraph 삭제는 도입하지 않음(언어/도메인 일반화 불가·본문 손실). source 품질은 `FINAL_PROMPT` 지시처럼 일반화 가능한 수단으로만 유도. `raw_md→clean_md` pass-through 무변경.

**테스트**: `python -m unittest tests.test_document_citations -v` → **19 passed**. 실데이터 `World_Model/final.md`: bare 15개 전부 `[doc_NNN]` 링크로 변환, 표 렌더 정상.

### 2026-06-04 (follow-up #2) — Citation 정규화: zero-pad canonical id + endpoint plain def

**배경(Codex 리뷰)**: (1) 축약형 `doc7`/`doc_7`/`doc-7`가 `[doc_7]`로 렌더되면 `FINAL_PROMPT`의 canonical `[doc_NNN]`(3-digit) 규칙과 UI presentation이 충돌. (2) citation endpoint가 `async def`면 동기 파일 읽기 + source sentence 스캔이 FastAPI event loop를 막는다.

**변경 파일**
- (edit) `frontend/citation_links.py` — 라벨/href의 doc id를 `f"doc_{int(digits):03d}"`로 zero-pad. `doc7`/`doc_7`/`doc-7`/`[doc_7]` 모두 `[doc_007]` 렌더 + `doc_007` href. (4자리 이상은 그대로 유지.)
- (edit) `api/api_routes/documents.py` — `document_citation`을 `async def` → **plain `def`**로 변경(FastAPI threadpool 실행, event loop 비차단). summary/merged는 단순 1-파일 read라 범위 밖으로 유지(최소 수정).
- (edit) `tests/test_document_citations.py` — `doc7`/`doc_7`/`doc-7`/`[doc_7]` → `[doc_007]`+`doc_007` 렌더 검증 + 단축 id가 3-digit `007.md`로 resolve되어 `docId=doc_007` 반환하는 service 케이스 추가(총 21).

**엔지니어링 결정**
- *Zero-pad는 presentation(frontend)에서만*: backend `_normalize_doc_id`/`_clean_md_path`는 무변경. frontend가 canonical `doc_007`을 보내고 backend의 기존 `int(stem):03d` fallback이 3-digit 파일과 매칭 → 라벨·href·파일명·프롬프트 규칙이 전부 3-digit으로 정렬.
- *endpoint plain def 최소 범위*: Codex가 지적한 citation endpoint만 변경. 무거운 작업(문장 스캔)을 가진 핸들러만 threadpool로 내려 event loop를 보호(저장소 §"Threading 모델"의 "긴 작업은 plain def" 원칙 일치).

**테스트**: `python -m unittest tests.test_document_citations -v` → **21 passed**. 전체 `discover tests` → 동일 사전-존재 실패 13건만, 신규 회귀 0. `inspect.iscoroutinefunction(document_citation) == False` 확인.
