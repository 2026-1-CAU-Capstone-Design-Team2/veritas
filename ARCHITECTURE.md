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

> 참고: 대시보드 `service`는 `api/services/dashboard_service.py` 하나로 일원화됨(레거시 `db/dashboard_service.py` 제거, 2026-06-08). `db/dashboard_repository.py`는 db 계층 repository로 남아 API 서비스가 사용(파일 직접 접근은 frontend가 아닌 API 프로세스). frontend는 `db/`를 직접 import하지 않는다 — 단, 부팅 reconcile(`frontend/ui/main.py` → `db.workspace_sync`)은 아직 직접 호출(인프라 경로, 추후 정리 대상).

---

## 핵심 데이터 흐름

### 1) AutoSurvey 조사 파이프라인 (`workflows/autosurvey_workflow.py`)
```
term_grounding → query_plan(initial) → scout collect → summarize
   → gap 분석 → query_plan(replan) → [collect → summarize → replan] 반복
   → final_report
```
각 단계는 `tools/`의 tool을 `ToolRegistry`로 호출. tool은 `LLMClient`(llm/)와 `RunStoreService`(services/)를 사용해 `runs/<workspace>/`에 산출물 기록. `progress_callback`으로 단계별 이벤트를 emit → API의 진행률 버퍼로 흐름.

`fetch_webpage`(`services/fetch_webpage_tool_funcs/crawl4ai_fetch.py`)는 Crawl4AI의 de-chrome된 `fit_markdown`을 우선 `raw_md`로 쓰되, 과하게 깎였으면(`_FIT_MIN_RATIO=0.25`/`_FIT_MIN_CHARS=500` 미달) `raw_markdown`으로 fallback — chrome 제거의 1차 레버. `document_cleanup` 외부 API(batch) 경로는 추가로 archived `corpus/raw_html`의 **block-run 구조적 추출**(HTML tag/ARIA role + 구조 통계만, keyword/selector 금지 — `html_body_extractor.py`)을 시도하고, **quality gate**(prose/table/link-density; 고정 retention 아님)를 통과하면 채택, 아니면 raw_md fallback(사유는 `cleanup_provenance`에 기록). `final_report`는 LLM 입력을 JSON blob이 아닌 **sectioned text**(`render_final_report_input`; allowlist plan 필드만, search_queries 제외)로 넘기고, 저장 직전 `## User Request`의 JSON 누출을 결정론적으로 복구(`repair_user_request_section_if_leaked`)한 뒤 `## Source Notes` 표만 정규화한다(`core/report_markdown_normalizer.py`).

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
       4. 3단계 resolution (**LLM 호출 없음, 추가 영속 artifact 없음**):
          a. direct      — final claim ↔ clean_md 문장 lexical match (score≥0.50)
          b. batch_anchor — 약하면 같은 doc를 인용한 summary/batch_*.md finding 중
                            final claim과 가장 겹치는 것을 골라, 그 finding으로
                            clean_md 문장을 재탐색 (anchor score≥0.45)
          c. document_only — 둘 다 약하면 match=None. 무관한 "가장 가까운 문장"을
                            highlight하지 않고 문서 수준 fallback으로 반환
   → CitationPopup (Qt.Popup, 외부 클릭 시 자동 닫힘)
       direct/batch_anchor → 하이라이트된 원문 문단,
       document_only → "문서 수준 근거로 연결됐지만 정확한 문장 위치 미확정" 안내
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
| final.md 인용 팝업(citation source-preview) | 매칭/anchor 알고리즘 → `api/services/document_citation_service.py`(pure 헬퍼, 3단계 resolution). route → `api/api_routes/documents.py`(얇은 plain def). 링크화/URL 파싱 → `frontend/citation_links.py`(Qt-free pure). UI/팝업 → `frontend/ui/pages/document_page.py`(`CitationPopup`) |
| 외부 API(batch) 문서 cleanup 품질 | block-run 구조적 추출 + quality verdict → `services/document_cleanup_tool_funcs/html_body_extractor.py`(`extract_main_text_with_stats`; tag/role + 구조 통계만, keyword 금지). gate/provenance 배선 → `tools/document_cleanup_tool/document_cleanup_tool.py:_batch_clean_body`(quality-based, 고정 retention 아님). 프롬프트 guardrail → `core/prompts/cleanup.py`·`autosurvey.py`. **local per-doc 경로는 변경 금지** |
| final.md `## Source Notes` 표 깨짐 | 정규화 → `core/report_markdown_normalizer.py`(Source Notes 섹션 한정, pure·idempotent). 호출 → `tools/final_report_tool/final_report_tool.py`(save 직전). 프롬프트 → `FINAL_PROMPT` |

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

### 2026-06-04 (feature+fix) — 외부 API 구조적 cleanup · Source Notes 정규화 · citation anchor 신뢰도

세 갈래(새 기능 1 + 버그 2)를 한 번에. 공통 원칙: **추가 LLM call 0, keyword/selector 기반 deterministic filter 금지**(언어·도메인 일반화 유지).

#### A. 외부 API batch cleanup = 구조적 HTML 추출 (raw pass-through 폐기)
**문제**: batch(OpenAI) 경로가 `raw_md`를 `clean_md`로 그대로 복사 → boilerplate가 batch summary·`final.md`·citation popup까지 전파. local per-doc 경로는 LLM cleanup으로 제거하므로 두 경로 source 품질이 갈렸다.

**변경 파일**
- (new) `services/document_cleanup_tool_funcs/html_body_extractor.py` — `extract_main_text(html)`. BeautifulSoup로 **HTML tag + ARIA role만** 기준으로 chrome(`nav`/`footer`/`aside`/`script`/`form`…) 제거. body 후보 `article`→`main`→`[role=main]`→`body`. heading/paragraph/list/code/table-row 보존 markdown-ish 변환. **keyword·class/id substring·site selector 일절 없음.**
- (edit) `services/document_cleanup_tool_funcs/__init__.py` — export.
- (edit) `services/run_store_tool_funcs/run_store_service.py` — `read_raw_html(doc_id)` accessor.
- (edit) `tools/document_cleanup_tool/document_cleanup_tool.py` — `_run_batch_mode`이 `_batch_clean_body()`로 raw_html 구조적 추출(≥200자일 때 채택, 아니면 raw_md fallback)을 `clean_md` + batch-metadata 입력 `bodies[]`에 사용. **local per-doc 경로(`_process_record`/`_cleanup_with_retry`) 무변경.**
- (edit) `core/prompts/cleanup.py` `BATCH_DOC_METADATA_PROMPT` + `core/prompts/autosurvey.py` `BATCH_SUMMARY_PROMPT` — "body evidence only; page chrome는 evidence 아님" 일반 원칙 추가(keyword list 아님, LLM call 수 불변).
- (edit) `tests/test_document_cleanup_modes.py` — 기존 `test_clean_md_is_raw_passthrough` → `..._falls_back_to_raw_when_no_html`로 재구성. raw_html 있을 때 구조적 추출/메타데이터 입력 sanitize 검증 + `HtmlBodyExtractorTests` 3건.

**결정**: 실패는 hard failure가 아니라 raw fallback(보수적 — body 손실보다 leftover noise를 택함). `clean_md`는 여전히 단일 downstream source(batch summary·RAG·verify·citation).

#### B2. `## Source Notes` 표 결정론적 정규화
**문제**: prompt가 표를 요구해도 LLM이 `- doc_001 | …`(bullet-prefixed), separator 누락, bare `doc_1` 등을 내보내 Markdown 표 렌더가 깨짐.

**변경 파일**
- (new) `core/report_markdown_normalizer.py` — `normalize_final_report_markdown(md)`. `## Source Notes` 섹션에만 적용(나머지 본문·수식·일반 표 무수정), pure·idempotent. canonical header+separator 강제, bullet 제거, doc id `[doc_NNN]` zero-pad 정규화, cell 줄바꿈/`|` 안전 처리, unknown `-` 보존.
- (edit) `tools/final_report_tool/final_report_tool.py` — `clean_latex_in_markdown` 이후 `save_final_report` 이전에 호출.
- (edit) `core/prompts/autosurvey.py` `FINAL_PROMPT` — Source Notes는 leading bullet 없는 single-line pipe row + separator 명시(보조 수단, normalizer가 최종 보정).
- (new) `tests/test_final_report_normalizer.py` — 8건(bullet→table, separator 삽입, `doc_1`/`doc-1`→`[doc_001]`, 비-Source-Notes 무변경, idempotent, `- None` 비-table).

#### B1. Citation anchor 신뢰도 (low-confidence 오highlight 제거)
**문제**: popup이 `final.md`(paraphrase 합성)의 같은 줄 claim을 clean_md에 즉석 매칭 → 약한 매칭도 "가장 가까운 후보"로 무관한 문장을 highlight.

**변경 파일**
- (edit) `api/services/document_citation_service.py` — `get_citation`을 **3단계 resolution**으로: direct(score≥0.50) → `_resolve_batch_anchor`(같은 doc 인용 `batch_*.md` finding 중 final claim과 최다 겹치는 것을 골라 clean_md 재탐색, anchor score≥0.45) → document_only(`match=None`). 응답에 `resolution` + `match.matchSource`("direct"/"batch_anchor") + `anchorClaim` 추가. 순수 scoring, LLM 없음.
- (edit) `frontend/ui/pages/document_page.py` `CitationPopup` — `match=None`+`document_only`면 무관한 highlight 대신 "문서 수준 근거로 연결됐지만 문장 위치 미확정" 안내. `batch_anchor`면 "가장 가까운 원문 근거 문장" 라벨.
- (edit) `core/prompts/autosurvey.py` `BATCH_SUMMARY_PROMPT`/`FINAL_PROMPT` — "각 `[doc_NNN]`는 그 문서가 그 문장의 구체 claim을 *독립적으로* 뒷받침할 때만; 여러 doc 인용 시 각자 독립 support" 추가.
- (edit) `tests/test_document_citations.py` — `test_unrelated_claim_resolves_document_only`(match=None+document_only), `..._marked_direct`, `..._uses_batch_anchor`, `..._multi_doc_each_resolves_to_own_batch_anchor` 추가.

**엔지니어링 결정(공통)**: "틀린 highlight보다 문서 수준 fallback이 낫다" — 약한 직접 매칭을 원문 위치로 가장하지 않는다. batch anchor index는 추가 LLM 없이 기존 `batch_*.md`+`clean_md`만 사용.

**테스트**: `test_document_cleanup_modes`(17) + `test_final_report_normalizer`(8) + `test_document_citations`(24) 모두 통과. 전체 `discover tests` → **267 tests, 동일 사전-존재 실패 13건만**(무관한 RAG/OpenAI 모듈), 신규 회귀 0.

### 2026-06-04 (fix) — clean_md chrome 잔존: extractor crash + 근본 원인(fetch fit/raw 가드) 튜닝

**배경(사용자 관찰)**: 조사 후 `clean_md`가 `raw_md`와 거의 동일하고 광고 배너·네비게이션·댓글 footer 등 chrome이 그대로 남음.

**진단 (실데이터 `runs/삼성전자-2` 23건)**:
1. `html_body_extractor.extract_main_text`가 실제 중첩 HTML에서 **크래시**(`<nav>` 등 decompose 후, `find_all` 리스트에 남은 파괴된 자식의 `.attrs` 접근 → `AttributeError`). `_batch_clean_body`의 try/except가 삼켜 **batch 모드에서 항상 raw_md fallback** → clean_md ≈ raw_md.
2. 더 근본: `raw_md`는 원본이 아니라 Crawl4AI pruning 결과. chrome 발원지는 fetch 단계 `crawl4ai_fetch._coerce_markdown` 가드 — `fit_markdown`이 `raw_markdown`의 **45%(`_FIT_MIN_RATIO`) 미만이면 노이즈 raw로 되돌림.** chrome-heavy 페이지일수록 pruning이 60~75%를 정상 제거 → 가드 발동 → 노이즈 채택.

**변경 파일**
- (edit) `services/document_cleanup_tool_funcs/html_body_extractor.py` — crash 수정(2-pass: read-only 수집 → decompose, `.decomposed` 가드) + 본문 선택을 "첫 `article`"에서 **구조적 density 점수**(`content_score` = text_len×(1−link_density))로 변경(teaser `<article>` 함정 해결).
- (edit) `tools/document_cleanup_tool/document_cleanup_tool.py` — `_batch_clean_body` 게이트를 절대길이 + **retention(≥50%)**로 강화: 길이로 구분 안 되는 content-loss를 막기 위해 과하게 작은 추출이면 raw_md로 fallback.
- (edit) `services/fetch_webpage_tool_funcs/crawl4ai_fetch.py` — **`_FIT_MIN_RATIO` 0.45 → 0.25** (사용자 선택한 근본 수정). de-chrome된 `fit_markdown`을 더 신뢰. `_FIT_MIN_CHARS=500` 절대 floor 유지로 과깎임(저-prose store/IR) 페이지는 raw 유지.
- (new) `tests/test_crawl4ai_fetch.py` — `_coerce_markdown` 선택 로직 6건(0.25 floor 회귀 가드 포함).
- (edit) `tools/fetch_webpage_tool/README.md` — 45%→25% 및 근거.

**엔지니어링 결정**
- *근본 원인은 fetch 레이어*: cleanup에서 raw_md를 2차 재추출하기보다, fetch가 이미 생성한 깨끗한 `fit_markdown`을 신뢰하는 편이 양 cleanup 경로(batch·local)·RAG·verify·citation을 한 번에 개선. INSTRUCTION의 "upstream extractor 개선" 노선.
- *length로 chrome 제거 vs 본문 손실을 구분 불가* → 절대 floor + 실측 군집(복구 본문 0.27~0.40 / 과깎임 0.04~0.05) 사이 간격(0.25)으로 결정. keyword·class/selector 매칭 없음(언어·도메인 일반화 유지).
- *적용 범위*: **신규 fetch부터** 적용 — 기존 워크스페이스의 `raw_md`는 불변이므로 효과를 보려면 재조사 필요.

**테스트**: `tests.test_crawl4ai_fetch`(6) + `tests.test_document_cleanup_modes`(17) 통과. 전체 `discover tests` → **273 tests, 동일 사전-존재 실패 13건만**, 신규 회귀 0.

### 2026-06-04 (fix) — Cleanup 품질 2차: block-run 추출 + quality gate + provenance (삼성전자-3 재조사)

**배경(사용자 재조사 `runs/삼성전자-3`)**: fetch 튜닝 후에도 chrome 잔존. 23건 중 structural 채택 10건, raw 동일 13건. doc 009 tail에 Tags/Recent Posts 잔존, doc 004는 좋은 추출(2,227자)이 raw 대비 14%라 `_MIN_STRUCTURAL_RETENTION=0.5` gate에 막힘, doc 011 표 중심 추출이 30%라 fallback, doc 001은 promo/IR navigation을 본문으로 오선택.

**진단**: 고정 retention gate가 noisy raw 길이를 기준 삼아 **chrome 많은 문서일수록 좋은 추출을 버리는 역설**. container 선택(article/main/body)만으론 비-semantic div 본문/관련기사 article box를 오선택. 변환된 문자열만 보면 footer link cluster가 본문처럼 보임 → DOM block 단계에서 link-density를 보존해 trimming해야 함. cleanup provenance 부재.

**변경 파일**
- (edit) `services/document_cleanup_tool_funcs/html_body_extractor.py` — **block model**(`_Block`: kind / text_len / link_len / control_count + 구조 `weight`) + **block-run window 선택**(Kadane max-weight 연속 구간 → leading nav·trailing tags/related/comment cluster 자동 제외, 중간의 짧은 본문 단락은 보존) + **quality verdict**(`extract_main_text_with_stats` → text/accepted/reason/extracted_len/prose_len/table_count/link_density). `extract_main_text(html)->str`은 compat wrapper로 유지. 전부 tag/role/구조 통계만 — keyword·class/id·selector 없음.
- (edit) `tools/document_cleanup_tool/document_cleanup_tool.py` — `_batch_clean_body`를 **고정 retention(0.5) 폐기 → quality-based gate**로. raw 대비 비율과 무관하게 prose≥400 또는 table(floor 200) + low link density면 채택, 아니면 raw fallback(reason: `too_short`/`low_quality`/`empty`/`no_html`). `_run_batch_mode` result.data에 `cleanup_provenance`(docId/accepted/reason/수치, **raw text 없음**) 추가. **local per-doc 경로 무변경.**
- (edit) `tests/test_document_cleanup_modes.py` — low-retention article 채택, nav-only reject, related-article-first body window, tail link cluster trim, table-heavy 채택, provenance(raw text 없음) 케이스 추가(총 24).

**엔지니어링 결정**
- *retention → quality*: 길이는 chrome 제거와 본문 손실을 구분하지 못한다. raw 대비 비율 대신 **본문 구조 통계**(prose/table/link-density)로 채택을 판단 → doc 004(0.09)·011(0.25) 같은 좋은 저-retention 추출을 채택하고, promo/nav(link-density↑)·thin(too_short)은 거절.
- *Kadane window*: chrome block에 음수 가중치를 줘 본문 run만 최대합으로 선택. leading nav + trailing related/tags/category/comment를 **구조만으로** 제거(키워드 불필요), 중간의 짧은 단락은 보존.
- *table 우대*: 표는 dense data라 prose-only(400)보다 낮은 floor(200) 적용 — 표 중심 출처(doc 011형)가 길이로 버려지지 않게.
- *provenance*: 채택/거절 사유 + 수치만 result.data에 기록(raw text 금지) — 어느 문서가 왜 fallback인지 리뷰/UI에서 확인 가능.
- *한계(정직)*: 개인정보 동의/약관 같은 **법적 boilerplate 산문**은 link-density가 낮아 article 산문과 구조적으로 구분 불가(doc 001은 807자 consent로 채택됨). keyword 금지 원칙상 구조만으로는 못 거른다. 단 807자 consent는 raw(25,000자 전체 chrome)보다 노이즈가 적어 downstream 영향은 오히려 감소.

**테스트**: `tests.test_document_cleanup_modes`(24) 통과. 실데이터 삼성전자-3: structural 채택 **10→20/23**, doc 004 전체 기사 복원·009/011 tail 제거 확인. 전체 `discover tests` → 신규 회귀 0(잔존 실패는 무관한 OpenAI-factory 모듈).

### 2026-06-04 (fix) — Final report JSON leakage guard (Multi_Armed_Bandit-2)

**배경(사용자 관찰)**: `runs/Multi_Armed_Bandit-2/final.md`의 `## User Request` 아래에 `json.dumps({user_request, plan, batch_summaries…})` payload가 통째로 출력됨. 원인은 `final_report_tool`이 합성 입력을 **단일 JSON blob**으로 LLM에 넘기고, `FINAL_PROMPT`가 `## User Request`에 무엇을 쓸지 제한하지 않아 모델이 입력을 보고서 내용으로 오인. 특정 모델 결함으로 치부하지 않고 **prompt/input contract에서 구조적으로 차단**.

**변경 파일**
- (edit) `tools/final_report_tool/final_report_tool.py` — 입력 조립을 `json.dumps({...})` → **`render_final_report_input()`**(pure)로 교체: Original User Request / Research Plan Summary(allowlist: topic·goal·must_cover·keywords — **search_queries·raw plan 제외**) / Run Stats / Batch Summaries를 사람이 읽는 sectioned text로 렌더(JSON 없음). 생성 후 **`repair_user_request_section_if_leaked()`** 결정론적 guard로 `## User Request`에 JSON payload(`{`/`"batch_summaries"`/`"search_queries"`/`"plan"`)가 남으면 원문 요청 blockquote로 교체. **추가 LLM call 없음**(retry 아님), guard는 해당 섹션만 수정(수식·Source Notes 등 무변경).
- (edit) `core/prompts/autosurvey.py` `FINAL_PROMPT` — 아래 입력은 internal source일 뿐 JSON/plan/search_queries/batch_summaries를 보고서로 재현 금지 + `## User Request`엔 원문 요청만 두라는 규칙 추가.
- (new) `tests/test_final_report_tool.py` — 입력에 raw JSON 없음·search_queries 제외, 누출 섹션 repair, 정상 섹션 무변경, Source Notes normalizer 비간섭, 누출 모델 출력→저장 final.md 정상 등 8건.

**엔지니어링 결정**
- *입력 계약이 1차 방어*: 모델에게 JSON을 주지 않으면 그대로 echo할 수 없다. sectioned text + 프롬프트 규칙으로 누출 자체를 차단.
- *결정론적 guard가 안전망*: 어떤 LLM이든 누출 시 `## User Request`만 원문으로 복구 — raw JSON 저장을 구조적으로 불가능하게. retry 없이 0 추가 call로 보장.
- *섹션 격리*: guard는 `## User Request`만, normalizer는 `## Source Notes`만 — 서로/본문/수식과 무간섭(테스트로 보장).

**테스트**: `tests.test_final_report_tool`(8) 통과. 실데이터 `Multi_Armed_Bandit-2/final.md`: guard가 누출 감지·`## User Request`를 원문 요청으로 복구(`"batch_summaries"` 제거) 확인. 전체 `discover tests` → 신규 회귀 0(잔존 실패는 무관한 OpenAI-factory 모듈).

### 2026-06-04 (perf) — AutoSurvey pre-fetch source scoring + citation anchor combined-score

**배경(INSTRUCTION "AutoSurvey Quality, Speed, and UX Review")**: collect 루프가 검색 결과를 순서대로 전부 fetch → 오프토픽 문서(예: 대체육 조사에 AI video / bath bomb market 리포트)가 fetch·cleanup LLM call·`maxDocs` 슬롯을 소비했다. 또 citation batch anchor가 상위 후보를 **source score 단독**으로 골라 클릭한 final claim과 무관한 source 문장을 highlight할 수 있었다.

**변경 파일**
- (new) `services/autosurvey_source_quality.py` — fetch **전** candidate scoring(pure). `build_topic_terms`(user request + plan topic/goal/must_cover/keywords + 현재 query 토큰 union, **search_queries 제외**), `score_candidate`(title/snippet/url 토큰 중 on-topic 비율 = precision — 오프토픽이 자기 주제어 때문에 generic market 단어를 공유해도 밀림), `rank_candidates`(score desc 정렬 + min_score gate + **domain diversity cap** + reference-site 예외). keyword/site/language list 없이 **구조적 토큰 overlap만**. topic 신호가 빈약하거나 전부 threshold 미만이면 **drop 없이 재정렬만** 해 수집을 starve 시키지 않는다(maxDocs·cleanup gate가 실제 backstop).
- (edit) `workflows/autosurvey_workflow.py` `run_collect` — 검색 직후 `rank_candidates`로 **kept 후보만 fetch**, dropped count/사유 로깅. `_reference_domains(plan)` 헬퍼(plan.reference_sites의 domain → gate/cap 예외).
- (edit) `api/services/document_citation_service.py` `_resolve_batch_anchor` — 상위 3개 anchor 후보를 source score가 아니라 **(source match + final-claim overlap) combined score**로 선택하고 overlap threshold 미만 후보는 제외. source는 강하지만 클릭 claim과 무관한 finding이 anchor를 가로채지 못한다.

**엔지니어링 결정**
- *성능 1차 레버는 fetch 전 filtering*: 오프토픽을 fetch 전에 떨궈 fetch·cleanup LLM·maxDocs를 절약(플랜의 "speed 개선은 먼저 fetch 전 filtering"과 일치).
- *cleanup과 책임 분리*: source quality = 조사 적합성(fetch 전), cleanup = 본문 정제(fetch 후). 서로 섞지 않음.
- *generalizable only*: cleanup 원칙과 동일하게 topic/site/keyword list 금지, 구조적 신호(토큰 overlap·domain·precision)만.

**테스트**
- (new) `tests/test_autosurvey_source_quality.py`(10): 오프토픽 < relevant 순위, clean 오프토픽 drop, domain cap(5→3 kept/2 capped), reference-site gate·cap 예외, thin-topic no-drop, search_queries 비포함.
- (edit) `tests/test_document_citations.py` — anchor가 source 강한 무관 finding(Atari) 대신 claim 관련 finding(PushT)로 resolve하는 회귀(구 source-score-only 로직에선 Atari 누출 확인).

**구현 범위(이번 increment)**: 플랜 항목 1·2·3(pre-fetch scoring·diversity·reference 예외)·8(anchor) 구현. 항목 **4**(post-fetch rejection metadata)·**5**(coverage ledger `summary/autosurvey_metrics.json`)·**6**(early stop)·**7**(fetch 동시성)·**9**(Research UI counts)와 "Demo Surprise" 플랜은 collect/cycle 루프 재구성·영속화·UI 변경이라 라이브 앱 검증 필요 → 후속 increment로 분리.

**테스트 실행**: 위 신규/회귀 통과. 전체 `discover tests` → 신규 회귀 0(잔존 3 실패는 무관한 OpenAI-factory 모듈).

### 2026-06-04 (perf) — AutoSurvey post-fetch rejection + early stop (플랜 항목 4·6)

**배경**: 위 increment(항목 1·2·3·8)에 이어, fetch 후/cycle 단계 성능 레버를 추가. (4) snippet은 그럴듯했지만 **fetched body가 주제와 거의 안 겹치는** 문서(anti-bot/redirect/엉뚱한 주제)가 cleanup LLM·`maxDocs` 슬롯을 소비. (6) core gap이 없어도 루프가 `max_docs`까지 무조건 수집.

**변경 파일**
- (edit) `services/autosurvey_source_quality.py` — `topic_hit_count`(body가 포함한 distinct topic term 수), `body_is_on_topic`(thin topic이면 절대 reject 안 함; 아니면 hit ≥ `_MIN_BODY_TOPIC_HITS=2`). pre-fetch precision과 달리 **body는 near-zero 겹침만 reject**(보수적; pre-fetch gate가 이미 명백한 오프토픽 snippet 제거).
- (edit) `services/run_store_tool_funcs/run_store_service.py` — `write_rejected_note`: rejected page를 `summary/rejected_*.md` **note로만** 기록(IndexedDocRecord·index.json 없음 → `doc_*` 번호·`maxDocs` 미소비). url/title/domain/query/reason/score만, **raw body 미저장**.
- (edit) `workflows/config.py` — `min_docs` knob(`VERITAS_MIN_DOCS`; 0이면 `max(scout_docs, round(max_docs*0.6))`로 파생, else [1,max_docs] clamp).
- (edit) `workflows/autosurvey_workflow.py` —
  - `_fetch_one(..., topic=None)`: dedup 통과 후 `body_is_on_topic` 실패 시 `write_rejected_note` + `doc_rejected` progress emit + `status="rejected"` 반환(reference-site fetch는 `topic=None`이라 면제).
  - `run_collect`: query별 `topic` 1회 생성해 pre-fetch ranking과 `_fetch_one`에 공유, `rejected_doc_ids`/`rejected_count`를 결과에 추가.
  - `_early_stop_decision`(pure staticmethod) + `run_all` 루프 삽입: `kept ≥ min_docs` 이고 (core gap 없음 → `no_core_gap` | 직전 cycle accepted ≤ `_EARLY_STOP_MIN_GAIN=1` → `low_marginal_gain`)이면 final로 break, `replan_skipped_reason="early_stop:<reason>"` 기록.

**엔지니어링 결정**
- *rejection은 note-only*: kept numbering·maxDocs 불변(중복 record가 dedup용으로 index에 남는 것과 달리 rejected는 순수 note). raw text 비저장으로 JSONL/JSON 불변 준수.
- *body gate는 보수적*: thin-topic 면제 + hit≥2로 relevant 문서 over-reject 방지. AI-video/bath-bomb류는 pre-fetch가 1차로 거름.
- *early stop은 min_docs 이후에만*: 기본 max_docs의 ~60%(floor=scout) 확보 전엔 절대 조기 종료 안 함.

**테스트**
- (new) `tests/test_autosurvey_collect.py`(8): `_early_stop_decision` 4분기, `write_rejected_note`(kept 0·raw text 없음), `_fetch_one` 오프토픽 reject(kept 미소비)·온토픽 keep·topic=None 면제(fake fetch tool).
- (edit) `tests/test_autosurvey_source_quality.py` — `body_is_on_topic`/`topic_hit_count`(relevant keep·offtopic reject·thin 면제).

**테스트 실행**: 신규/회귀 통과. config `min_docs` 파생(15→9, 5→3, scout-floor) 확인. 전체 `discover tests` → 신규 회귀 0(잔존 3 실패는 무관한 OpenAI-factory 모듈).

**잔여(후속)**: 항목 5(coverage ledger `autosurvey_metrics.json`)·7(fetch 동시성)·9(Research UI counts) + "Demo Surprise" 플랜.

### 2026-06-04 (fix) — source-quality increment 리뷰 반영 (Codex feedback 최소 패치)

**배경**: 위 source-quality/early-stop 증분에 대한 Codex 리뷰가 3가지 엔지니어링 리스크 지적.

**변경 파일**
- (edit) `workflows/autosurvey_workflow.py` — **early-stop 의미 강화**: gap이 남아 있는데 `accepted ≤ _EARLY_STOP_MIN_GAIN`이라고 **첫 cycle에 바로 멈추지 않음**. `_early_stop_decision`에 `low_gain_streak`·`queries_exhausted`(+`low_gain_patience=2`) 추가 — low-gain 종료는 **연속 low-gain(streak≥patience)** 이거나 **남은 query 소진** 시에만. `no_core_gap`(kept≥min_docs & gap 없음) 종료는 유지. `run_all`이 `low_gain_streak`를 cycle마다 누적/리셋하고 `_remaining_search_queries(active_plan)`로 소진 여부 전달.
- (edit) `workflows/autosurvey_workflow.py` `run_collect(..., user_request="")` — **원문 user request를 source scoring에 전달**. `build_topic_terms(user_request=…, plan=…, query=…)`로 호출. `run_all`의 scout·main collect가 `user_request` 전달. `main.py`의 positional `run_collect(plan)`은 기본값 `""`로 하위호환.
- (edit) `services/autosurvey_source_quality.py` — **reference 도메인 subdomain 매칭**: `_matches_reference(domain, refs)` = `domain == ref or domain.endswith("." + ref)`. pinned `samsung.com`이 `news.samsung.com`·`www.samsung.com`까지 relevance gate·domain cap에서 면제(`samsung.com.evil.com` 같은 suffix spoof는 불매칭). site allowlist 아님, 구조적 suffix.

**테스트**
- (edit) `tests/test_autosurvey_collect.py` — early-stop: 첫 low-gain+gap은 미정지, 연속 low-gain·query 소진 종료, good-gain은 query 소진에도 미정지.
- (edit) `tests/test_autosurvey_source_quality.py` — request-only term이 TopicTerms·ranking에 반영되고 `search_queries`는 여전히 제외; parent-domain reference가 subdomain을 gate·cap에서 면제; `_matches_reference` parent/subdomain 매칭·lookalike 거부.

**테스트 실행**: 위 3개 모듈 통과(51). 전체 `discover tests` → 신규 회귀 0(잔존 3 실패는 무관한 OpenAI-factory 모듈). 추가 LLM call 없음.

### 2026-06-04 (fix) — Diffusion_LM-2 리뷰: 결정론적 품질/citation 보정 (플랜 항목 A·B·C·E)

**배경**: `runs/Diffusion_LM-2` run 리뷰 — citation 미해결률 높음(82개 중 document_only 68), Source Notes row가 claim처럼 링크됨, query drift로 오프토픽 수집, 일부 `clean_md`가 embedded JSON/listing으로 raw보다 비대, final.md 말미 chat체 마무리. 이번 increment는 **결정론적·테스트 가능한 4개**만(LLM contract 변경 없는 범위).

**변경 파일**
- (A, edit) `workflows/autosurvey_workflow.py` `run_collect` — **query drift 차단**: pre-fetch ranking은 full topic(core+live query)로 하되, post-fetch body 수용 게이트(`_fetch_one`)는 **core topic(user request+plan, query 제외)** 으로. drift된 query 토큰만 겹치는 오프토픽 body가 통과하지 못함. `core_topic`은 루프 밖 1회 생성.
- (B, edit) `services/document_cleanup_tool_funcs/html_body_extractor.py` — **structured-payload 가드**: `structured_punct_ratio`(JSON/listing 구조문자 `{}[]":,` 밀도) + `is_structured_payload(text, raw_len)`(raw 대비 1.5배↑ **그리고** punct 밀도 0.12↑ — 둘 다 충족 시만). (edit) `_batch_clean_body`가 채택 직전 호출해 bloated payload면 `raw_md` fallback + provenance `reason="structured_payload"`. 정상 장문 기사(저밀도)는 raw보다 커도 미플래그. keyword/site 규칙 없음.
- (C, edit) `frontend/citation_links.py` — **Source Notes는 document-level**: `## Source Notes` 섹션(다음 heading 전까지) 내 marker는 claim 없이(`veritas-citation:doc_007`) 링크 → 표 row를 문장으로 증명하려 하지 않음. (edit) `frontend/ui/pages/document_page.py` 팝업: claim이 비면 "정확한 위치 확정 못함" 대신 **문서 출처 카드**("이 문서의 출처 정보입니다") 표시.
- (E, edit) `core/prompts/autosurvey.py` `FINAL_PROMPT` — assistant-chat 마무리("If you want…/원하시면…") 금지, forward action은 `## Remaining Gaps` 하위 "Recommended next steps"로.

**테스트**
- (A) `test_autosurvey_source_quality.py`: drift query는 full topic을 통과하지만 core gate는 거부.
- (B) `test_document_cleanup_modes.py`: prose 저밀도 / JSON 고밀도 / bloated payload 플래그 / 장문 prose 미플래그 / 소형 body 미플래그.
- (C) `test_document_citations.py`: Source Notes marker는 claim 없는 document-level href(빈 claim round-trip), 다음 섹션 marker는 다시 claim 부여.

**구현 범위/잔여**: 플랜 항목 **A·B·C·E** 구현. 항목 **D(citation evidence atoms)** — 문서 summary 출력 계약에 evidence atom(`evidence_id`/`localized_claim`/`source_quote`) 추가 → 결정론적 anchor 검증 → `summary/citation_evidence.json` sidecar → `final_citations.json` postprocessor → frontend/API가 evidence map 우선 해석 — 은 **summary LLM 계약 변경 + 신규 산출물 + frontend/API 교체**라 라이브 검증 필요한 대형 cross-cutting 작업으로 별도 increment 분리. (cross-language 미스매치·multi-citation 한 줄·batch-anchor 약함의 근본 해결은 D에 속함.) 항목 10(rejected post-cleanup 문서를 evidence에서 제외 + 대체 수집)도 D와 함께.

**테스트 실행**: 위 4개 모듈 통과(83). 전체 `discover tests` → 신규 회귀 0(잔존 3 실패는 무관한 OpenAI-factory 모듈). 추가 LLM call 없음.

### 2026-06-05 — Diffusion_LM-2 항목 D: citation evidence atoms (cross-language anchor) + CLI run_collect 수정

**배경**: 직전 increment에서 별도로 분리해 둔 **항목 D**를 구현. 근본 문제는 RAG 청킹이 아니라, final claim은 한국어 합성인데 source 문서는 영어라 클릭 시 `clean_md`에 대한 lexical 문장 매칭이 cross-language를 못 넘는 것(이 때문에 `document_only` 미해결률이 높았다). 해결의 핵심: **요약 LLM이 이미 읽은 문서에서 verbatim quote를 함께 내보내고, 클릭은 한국어 final claim ↔ 한국어 localized claim(동일 언어)으로 매칭하되 atom이 이미 검증된 원문(영어) 문장을 보유**하게 한다. **추가 LLM call 없음**(기존 per-doc summary 호출의 출력 계약만 확장).

**변경 파일**
- (new) `services/citation_match.py` — 순수 lexical 매칭 헬퍼(`normalize_text`/`tokenize`/`split_paragraphs`/`split_sentences`/`score_sentence`/`match_claim_in_source`/`claim_overlap`/`confidence_for`)를 `document_citation_service`에서 **추출(단일 출처)**. AutoSurvey 파이프라인이 `api/`를 import하지 않고 재사용하도록 `services/`에 위치. `document_citation_service`는 re-export로 기존 import 하위호환.
- (new) `services/citation_evidence.py` — `build_evidence_atoms()`(payload의 `evidence[{claim, quote}]`를 `clean_md`에 결정론적 검증 후 채택), `match_claim_to_evidence()`(클릭 claim ↔ atom `localizedClaim` 동일언어 overlap), `load_atoms_from_payload()`.
- (new) `services/final_citations.py` — `build_final_citations()`: 저장된 `final.md`의 body `[doc_NNN]` occurrence별 resolution(`evidence_anchor`/`document_only`) 맵 생성. 라이브 팝업과 **동일 matcher** 사용. 펜스/`## Source Notes`는 document-level.
- (edit) `core/prompts/autosurvey.py` — `DOC_SUMMARY_PROMPT`·`DOC_SUMMARY_REDUCE_PROMPT`에 optional `evidence[]` 계약 추가. quote는 문서 본문에서 **verbatim 복사**(번역/패러프레이즈 금지, 원문 언어), claim은 요약 언어. 빠지거나 깨져도 무해.
- (edit) `api/services/document_citation_service.py` — resolution **step 0(evidence_anchor)** 추가: `_load_evidence_atoms()`(sidecar 로더, path-traversal 가드 동일) → `match_claim_to_evidence()`. 우선순위 `evidence_anchor → direct → batch_anchor → document_only`. 순수 헬퍼는 `services/citation_match`에서 import.
- (edit) `tools/document_summarize_tool/document_summarize_tool.py` — per-doc 요약 직후 `_persist_citation_evidence()`로 atom 빌드+검증(`clean_md` 대조, raw fallback)+sidecar 저장. best-effort(예외 삼킴, 요약 실패 안 시킴).
- (edit) `tools/final_report_tool/final_report_tool.py` — `save_final_report` 직후 `_persist_final_citations()`로 `final_citations.json` 생성(best-effort).
- (edit) `services/run_store_tool_funcs/{path_manager,run_store_service}.py` — `summary/citation_evidence/<id>.json`·`summary/final_citations.json` 경로 + `write/load_citation_evidence`/`load_all_citation_evidence`/`save_final_citations`.
- (edit) `frontend/ui/pages/document_page.py` — 팝업이 `matchSource=="evidence_anchor"`를 **검증된 근거**("문서 요약 단계에서 검증된 원문 근거 문장입니다")로 렌더(batch_anchor/ low보다 강한 신뢰).
- (new) `tests/test_citation_evidence.py` — atom 빌드/검증, 미검증 quote 폐기, KO claim→EN source 해소, evidence-first `get_citation`, `final_citations`(Source Notes document-level·펜스 무시) 23 케이스.
- (edit, B) `main.py` — `--phase collect`의 `run_collect(plan)`에 `user_request=` 전달(누락 시 source-quality 게이트 약화). workflow 내부 호출은 이미 전달 중.

**엔지니어링 결정**
- *Cross-language bridge*: 클릭 해소를 final claim(KO)→atom `localizedClaim`(KO) **동일언어** 매칭으로 옮기고, atom은 검증된 원문(EN) 문장·offset을 보유. direct(KO→EN)·batch-anchor 약점을 우회. (multi-citation 한 줄도 같은 줄 claim을 각 doc의 atom에 매칭 → doc별로 다른 atom으로 해소.)
- *결정론적 검증*: LLM verbatim quote를 `clean_md`(팝업이 읽는 동일 source)에 `match_claim_in_source`로 대조, `score≥0.5`만 채택. 못 찾은 key point는 요약 텍스트로는 남되 clickable exact citation은 아님.
- *No keyword/sentinel*: 전부 연속 점수 임계값(검증 0.5 / claim overlap 0.4)·구조적 토크나이즈만. keyword 사전·site·언어 special-case·sentinel 값 **없음**(기존 `_DIRECT_STRONG_SCORE` 등과 동일 성격).
- *프롬프트 안정성*: `BATCH_SUMMARY_PROMPT`/`FINAL_PROMPT`에 `evidence_id`를 **주입하지 않음**. occurrence→atom 연결은 클릭/후처리의 claim↔localizedClaim 매칭으로 처리해 batch/final 출력 형식을 건드리지 않음.
- *Graceful fallback*: sidecar 없거나 atom 0개면 step 0는 `None`→기존 direct/batch/document_only로 그대로. 기존 21 케이스 회귀 0.
- *영속화/privacy*: sidecar는 bounded snippet(text≤500 / paragraphText≤700)·offset만, raw body 미저장.
- *레이어 경계*: 순수 헬퍼를 `services/`로 이동해 `tools/`가 `api/`를 import하지 않음. `document_citation_service`는 re-export로 하위호환.

**테스트 실행**: `test_citation_evidence`(23)+`test_document_citations`(21) 통과, 인접(`final_report_tool`/`final_report_normalizer`/`document_cleanup_modes`/`autosurvey_collect`/`autosurvey_source_quality`) 72 통과, 전체 `discover tests` **385 OK**(신규 회귀 0). 추가 LLM call 없음.

**잔여**: 항목 10(post-cleanup에서 비대/구조-payload `clean_md`를 evidence·요약에서 제외 + 대체 수집)은 본 increment 범위 밖(별도). batch/final이 atom을 직접 인용하도록 강제하는 변경도 의도적으로 하지 않음(프롬프트 안정성 우선).

### 2026-06-05 — DRB AutoSurvey 벤치마크 하니스 (AutoSurvey vs flat baseline)

**기능**: AutoSurvey의 iterative design이 일반 Flat-LLM(web_search+fetch+1-shot) 대비 조사 품질을 정량적으로 얼마나 높이는지 [DeepResearch Bench](https://github.com/Ayanami0730/deep_research_bench)(RACE/FACT)로 측정하는 **평가 전용 하니스**. 동일 generator·동일 search/fetch·동일 문서 budget·동일 citation 형식으로 두 시스템을 비교한다. 프로덕션 AutoSurvey 알고리즘은 변경하지 않는다.

**변경 파일**
- (vendored) `deep_research_bench/` — 외부 평가자 트리(임시 vendored). DRB 스크립트(`deepresearch_bench_race.py`, `utils/*`)는 그대로 사용하며 하니스가 그 internal을 import하지 않는다.
- (new) `benchmarks/drb/drb_vendor.py` — DRB root 해석/검증, official raw 경로(`data/test_data/raw_data/<model>.jsonl`) 빌드, traversal·model_name 거부. 순수 path 로직.
- (new) `benchmarks/drb/drb_io.py` — `json.JSONDecoder().raw_decode` 기반 robust object iterator(JSONL/concatenated/embedded-newline 허용), `query.jsonl` 로드+필터(limit/task_ids/languages/topics), **official writer는 `id`/`prompt`/`article`만** 기록, `.meta.jsonl` sidecar, completed-id resume.
- (new) `benchmarks/drb/citation_adapter.py` — workspace `final.md`→DRB article: `[doc_NNN]`/bare 마커를 first-appearance 순 numeric `[n]`으로 renumber, `summary/index.json`(final_url>url)로 `## References` 생성, **`final.md` 불변**, code fence/inline code/링크/URL 보존, unmapped doc id는 warning.
- (new) `core/prompts/drb_benchmark.py` — flat baseline의 query-plan·final-report 프롬프트(동일 언어, numeric `[n]`, URL References, invented URL·tool narration 금지). 프로덕션 `core/prompts/__init__` re-export에는 의도적으로 미포함.
- (new) `benchmarks/drb/flat_agent.py` — flat 오케스트레이션(주입된 query/search/fetch/report callable): ≤N query → search → URL dedupe → ≤max_docs fetch → numeric source packet → 1회 report. References는 실제 fetched source로 결정론 생성(모델이 쓴 References는 교체 → URL fabrication 방지). **AutoSurvey orchestration/tool import 없음**(정적 테스트로 강제).
- (new) `benchmarks/drb/veritas_runner.py` — `main.py`처럼 AutoSurvey 직결(`LLMClient`→`build_autosurvey_llm`→`build_registry`→`AutoSurveyConfig.from_env`→`AutoSurveyWorkflow.run_all`), task당 `runs/drb/<model>/task_<id>/` 워크스페이스, `citation_adapter`로 article 추출. chat-facing `AutoSurveyTool` 미사용, memory/RAG/screen/local-private/proactive 미주입(`enable_screen_context=False`).
- (new) `benchmarks/drb/flat_runner.py` — flat_runner: 동일 generator(`build_autosurvey_llm`) + 동일 `WebSearchTool` + 동일 `fetch_with_crawl4ai`(동일 `--fetch-max-chars`).
- (new) `benchmarks/drb/validate_raw_data.py` — official key only/non-empty article/inline `[n]`/URL-bearing `## References` 검증 CLI.
- (new) `benchmarks/drb/analyze_results.py` — RACE per-task(`raw_results.jsonl`)+aggregate(`race_result.txt`), FACT(`fact_result.txt`) 파싱 → `bench_results/drb/<comparison>/`(`summary.csv`/`paired_deltas.csv`/`comparison_report.md`), mean/median delta·win rate·**고정 seed bootstrap 95% CI**, per-task 부재 시 aggregate-only fallback.
- (new) `benchmarks/{__init__,drb/__init__,drb/README.md}` — 패키지 + 디렉터리 README(실행 명령·budget/judge 정책·fairness·미실행 항목).
- (edit) `.gitignore` — DRB 생성 산출물만 ignore(벤더 트리 자체는 추적): `raw_data/{veritas,flat}_*.jsonl`·`*.meta.jsonl`·`results/{race,fact}/{veritas,flat}_*/`·`/runs/drb/`·`/bench_results/drb/`·`/benchmarks/drb/{out,cache}/`.

**엔지니어링 결정**
- *공정성*: 두 arm 모두 동일 generator/search/fetch/budget/citation 형식. flat은 source-quality gate·cleanup·batch·gap·replan·RAG·final normalizer 전부 없음 → iterative design 자체의 효과만 분리.
- *official 형식 격리*: raw 행은 `id`/`prompt`/`article`만(메타 누출 차단), 모든 run 메타는 `.meta.jsonl` sidecar(키·full body 미저장).
- *결정론 + no-LLM 분석*: stdlib만(bootstrap은 `random.Random(seed)`), per-task 부재 시 aggregate-only로 정직하게 degrade. judge 비용 라벨(`budget_judge`/`official_judge_confirmation`)은 사용자가 명시, leaderboard score와 혼동 금지.
- *레이어 격리*: 하니스는 `benchmarks/drb/`에 고립, DRB 평가자 internal 미import. flat_agent는 주입 callable로 테스트(네트워크/LLM 없음).

**테스트**: `test_drb_{vendor_layout,benchmark_io,citation_adapter,flat_baseline,analysis}` 5개 모듈 **47 passed**(전부 fake, 네트워크/LLM/judge 미접속). `git check-ignore`로 생성 산출물 ignore 확인. 전체 `discover tests` **432 OK**(신규 회귀 0).

**미실행(예산/서버 필요, 본 increment 범위 밖)**: 실제 article 생성(2-task smoke 포함, llama-server 필요), DRB RACE/FACT 공식 평가, budget-judge pilot, official confirmation, 100-task full judging — 전부 **미실행**. 명령은 `benchmarks/drb/README.md`에 문서화.

### 2026-06-09 (fix) — collect 정합성: entity anchor 게이트 + homepage-root 필터 + batch 노트 가이드

**배경**: 데모 직전 테스트에서 3가지 결함. (1) 배치 요약이 빈약(Repeated Findings·Reliability Notes가 "없음")하고, (2) "삼성전자 4분기 실적" 조사에 **쿠팡·에이블리 등 다른 기업** 문서가 수집·요약되며, (3) 사이트 **메인 페이지(루트 URL)** 가 그대로 fetch·summarize됨. 근본 원인: post-fetch 수용 게이트(`body_is_on_topic`)와 pre-fetch ranking이 **토픽 토큰 겹침 "개수/비율"** 만 보므로, 같은 종류(실적)의 일반 재무어휘(`4분기`/`실적`/`매출`/`영업이익`)만으로 다른 기업 문서가 `_MIN_BODY_TOPIC_HITS=2`를 통과 — **핵심 named entity(삼성전자)의 존재를 어느 게이트도 요구하지 않음**. (1)은 (2)의 증상(서로 다른 기업 3문서라 repeated/cross-source 신호가 구조적으로 비어 있음).

**변경 파일**
- (edit) `services/autosurvey_source_quality.py` — **entity anchor 게이트(post-fetch)**: `TopicTerms`에 `anchors`(required-presence 집합) 추가, `build_topic_terms(..., anchor_terms=())`. `body_has_anchor()`/`_anchor_present()`(한글·길이≥4는 substring — 조사 `삼성전자의`/`삼성전자가`까지 robust, 짧은 Latin은 word-boundary), `_normalize_anchor_terms()`(빈/1글자/순수 숫자 제거 — 연도 `2024`가 anchor 되어 게이트 무력화되는 것 차단). `body_is_on_topic`은 anchor가 있을 때 **본문에 최소 1개 anchor 포함**을 추가 조건으로 요구. anchor 비면(개념형 요청) 기존 count 게이트 그대로(opt-in, 굶김 없음). **homepage-root 필터(pre-fetch)**: `is_homepage_root(url)`(netloc 있고 path가 `""`/`"/"`, query 없음 — 구조적 URL shape만) → `rank_candidates`에서 reference 다음·relevance 앞에 `reason="homepage_root"` drop. reference 도메인은 면제(루트를 일부러 pin한 것).
- (edit) `workflows/autosurvey_workflow.py` — `run_collect(..., anchor_terms=None)` 추가, core/full topic 모두 anchor 전달. `_anchor_terms_from_store()`(grounding의 `candidate_entities` 로드, 없으면 빈 리스트). `run_all`은 in-memory grounding에서 `anchor_entities`를 1회 추출해 scout·main collect에 전달(매 cycle 디스크 재독 회피). 키워드/하드코딩 없음 — anchor는 사용자 요청에서 LLM이 뽑은 entity.
- (edit) `core/prompts/autosurvey.py` — `TERM_GROUNDING_PROMPT`: `candidate_entities`를 "연구가 반드시 다뤄야 하는 핵심 named subject(회사/제품/인물/모델/데이터셋)"로 재정의(모호하지 않아도 주 subject 포함; `삼성전자` 같은 것이 비지 않게). 일반 descriptor(`실적`/`매출`/`market` 등)는 `grounded_terms`로. `BATCH_SUMMARY_PROMPT`: Repeated Findings(배치 내 2+ 문서 독립 지지, 공유 사실 없으면 정직하게 비움)·Reliability Notes(문서 자체의 source-quality 신호: 발행처 권위, 보고 기간/최신성, 공식/감사 vs 자체보고·잠정·추정·홍보, 방법론/표본 caveat, 소스 간 같은 지표 불일치) 정의 1줄씩 보강(키워드 사전 아님, 구조적 정의).

**테스트**
- (edit) `tests/test_autosurvey_source_quality.py` — `EntityAnchorTests`(쿠팡 본문이 옛 count 게이트는 통과하지만 anchor 게이트는 거부 / 삼성 본문 keep / 조사 robust / anchor 없으면 게이트 무력화 / 순수 연도 anchor 불가 / 짧은 Latin word-boundary), `HomepageRootTests`(루트=homepage·검색페이지·실경로 구분 / on-topic homepage도 pre-fetch drop / reference 루트는 면제).
- (edit) `tests/test_autosurvey_collect.py` — `EntityAnchorFetchTests`: `_fetch_one`이 anchor 있는 topic에서 다른 기업 본문을 rejected(슬롯 미소비), 삼성 본문은 fetched.
- (edit) `tests/test_document_summarize_context_budget.py` — batch trim 테스트의 `n_ctx`를 5000→6000(char=token 모델에서 약간 길어진 system 프롬프트 수용; 문서 16k는 여전히 trim 강제 — 의도 보존).

**엔지니어링 결정**
- *anchor는 본문에만(pre-fetch 미적용)*: 스니펫은 짧아 relevant 결과도 entity가 안 보일 수 있음 → pre-fetch hard-drop은 위험. 본문은 텍스트가 풍부해 entity 유무가 robust. 비용은 오프토픽 1회 fetch뿐(rejected는 슬롯·cleanup·batch 미포함)이라 사용자 가시 버그(배치에 쿠팡 등장)는 완전 차단.
- *opt-in*: anchor 비면(`대체육 시장` 같은 개념형) 기존 동작 그대로 → 기존 회귀 0. 게이트는 grounding이 entity를 줄 때만 발동.
- *적용 시점*: `run_all`이 `run_term_grounding(force=True)`로 매 신규 조사마다 재-grounding → 새 프롬프트로 `candidate_entities` 채워짐. **기존 워크스페이스의 stale `grounding.json`(구 프롬프트, candidate_entities 비었을 수 있음)은 재조사 전까지 게이트 미발동** — 데모는 새 조사로 시작.

**테스트 실행**: `test_autosurvey_source_quality`·`test_autosurvey_collect`(40)·`test_document_summarize_context_budget`(3)·`test_autosurvey_memory_brief`(4) 통과. 전체 `discover tests` → **신규 회귀 0**(이 시점의 잔존 2 실패 `test_rag_grounding`·`test_chat_conversation_lock`은 본 변경과 무관한 `ChatAgent._ask_rag_iter_unlocked` 누락 — clean tree에서도 동일 재현, 아래 항목에서 해결). 추가 LLM call 없음.

### 2026-06-09 (fix) — 스트리밍 `/rag` 슬래시 크래시(`_ask_rag_iter_unlocked` 누락)

**배경**: `ChatAgent.ask_auto_iter`(스트리밍 일반 채팅)의 `/rag` 분기가 정의되지 않은 `self._ask_rag_iter_unlocked(...)`를 호출(line 356) → research/auto 모드에서 `/rag <질문>`을 직접 타이핑하면 `AttributeError`. 비스트리밍 `ask_auto`는 존재하는 `_ask_rag_unlocked`를 호출해 정상이라 **iter 변형만 비대칭으로 깨져 있던 latent bug**. 도달 조건이 좁아(모드≠rag + 명시적 `/rag` 슬래시 + 스트리밍) 일반 RAG 데모(RAG 모드→`ask_rag_iter`)에선 안 밟힘.

**변경 파일**
- (edit) `agent/chat_agent.py` — `ask_rag_iter`의 lock 안쪽 코어(table_query 선처리 → `rag_service.iter_answer` 스트리밍 → 구버전 `AttributeError` 1-shot fallback)를 **`_ask_rag_iter_unlocked` 제너레이터로 추출**. `ask_rag_iter`는 `_conversation_lock`만 잡고 `yield from`으로 위임(반복 동안 lock 유지 — 기존 의미 보존). `ask_auto_iter`의 `/rag` 분기(이미 lock 보유)는 이 lock-free 코어를 직접 iterate해 self-deadlock 회피. `ask_rag`↔`_ask_rag_unlocked` 패턴의 스트리밍 쌍둥이. 동작 변경 없음(순수 extract-method).

**테스트 실행**: `test_chat_conversation_lock`(4: `ask_auto_iter` `/rag` 데드락/크래시 회귀 포함)·`test_rag_grounding`(22, slash→strict 라우팅 포함) → **26 OK**. 추가 LLM call 없음.

### 2026-06-09 (fix) — 네이티브 ghost를 paint 오버레이 → **인라인 grey 삽입**으로 (type-along 폐지 + 문단 중간 reflow)

**배경(사용자)**: (1) 받아쓰기(type-along)식 — 사용자가 제안을 그대로 타이핑해 수락하는 — 방식 폐지, **TAB(수락)/ESC(거절)/다시쓰기만** 유지. (2) 문단 **중간**에서 제안 시, paint 오버레이는 문서를 reflow하지 못해 캐럿 뒤 검은 텍스트가 안 밀리고 회색 제안과 **겹침**. 근본 원인: 6/8 increment들이 IME 충돌 회피를 위해 ghost를 문서에 넣지 않고 `paintEvent`로 캐럿 뒤에 그리고 reflow는 **문서 끝 빈 줄 예약**으로 흉내냈는데, end-예약은 캐럿 줄의 우측 텍스트를 밀 수 없다(겹침의 구조적 원인). type-along을 폐지하면 "타이핑이 ghost와 충돌" 문제 자체가 사라지므로 오버레이의 존재 이유가 없어지고, **인라인 삽입**이 reflow를 공짜로 해결한다.

**변경 파일**
- (rewrite) `frontend/ui/windows/editor_window.py:MarkdownSourceEdit` — 제안을 **캐럿에 grey `QTextCharFormat` span으로 실제 삽입**(`_insert_ghost_span`) → 뒤 텍스트가 자연 reflow(밀림). 캐럿은 span 앞(`_ghost_start`)에 주차. **type-along 전면 제거**(`_evaluate_typealong`/`_end_typealong`/`_ghost_remaining`/`_paint_ghost`/`_set_reservation`/EOF 예약 전부 삭제). TAB=`accept_ghost`(grey span을 normal 포맷으로 재삽입 → 깔끔한 단일 undoable 삽입), ESC/클릭/IME/그 외 키=`_dismiss_ghost`(span 삭제 + `ghostDismissed`), 다시=`_request_retry`. 거절 키는 dismiss 후 `super()`로 통과시켜 입력을 삼키지 않음. 스트리밍은 문서를 토큰마다 안 흔들도록 **완성 시 1회 삽입**(그 전엔 "작성 중" 칩만). `document_text()`는 미수락 span을 strip(미리보기/저장/카운트/다음 제안 prefix가 안 봄 — 옛 예약-strip과 동일 계약). ghost 편집은 signals 차단(수락만 실제 `textChanged`). 소스 편집기엔 `QSyntaxHighlighter`가 없어 grey 포맷이 덮이지 않음(확인).
- (new) `tests/test_editor_ghost.py` — offscreen Qt 위젯 테스트 8: 최종 삽입 후 `toPlainText`엔 있고 `document_text`엔 없음, **문단 중간 push(head+ghost+tail 순서)**, accept 영속, reject/다시 제거 + 텍스트 보고, 스트리밍은 final까지 문서 불변, clear는 무신호, type-along 없음.

**검증**: `test_editor_ghost`(8) offscreen 통과. `MarkdownSourceEdit` import OK, dangling 옛-속성 참조 0(grep). `py_compile` 통과. (CLAUDE.md/§Proactive의 "native는 next_sentence ghost만 렌더"는 불변 — inline_diff/marker 렌더러는 여전히 미구현이고, 수정계열 task는 그대로 외부 카드로만 렌더.)

### 2026-06-09 (fix) — 네이티브 reject 사다리가 긴 문서에서 문서 전체를 얼리는 문제 (clamped cursor)

**배경(사용자)**: ghostwriting 3회 거절 시 그 anchor에 180s lock이 걸리는데, **완전히 다른 커서 위치(다른 문단/문장)에서도** 제안이 안 뜸. 근본 원인: `/editor/suggest`가 reject 사다리에 넘기는 커서가 `len(prefix)`인데, prefix는 프론트에서 `[-1500:]`·백엔드에서 `[-2000:]`로 잘려 — **캐럿 앞 텍스트가 prefix cap을 넘으면 모든 깊은 위치의 cursor가 동일 값(예: 1500)으로 뭉개짐**. 사다리의 proximity 매칭(`abs(stored-cur) ≤ 24`)이 서로 다른 문단을 같은 지점으로 보고, 한 곳의 cooldown이 문서 전체를 막음. (사다리 로직 자체는 정상 — 테스트가 cursor를 직접 주입해 클램핑을 우회했었음.)

**변경 파일** (윈도우 커서/글로벌 커서 분리: `observation.cursor_index`=feature용 윈도우 상대값 유지, 신규 `doc_cursor`=글로벌 offset로 사다리/anchor_id만 키잉)
- (edit) `frontend/ui/windows/editor_window.py` `_fire_suggestion` + `frontend/controllers/editor_stream.py:EditorSuggestWorker` — 진짜 캐럿 offset `pos`를 `cursor`로 전송.
- (edit) `api/api_models.py` — `EditorSuggestRequest.cursor`(true offset), `ProactiveObserveRequest.documentCursor` 추가.
- (edit) `api/api_routes/editor.py` + `api/services/editor_service.py:suggest_stream(document_cursor=)` — `cursor=len(prefix)`(feature)와 별도로 `documentCursor`=진짜 offset 전달(미전달 시 `len(prefix)` fallback).
- (edit) `api/services/proactive_service.py` + `services/proactive/models.py:ProactiveObservation.doc_cursor` — 흘려보냄.
- (edit) `services/proactive/orchestrator.py:_extract_anchor` — anchor의 `cursor_index`를 `doc_cursor`(있으면) 우선으로 설정. anchor_id 버킷(`//80`)·proximity 사다리가 글로벌 위치로 동작. feature가 읽는 `observation.cursor_index`는 무영향.
- (edit) `services/proactive/README.md` §3 — proximity가 동작하려면 커서가 문서 전체 기준이어야 함을 명시.

**테스트**: `tests/test_proactive_api.py` +3 — `doc_cursor`가 anchor를 글로벌 offset으로 만듦 / 미전달 시 윈도우 cursor fallback / **3-reject cooldown이 먼 위치로 누수되지 않음**(회귀). 기존 사다리 테스트(jitter 유지·이동 시 해제·누적) 전부 통과.

**검증**: 세션에서 건드린 12개 스위트(autosurvey·rag·conversation_lock·proactive 전체·ui_automation·editor_ghost) **132 OK**, 신규 회귀 0. 추가 LLM call 없음.

### 2026-06-09 (fix) — 검증 신뢰도 과도-"낮음" 완화 (request_alignment 소프트 오버라이드 + 루브릭 재정의)

**배경(사용자)**: 한 워크스페이스(`runs/AI_Agent_산업`)에서 18개 문서 전부 "낮음"으로 분류. 원인: `_derive_level`이 `request_alignment=="weak"`이면 다른 신호와 무관하게 **무조건 "low"**(하드 오버라이드)인데, 0.8B judge가 18개 전부 alignment=weak으로 과판정(rationale은 "on-topic이지만 요청을 완벽히 답하진 않음" — 즉 mixed여야 할 것을 weak으로) → 전부 강등. 가장 판정이 어려운 신호에 절대 거부권을 준 설계가 약한 모델의 과판정을 증폭, 게다가 상류 수집 on-topic 게이트와 모순(이미 on-topic 통과분을 100% off-topic 처리).

**변경 파일**
- (edit) `services/verification/reliability/llm_judge.py` `_derive_level` — **소프트 오버라이드**: weak alignment는 등급을 **최대 medium으로 cap**하고, `{authority, verifiability, self_consistency}` 중 **하나 이상도 weak일 때만 "low"**(off-topic *이면서* 저품질인 출처). 단일 weak-alignment 신호가 신뢰도 높은 출처를 low로 못 만들게. `_verdicts_from_response`의 자동-조정 note도 실제 결과(medium=등급 조정 / low=등급 하향)에 맞춰 문구 수정.
- (edit) `core/prompts/verify.py` `RELIABILITY_JUDGE_PROMPT` — `request_alignment`를 "요청을 완벽히 답하나"가 아니라 **"같은 주제/도메인인가"**(topical match)로 재정의. 수집 게이트를 통과한 문서는 **기본 최소 mixed**, weak은 "명백히 다른 분야/오수집"에만. 좁거나 일부만 다룬 문서는 mixed. 조합 규칙도 소프트 오버라이드와 일치하도록 갱신. 모듈 docstring도 SOFT로 갱신.

**테스트**: (new) `tests/test_verify_reliability.py` (9) — lone weak alignment→medium(핵심 회귀), weak+1weak→low, high는 2 strong & 0 weak 필요, on-topic이라도 2 weak supports면 low, llm_level 무시 등.

### 2026-06-09 (feat) — editor 이어쓰기에 문서 구조 주입 + 상대 관련성 grounding 게이트

**배경(사용자/실무 평가)**: ghost 이어쓰기가 (1) 원문 끝 ~2000자 슬라이딩 창만 봐서 섹션 주제/논지를 못 따라가고, (2) grounding을 유사도 하한 없이 top-K 무차별 주입해 약한 모델이 무관 청크에 끌려감. → 사람이 "섹션 맥락에 맞춰" 잇는 방식과 괴리.

**변경 파일**
- (edit) `agent/chat_agent.py` — **상대 관련성 게이트** `_filter_by_relative_distance`(best 대비 `_EDITOR_RAG_REL_MARGIN=0.08` 이내 청크만, 최대 `_EDITOR_RAG_TOP_K=3`; 절대 cosine 하한이 불안정하므로 best 주변 tight cluster만 유지)를 `_editor_context`에 적용. **구조 헬퍼** `_nearest_heading`/`_current_paragraph`. `iter_ghostwrite(..., section_heading="")`: 섹션 제목(호출자 제공 또는 prefix에서 추출)을 `[현재 섹션 제목]`으로 주입, 검색 쿼리를 raw 꼬리 대신 **섹션 제목+현재 문단**으로.
- (edit) `core/prompts/editor.py` — `SUGGEST_SECTION_BLOCK_TEMPLATE` 신설 + `SUGGEST_USER_TEMPLATE`에 `{section_block}` + `SUGGEST_SYSTEM_PROMPT`에 "섹션 주제 범위 안에서 이어쓰라" 1문장(`머리표지`/`첫 번째로` 등 기존 계약 유지). `core/prompts/__init__` re-export 추가.
- (edit) **섹션 제목 배선** (cursor 배선과 동일 패턴): `frontend/ui/windows/editor_window.py`(`_section_heading_before`로 전체 pre-cursor 텍스트에서 추출해 전송) → `frontend/controllers/editor_stream.py`(`sectionHeading`) → `api/api_models.py`(`EditorSuggestRequest.sectionHeading`) → `api/api_routes/editor.py` → `api/services/editor_service.py`(observe `metadata.section_heading`) → `services/proactive/generator.py`(observation에서 읽어 ghostwrite로) → `api/services/agent_runtime.py:ghostwrite_iter`(전달). 긴 문서에서 제목이 prefix 창 밖이어도 유지됨.

**테스트**: (new) `tests/test_editor_ghost_grounding.py` (11) — 상대 거리 게이트(꼬리 drop·top_k cap·distance 없음 fallback·빈 입력), `_nearest_heading`/`_current_paragraph`, `iter_ghostwrite`가 섹션 블록 주입(호출자 제공/ prefix fallback/ 제목 없으면 미주입). (edit) `tests/test_proactive_api.py` fake ghost 2곳에 `section_heading=` kwarg.

**검증**: 직접 관련 스위트(proactive 전체·rag_grounding·editor_ghost·editor_ghost_grounding·verify_reliability·ui_automation) **101 OK**(2회 반복 안정). 세션 전체 14개 스위트 **152 OK**. (대규모 혼합 실행 1회에서 `test_proactive_adaptation`의 1건이 ERROR였으나 재현 안 됨 — orchestrator background monitor 스레드의 비결정적 타이밍에 기인한 **기존 테스트 격리 flake**로, 단독·소규모 조합·재실행 시 전부 통과. 본 변경과 무관.) 추가 LLM call 없음.

### 2026-06-09 (fix) — 조사 진행 타일 "18 / 15건" (분모가 수집수보다 작게 표시)

**배경(사용자)**: 15개 이상(18·20 등) 요청 시 "수집된 문서 수" 타일이 `수집수 / 15`처럼 **분모가 수집수보다 작게** 표시. 원인: 분모 `_max_docs`는 research 페이지의 로컬 값으로 (a) 실행 시작 시 stepper 값, (b) `_render_result`에서 `response.maxDocs`로만 갱신됨(기본 15). 워크스페이스를 **새 페이지에서 다시 열람/복원**할 때 복원 job 레코드에 `maxDocs`가 없거나 stale이면 기본 15에 머문 채, 실제 수집 문서(예: 18개)가 reconcile돼 "18/15". 분자(18)는 실재 kept 수로 정확하고 분모만 stale(수집 상한은 수집수보다 작을 수 없음). 진행률(`total=_max_docs`)도 같은 stale 값을 써서 >100%를 clamp로 가리는 부작용.

**변경 파일**
- (edit) `frontend/ui/pages/research_page.py` — 순수 헬퍼 `_format_doc_count(collected, target)`: 분모를 **`max(target, collected)`로 clamp**(수집 상한은 수집수 미만일 수 없음). 요청값이 더 크면(early-stop으로 "18/20") 그대로 보존. `_doc_count_text`가 이를 사용. `_render_result`는 reconcile 후 `self._max_docs = max(self._max_docs, len(self._doc_bars))`로 저장값도 현실과 일치시켜 진행률 total도 정상화.
- (new) `tests/test_research_doc_count.py` (5, offscreen Qt) — stale 낮은 target clamp("18/15"→"18/18"), 더 큰 요청 cap 보존("18/20"), exact, in-progress, 0건.

**검증**: 5개 통과. `py_compile` 통과. 격리된 표시 로직 변경(다른 스위트 무영향).

### 2026-06-05 — DRB FACT를 crawl4ai 스크랩으로 (Jina 키 제거) + BOM 견고성

**기능**: DRB FACT 파이프라인(`extract→deduplicate→scrape→validate→stat`)에서 **Jina(`JINA_API_KEY`)가 필요한 scrape 단계만** Veritas의 `fetch_with_crawl4ai`로 대체. FACT를 **Jina 키·비용 없이** 실행 가능. 인용 URL이 어차피 crawl4ai로 수집된 것이라 재스크랩이 내부적으로 일관되고, 양쪽 시스템에 동일 적용되어 A/B delta는 공정(단 비공식 → `fact_crawl4ai_budget` 라벨).

**변경 파일**
- (new) `benchmarks/drb/crawl4ai_scrape.py` — `utils.scrape` drop-in. 입력 `deduplicated.jsonl`의 각 `citations_deduped[url]['url_content']`를 crawl4ai 페치 텍스트(`"<title>\n\n<content>"`, 실패 시 `"scrape failed: …"` 센티넬)로 채워 `scraped.jsonl`로 출력 → `utils.validate`가 그대로 읽음. resume(완료 id 스킵), URL 미충족분만 스크랩, ThreadPool 동시성. 평가자 트리 **무수정**(scrape 명령만 교체). 어디서 실행하든 import되도록 repo 루트를 `sys.path`에 자가 부트스트랩 + `force_utf8_stdio`.
- (edit) `benchmarks/drb/drb_io.py` — `iter_json_objects`가 **선행 UTF-8 BOM**(`Set-Content -Encoding utf8` 등이 붙임)을 strip(`_BOM = chr(0xFEFF)`). BOM 때문에 첫 레코드가 누락되던 실버그 수정.
- (new) `tests/test_drb_crawl4ai_scrape.py` — fake fetch로 url_content 빌드/실패 센티넬/미충족 URL만 스크랩/resume 검증(10). (edit) `tests/test_drb_benchmark_io.py` — 선행 BOM이 첫 레코드를 안 먹는지 회귀.
- (edit) `benchmarks/drb/{README.md,RUN.md}` — FACT를 crawl4ai 경로로(Jina 불필요), `& $py`(직접 경로) 권장 — `conda run`이 한국어 Windows에서 자식 비-ASCII 출력 재출력 시 cp949 `UnicodeEncodeError`로 죽는 이슈 회피.

**검증(실제 실행)**: 6개 DRB 모듈 **58 passed**. **라이브 스모크**: 합성 `deduplicated.jsonl`(BOM 포함) → `crawl4ai_scrape`가 실제 인터넷에서 2개 URL 스크랩 성공(`url_content` 채워짐) — llama-server 없이 FACT scrape 단계 단독 동작 확인. 추가 LLM/네트워크는 단위테스트에서 미사용(전부 fake).

**주의/한계**: crawl4ai는 HTTP-only(JS/anti-bot 약함). 이 FACT 변형은 **비공식**(leaderboard·타 연구 FACT와 직접 비교 불가) — `fact_crawl4ai_budget`로 라벨, 양쪽 동일 scraper 적용. 공식 Jina 경로는 `utils.scrape` + `JINA_API_KEY`로 그대로 사용 가능(RUN.md 옵션).

### 2026-06-08 — 실시간 보조 4종 완결성 보강 + 로컬 표 RAG 단일라운드 경로

**배경(사용자 검증 요청)**: 구현돼 있으나 온전치 않은 4개 지점 — (1-1) native ghostwriting 문장 중간 끊김, (1-2) 거절 횟수별 context 확장 미작동, (1-3) 3-reject cooldown이 cursor 한 칸만 움직여도 우회됨, (1-4) 로컬 .csv/.xlsx 수치 값의 채팅 RAG 질의응답.

**변경 파일**
- (edit) `services/proactive/generator.py` — ghost 토큰 cap `64 → DEFAULT_GHOST_MAX_TOKENS=192`. 64토큰은 한국어 1문장도 빠듯해 `SUGGEST_SYSTEM_PROMPT`가 요구하는 1~2문장이 EOS 전에 length-cap으로 잘림. (edit) `api/services/agent_runtime.py` — `VERITAS_PROACTIVE_GHOST_MAX_TOKENS` env override 배선. (edit) `core/prompts/editor.py` — "문장을 중간에 끊지 말고 완결" 지시 보강. `iter_ghostwrite`의 `min(256,…)` clamp 안.
- (edit) `services/proactive/orchestrator.py` — **(1-3)** native reject ladder를 anchor_id 정확일치 → **cursor proximity 매칭**(`NATIVE_ANCHOR_PROXIMITY_CHARS=120`, env override)으로. `_AnchorRejectState`에 `document_id`/`cursor_index` 저장, `_match_state_key_locked`/`_read_anchor_state_for`/`_state_for_anchor_locked` 추가. raw `anchor_id`는 단락 텍스트 해시를 포함해 스페이스 한 번에 새 id가 생겨 cooldown을 우회하고 ladder 누적을 깨던 근본 원인을 해소. **(1-2)** observe가 `last_rejected_text`를 `reject_level`과 무관하게 forward(이전엔 `reject_level>0` 가드에 막혀 "다시"의 avoid 텍스트가 누락). ladder 메서드들이 `anchor_id` 문자열 대신 `ActiveAnchor`를 받도록 시그니처 변경(`_read_anchor_state(anchor_id)` 정확일치 accessor는 테스트/진단용으로 유지).
- (edit) `frontend/ui/windows/editor_window.py` — **(1-2)** `ghostDismissed = Signal(str)`로 변경, ESC/타이핑-덮어쓰기 시에도 거절된 ghost 텍스트를 feedback `generated_text`로 전달(이전엔 "다시"에서만).
- (edit) `agent/chat_agent.py` — **(1-4)** `_local_table_catalog_block()` 추가 → tool-decision 프롬프트에 등록된 로컬 표 파일명+컬럼 카탈로그 주입. 채팅 tool 선택은 단일 라운드(`collect_tool_outputs`가 결과를 되먹이지 않음)라 `table_query` `list_tables→query` 발견 체인이 불가 → 카탈로그를 미리 주어 단일 `table_query(query)`로 특정 수치 조회 가능. 키워드 라우팅 아님(컨텍스트 제공, LLM이 결정). (edit) `services/local_corpus/table_query_service.py` — `_csv_header`를 첫 비어있지 않은 행만 읽도록 최적화(카탈로그가 매 턴 전체 CSV를 읽지 않게).
- (edit) `services/proactive/README.md` §3 — proximity 매칭 + last_rejected_text forward 반영.
- (tests) `tests/test_proactive_api.py` +4 (ghost 토큰 budget, proximity cooldown jitter, proximity 누적, retry avoid 텍스트 forward), `tests/test_table_query_tool.py` +3 (카탈로그 블록 생성/빈 경우/registry 없음).

**엔지니어링 결정**
- *1-4는 인프라가 이미 완비*: 로컬 파일(csv/docx/pdf/xlsx/txt/md)은 `LocalCorpusService`가 파싱→markdown→공유 ChromaDB에 `source_scope=LOCAL`로 인덱싱하고, `rag_search`(기본 `include_private_local=True`)·`table_query`가 채팅 allowlist에 있으며 경로(`run_root/local/manifest.json`)도 일치. **유일한 실제 갭**은 단일 라운드 tool 결정이 table_query 2단계 발견을 못 하던 것 → 카탈로그 주입으로 해소(아키텍처 단일-라운드 계약 유지, 최소 침습).
- *ghost는 budget 상향이 근본 해법*: 프롬프트는 이미 1~2문장으로 self-limit하므로 EOS가 종료를 맡게 generous budget만 주면 됨. 트리밍은 유효 내용 손실 위험이 있어 채택 안 함.
- *reject ladder는 proximity가 정공법*: 사용자가 말한 "cursor 위치 threshold"를 fixed-bucket 경계 flip 없이 구현. `anchor_id`(외부 adaptation cooldown·로그용)는 무수정 — native in-memory ladder만 coarse 매칭.

**검증**: `python -m unittest discover -s tests` → **471 tests OK**(기존 464 + 신규 7, 회귀 0). 편집한 7개 파일 `py_compile` 통과(frontend 포함).

### 2026-06-08 (cleanup) — 벤치마크/artifact 제거 · dead code · 설계문서 보관 · 대시보드 MVC 리팩터

멀티에이전트 감사(5 dimension finder → 적대적 삭제안전 verifier → synthesis)로 도출, 사용자 승인 항목만 실행. **4개 핵심 기능 blast radius 0**.

**삭제 (배포 불필요 / dead)**
- DRB 벤치마크 클러스터 **원자적 제거**(production import 0건 검증): `deep_research_bench/`(~173MB), `benchmarks/`, `core/prompts/drb_benchmark.py`, `tests/test_drb_*.py`(6), `DRB_AUTOSURVEY_BENCHMARK_VENDOR_INSTRUCTION.md`. `.gitignore`의 DRB 블록 + 위 로그 entry들의 코드는 히스토리로 유지. 부분 삭제 시 vendor-layout 테스트/`flat_runner` import가 깨지므로 한 번에. (테스트 471→413, 삭제분 58개 외 회귀 0.)
- `logs/memory_trace.log`(stale 단일세션 trace; `--mem-debug` 시 재생성), `llm/context_policy.py`(0바이트 빈 모듈), `frontend/ui/main_window.py:PlaceholderPage`(미사용 클래스).
- `embedding_fact_extractor`는 **유지**(사용자 결정 — 문서화된 F1 baseline 보존).

**보관**: `scenarios.md`, `docs/memory_sqlite_phase_AB_tasklist.md`, `docs/memory_sqlite_unification_proposal.md` → `docs/deprecated/`로 `git mv`(런타임 소비자 0, 사람용 설계문서). `generate_scenario_b.py` docstring 경로 갱신.

**MVC 리팩터 (CLAUDE.md 불변식: frontend는 코어 직접 호출 금지)**
- (del) `db/dashboard_service.py` — 그 wrapper 로직을 `api/services/dashboard_service.py::get_home_summary()`로 이관(동일 shape). (edit) `db/dashboard_repository.py` — db 계층 repository로 유지(`db.activity_repository`와 동일 패턴), `rename_workspace()` 추가 + 방어적 `init_db()`. (edit) `api/api_routes/dashboard.py` — `GET /api/v1/dashboard/home` + `POST /api/v1/dashboard/workspaces/{id}/rename`(plain def, 스레드풀). (edit) `frontend/controllers/agent_controller.py` — `get_dashboard_home()`/`rename_workspace()` HTTP wrapper. (edit) `frontend/ui/pages/dashboard_page.py` — `from db.*` import 전부 제거, `init_db()`·직접 SQLite UPDATE 제거 → HTTP 경유.
- 검증: 임시 `LOCALAPPDATA` DB로 smoke — `get_home_summary` shape·`rename_workspace`(updated True / missing·빈문자열 False) 정상. **남은 위반**: `frontend/ui/main.py`의 부팅 reconcile(`db.workspace_sync`) — 인프라 경로라 범위 외(추후).

**검증**: `discover -s tests` → **413 OK**(회귀 0). 편집 파일 전부 `py_compile` 통과. 대시보드 end-to-end smoke OK.

### 2026-06-08 (fix) — native ghostwriting 타이밍: 타이핑 인지 debounce + cooldown lock 해제

**배경(사용자 관찰)**: (1) 타이핑 속도보다 suggestion observe가 빨리 들어와, 연속 타이핑 중 떠버린 ghost를 다음 키 입력이 "거절"로 만들어 버림. (2) cooldown lock이 문장/문단을 옮겨도 안 풀리는 것처럼 느껴짐.

**원인/수정**
- **(1) 타이핑 인지 debounce** (`frontend/ui/windows/editor_window.py`): suggest debounce가 고정 300ms라 단어 사이 자연스러운 멈칫에도 발화 → 재개 타이핑이 reject가 됨. 최근 편집 timestamp(`_edit_times` deque)로 타이핑 속도를 추정해 **속도가 빠를수록 더 긴 idle을 요구**하는 적응형 delay(`_adaptive_suggest_delay_ms`: idle 1000 / moderate 1400 / fast 1800ms)로 교체. 연속 타이핑(flow) 중엔 timer가 계속 리셋돼 발화 안 함 → 정말 멈춘 뒤 1~2초에 발화. observe call 폭주와 phantom reject 동시 해소.
- **(2) cooldown lock 해제** (`services/proactive/orchestrator.py`): 1-3의 proximity window 기본값 **120 → 24자**. 120은 문장 하나 크기라 인접 문장으로 caret을 옮겨도 cooled state에 매칭 → 영구 lock처럼 보였음. window를 "몇 글자"로 줄여 스페이스/소규모 편집은 흡수하되 문장·문단 이동 시 cooldown 해제. (1)의 phantom reject 제거로 cooldown이 애초에 과형성되던 것도 함께 완화. `VERITAS_PROACTIVE_ANCHOR_PROXIMITY_CHARS`로 조정.

**테스트**: `tests/test_proactive_api.py` +1 (`test_cooldown_releases_when_cursor_moves_to_different_spot` — 같은 spot 잔존 / window 밖 해제). 기존 proximity 테스트(jitter 유지·누적)는 24자에서도 통과. (edit) `services/proactive/README.md` §3 값/동작 갱신.

**검증**: `discover -s tests` → **414 OK**(회귀 0). `editor_window.py`·`orchestrator.py` `py_compile` 통과.

### 2026-06-08 (fix) — 거절 retry additive grounding + 제안 타이핑-스루(type-along)

**배경(사용자 요청)**: (1) reject ladder의 level≥1 retry가 `editor_assist("continue")`(forced-RAG)를 써서, 인덱스 없는 워크스페이스에서 거절하면 `EditorGroundingUnavailable`로 재제안이 안 떴음. (2) 제안이 뜬 상태에서 사용자가 타이핑하는 게 무조건 거절은 아님 — 제안과 일치하면 받아쓰기로 소비하고, 글자가 달라지면 그 지점부터 재생성.

**(1) retry additive grounding**
- (edit) `agent/chat_agent.py` `iter_editor_assist(..., additive_grounding=False)` — forced-RAG 액션도 `additive_grounding=True`면 grounding 있으면 쓰고 없으면 plain fallback(raise 안 함). 사용자 클릭 quick action은 기본 False로 hard-gate 유지. (edit) `api/services/agent_runtime.py:editor_assist_iter` pass-through. (edit) `services/proactive/generator.py` — proactive editor_assist 2곳(native retry continue + 일반 lead-in)에 `additive_grounding=True`.
- 테스트: `tests/test_rag_grounding.py` +2 (forced→raise, additive→plain fallback). 기존 proactive fake에 kwarg 추가.

**(2) type-along (받아쓰기)** — `frontend/ui/windows/editor_window.py:MarkdownSourceEdit`
- 완결(final) 제안이 떠 있을 때 입력된 문자가 제안(선행 공백 무시한 정규형)의 prefix면 grey ghost에서 그만큼 소비하고 나머지를 계속 표시. **anchor 기준 `toPlainText()[anchor:caret]` vs 제안**으로 판정 → keyPress(영문)와 IME commit(한글) 둘 다 자연 처리(IME는 commitString만 비교, 조합 중엔 ghost 숨김 후 정착 시 reshow).
- 판정: 전체 일치 → **accept**(ladder clear), prefix → 소비 후 잔여 표시, 분기 → 종료(소비 0이면 **reject**, ≥1이면 **partial accept**로 ladder 오염 방지). 분기 지점부터는 기존 적응형 debounce가 새 제안을 재생성. 스트리밍(non-final) 중 입력은 종전대로 취소.
- `is_typing_along()` 추가 → `_fire_suggestion` 가드(ghost 숨김 중 중복 발화 방지). undo 불변식 유지(ghost는 항상 topmost; 입력 char은 실제 편집).
- 검증: headless(offscreen) Qt smoke — keyPress 5케이스(full→accept / partial-diverge→accept / 즉시분기→reject / 선행공백 skip / Esc-after-partial→accept) + IME 2케이스(음절별 commit full→accept / 즉시분기→reject) 전부 통과. (프로젝트에 Qt 테스트 하니스가 없어 스위트에는 미편입.)

**검증**: `discover -s tests` → **416 OK**(회귀 0). 편집 파일 `py_compile` 통과.

### 2026-06-08 (fix) — type-along ghost 가시성(reformat 방식) + 머리표지 echo 제거

**배경(사용자 관찰)**: (1) 받아쓰기(type-along) 시 회색 ghost가 사라져 사용자가 무엇을 칠지 볼 수 없음(특히 한글 IME 조합 중). (2) "첫 번째로,"나 번호 bullet 뒤에서 제안이 그 머리표지를 **반복**해서 출력.

**(1) type-along을 reformat 방식으로 재구현** — `frontend/ui/windows/editor_window.py:MarkdownSourceEdit`
- 기존 remove+insert(매 입력마다 grey run 제거 후 재삽입) 방식은 IME preedit와 충돌해 조합 중 ghost를 숨겼음(`reshow=not composing`). → **제안 전체를 회색 run으로 유지하고, 일치하는 prefix만 그 자리에서 회색→일반 색으로 recolour**(`_accept_ghost_prefix`: `setCharFormat`, 삽입/삭제 없음). grey run은 항상 *남은* 제안을 담아 **조합 중에도 계속 보임**. 입력 문자는 이미 grey로 존재하므로 super-insert하지 않고 흡수(keyPress)하거나, IME는 commit을 recolour로 흡수하고 다음 음절 preedit만 `_show_preedit_only`로 렌더.
- recolour 편집은 textChanged를 안 emit하므로 `ghostProgressed` 시그널 추가 → window가 dirty/preview/save 동기화. recolour로 topmost-edit 불변식이 깨지므로 `_remove_ghost_run`은 `_typealong_consumed>0`이면 undo 대신 range-delete. `document_text()`는 남은 grey만 제외(= 수락분만 저장/미리보기). 분기 시 소비 0이면 reject, ≥1이면 partial-accept.
- 검증: headless(offscreen) Qt — keyPress/IME 모두 **type-along 내내 `has_ghost()` 참**(ghost 가시) + full→accept / 즉시분기→reject / partial→accept / 선행공백 skip / Tab 수락 전부 통과.

**(2) 머리표지 echo 제거** — `agent/chat_agent.py` `_strip_prefix_echo(prefix, suggestion)`: 출력 head가 prefix 말단의 단어/머리표지를 그대로 반복하면(단어/줄 경계에서 시작하는 ≥2자 verbatim overlap) 제거. `iter_ghostwrite`의 flush 2지점(head-only)에 적용. 1자 overlap(정상 단어완성, 예: "다"→" 다음")은 floor 미만이라 보존. `core/prompts/editor.py:SUGGEST_SYSTEM_PROMPT`에 "방금 입력한 머리표지/어구 반복 금지" 지시 보강.
- 테스트: `tests/test_rag_grounding.py` +7 (echo 6: marker/bullet/subject strip, 단어완성·midword·no-overlap 보존 / prompt 1).

**검증**: `discover -s tests` → **423 OK**(회귀 0). `chat_agent.py`·`editor.py`·`editor_window.py` `py_compile` 통과.

### 2026-06-08 (fix) — type-along IME 한글자 밀림(preedit preview) + echo 전체텍스트 strip

**배경(사용자)**: (1) 한글 받아쓰기 시 다음 음절을 조합할 때, 회색 ghost가 아직 그 음절을 포함해 **조합 중 preedit 글자와 회색의 같은 글자가 겹쳐**(한글자 밀림). (2) echo 제거가 여러 단어 길이 echo는 여전히 못 잡음.

**(1) preedit preview** — `frontend/ui/windows/editor_window.py`
- IME preedit가 다음 grapheme(선행 공백+1글자)을 조합하는 동안 회색에서 그 grapheme을 **일시적으로 숨김**(`_apply_preedit_preview` range-delete, `_preedit_borrow`에 보관). 그러면 "조합중 preedit 글자 + 회색 나머지"가 정확히 합쳐져 중복/밀림이 사라짐. 매 inputMethodEvent 시작에 `_restore_preedit_preview`로 회색을 온전히 복원 후 commit 판정 → 다시 다음 grapheme 숨김. 조합 취소/키/마우스 interrupt 시에도 복원. range 편집이 topmost-undo 불변식을 깨므로 `_ghost_recolor_dirty` 플래그로 `_remove_ghost_run`이 range-delete 사용(fresh 제안마다 리셋).
- 검증: offscreen Qt — 조합 중 `_ghost_text`가 조합 글자를 제외한 나머지만 표시(중복 없음), commit마다 정확히 전진, 조합취소→복원, 조합후 분기→full text reject, 전체완성→accept, keyPress(영문) 정상.

**(2) echo 전체텍스트 strip** — `agent/chat_agent.py:_strip_prefix_echo` → 공개 `strip_prefix_echo`로 rename. `api/services/editor_service.py:suggest_stream`이 **수집된 최종 텍스트 전체**에 적용(iter_ghostwrite의 per-chunk strip은 decision window 크기에 갇혀 여러 단어 echo를 못 잡음). idempotent하므로 양쪽 적용 안전. 테스트 +1(multi-word echo).

**검증**: `discover -s tests` → **424 OK**(회귀 0). 편집 파일 `py_compile` 통과.

### 2026-06-08 (fix) — type-along을 in-document grey → **paint 오버레이**로 재구현 (IME freeze/중복 근본 해결)

**배경(사용자)**: 받아쓰기 중 (1) 갑자기 freeze 후 재제안, (2) 마지막 글자가 하나 더 복사돼 보임. 두 버그 모두 **IME 조합이 활성화된 동안 ghost grey run을 문서에서 편집(삽입/삭제/recolour)**하던 데서 비롯됨 — 연속 한글 입력은 preedit이 끊기지 않아 "조합 중 문서 편집"이 불가피했고, 그게 위치 어긋남(freeze)·복원-삽입 중복(마지막 글자)을 유발.

**수정** — `frontend/ui/windows/editor_window.py:MarkdownSourceEdit` 전면 재구현
- ghost를 **문서에 넣지 않고 `paintEvent`에서 회색 오버레이로 그림**(`_ghost_remaining`, `_paint_ghost` char 단위 wrap). 사용자는 실제 텍스트를 타이핑하고, commit/keyPress마다 `_evaluate_typealong`이 `toPlainText()[anchor:caret]`을 제안과 비교해 painted remainder를 줄임. **문서는 ghost로 인해 절대 편집되지 않음 → IME 충돌 원천 차단**(freeze·중복 소멸).
- 조합 중에는 다음 grapheme을 preedit이 이미 보여주므로 paint에서 그 grapheme을 skip(`_paint_skip`) → 중복 없음. `inputMethodEvent`는 `super()`로 commit/preedit을 자연 처리만 하고 ghost 편집 없음.
- accept=remainder를 실제 삽입, divergence(소비 0)=reject / (소비 ≥1)=partial-accept, Tab=remainder 삽입. `document_text()`는 그냥 `toPlainText()`(ghost가 문서에 없으므로). 제거: `_ghost_text`/`_preedit_borrow`/`_ghost_recolor_dirty`/recolour·preedit-preview 메서드 전부 + `ghostProgressed` 시그널(타이핑이 textChanged를 직접 emit하므로 불필요).
- 검증: offscreen Qt — ghost가 문서에 없음, keyPress/IME full→accept·즉시분기→reject·partial→accept, 조합 중 paint-skip=1(중복 방지), commit마다 remainder 정확히 축소, Tab=remainder 삽입 전부 통과.

**검증**: `discover -s tests` → **424 OK**(회귀 0). 편집 파일 `py_compile` 통과. (이전 in-document type-along 구현들을 본 오버레이가 대체.)

### 2026-06-08 (fix) — 오버레이 reflow 부재 → in-document grey + commit-recolour로 복귀

**배경(사용자)**: 오버레이 방식에서 (1) streaming 일부 안 보임, (2) 생성된 ghost가 편집기 공간을 못 늘려 안 보임, (3) ghost와 받아쓴 글씨가 겹쳐 가독성 저하. 셋 다 **오버레이가 문서를 reflow하지 않는**(paint만, 문서 안 늘어남) 근본 한계.

**수정** — `frontend/ui/windows/editor_window.py:MarkdownSourceEdit`를 **문서 내 grey + commit-recolour** 방식으로 복귀
- ghost를 다시 문서에 grey 텍스트로 삽입 → **실제 텍스트라 reflow**(아래 내용 밀려남·스크롤·확장 자동, 받아쓴 글씨와 순차 배치라 겹치지 않음). `document_text()`는 grey 구간 제외, `set_ghost`가 `_scroll_ghost_into_view`로 grey 끝까지 보이게 스크롤.
- type-along은 **삽입/삭제 없이 commit 시 matched grey를 grey→normal로 recolour**(`_accept_ghost_prefix`: `setCharFormat`만 → 캐럿/위치 불변 → IME 미충돌, freeze 없음). commit은 super 재삽입 없이 recolour로 흡수, 다음 음절 preedit만 `_show_preedit_only`. recolour로 undo 불변식이 깨지므로 `_ghost_recolor_dirty`면 `_remove_ghost_run`이 range-delete. recolour는 textChanged 미발생 → `ghostProgressed` 시그널로 dirty/preview/save 동기화 복원.
- **알려진 한계**: 한글 IME *조합 중*에는 조합 중 음절(preedit)과 grey의 그 음절 복사본이 잠깐 겹쳐 보임 — inline 제안 + IME의 본질적 제약(commit 순간 recolour로 해소). 오버레이는 이걸 없앴으나 reflow를 못 해 더 나쁜 가시성 문제를 유발했으므로, reflow(가시성)를 우선해 in-document 채택.
- 검증: offscreen Qt — ghost가 문서에 있어 `toPlainText`에 포함·`document_text`는 제외, keyPress/IME recolour로 document_text 증가, 즉시분기→reject, streaming partial→final, Tab=remainder 삽입 전부 통과.

**검증**: `discover -s tests` → **424 OK**(회귀 0). 편집 파일 `py_compile` 통과.

### 2026-06-08 (fix) — type-along: recolour-absorb → super-insert+delete (받아쓴 글씨 회색화/desync 해결)

**배경(사용자)**: in-document 복귀 후 reflow는 OK였으나, 받아쓰기 중 (1) 사용자가 친 글씨가 회색으로 변하고 (2) suggestion이 사라지는 desync. 원인: recolour-absorb 방식이 ① commit을 `_show_preedit_only`(합성 preedit 이벤트)로 흡수 → 실IME state desync, ② recolour가 anchor 어긋남 시 사용자 텍스트를 회색으로 칠함.

**수정** — `frontend/ui/windows/editor_window.py:MarkdownSourceEdit`
- recolour-absorb 폐기 → **super-insert + delete**: 입력(keyPress 문자 / IME commit)을 `super()`가 자연 삽입(정상 색·합성 이벤트 없음)하게 두고, grey가 중복하게 된 prefix만 `_consume_grey_prefix`로 **삭제**. 사용자 텍스트는 Qt가 normal 포맷으로 넣어 절대 회색이 안 되고(① 해결), 합성 preedit도 없어 IME desync 없음(② 해결). `accept_ghost`(Tab)는 grey 제거 후 normal 재삽입.
- 제거: `_accept_ghost_prefix`/`_normal_char_format`/`_show_preedit_only`. range 삭제가 undo 불변식을 깨므로 `_ghost_recolor_dirty`면 `_remove_ghost_run`이 range-delete.
- 검증: offscreen Qt + **색상 단언** — keyPress/IME로 받아친 글씨가 `editor.ghost`(회색)가 아님을 단언, grey 정확히 축소, 즉시분기→reject, Tab 수락분도 normal 색. 전부 통과.
- **남은 리스크**: IME 조합 중 grey 중복분 삭제(delete-during-preedit)는 offscreen 완전 재현 불가 — 실기기 확인 필요. 조합 중 1글자 겹침은 inline+IME 본질적 한계로 commit 시 해소.

**검증**: `discover -s tests` → **424 OK**(회귀 0). 편집 파일 `py_compile` 통과.

### 2026-06-08 (fix) — type-along: 조합 중 문서 편집 제거 (hide-during-composition)

**배경(사용자)**: super-insert+delete로도 받아쓰기 시 글씨 회색화·suggestion 사라짐이 재현 → **조합(preedit)이 활성화된 동안 grey를 건드리는 모든 동작(색변경/삽입/삭제)이 한글 IME와 충돌**한다는 근본 결론.

**수정** — `frontend/ui/windows/editor_window.py:MarkdownSourceEdit`: 조합 중에는 문서를 전혀 안 건드림.
- 입력 시작 순간(첫 키 입력 → `super()`가 preedit을 적용하기 직전, **아직 조합 비활성**) grey를 깨끗이 제거(`_remove_grey_if_shown`, undo) 후 type-along 세션 시작(`_begin_typealong`).
- 조합 중에는 grey를 **숨긴 채** 입력 텍스트만 `toPlainText()[anchor:caret]` 비교 추적(`_evaluate_typealong`). 문서엔 사용자가 친 정상색 텍스트만 존재 → 회색화·desync 불가.
- 조합이 **settle(preedit 빔, 공백/멈춤)** 한 순간에만 남은 grey를 다시 삽입(`_insert_grey`). 즉 grey 삽입/제거는 항상 비조합 시점에서만 발생 → IME 충돌 0.
- 전체 일치→accept, 첫 글자 분기→reject, ≥1자 후 분기→partial-accept, Tab=남은 부분 정상색 삽입. `has_ghost`/`is_typing_along`는 세션 기준(숨김 중에도 True)이라 suggest 타이머 계속 억제.
- 제거: `_typealong_match_len`/`_consume_grey_prefix`/recolour·합성 preedit 전부.
- **트레이드오프**: grey가 단어를 조합하는 동안 잠깐 숨고 단어 사이(공백)에서 다시 나타남 — 조합 중 문서 무편집을 위한 불가피한 절충.
- 검증: offscreen Qt + **색상 단언** — 조합 중 grey 부재·커밋 텍스트 정상색, settle 시 재표시, 전체/분기/Tab 시나리오 통과.

**검증**: `discover -s tests` → **424 OK**(회귀 0). 편집 파일 `py_compile` 통과.

### 2026-06-08 (fix) — type-along 최종: paint 오버레이 + EOF 빈줄 예약(reflow) — 원래 의도(조합 중에도 grey 계속 보임) 달성

**배경(사용자)**: hide-during-composition은 동작하나 단어 조합 중 grey가 숨어 불편 → "원래 의도(받아쓰는 내내 grey가 계속 보이며 줄어듦)"로 재시도 요청.

**핵심 모순 해소**: grey가 조합 중에도 보이려면(=의도) 조합 중 문서를 못 건드린다(=충돌 회피)와 양립해야 함 → **grey를 문서가 아니라 오버레이로 paint**하면 둘 다 만족. 단 오버레이는 문서를 못 늘려 reflow가 안 되므로(이전 실패) → **문서 끝에 빈 줄을 예약**해 reflow 공간 확보.

**수정** — `frontend/ui/windows/editor_window.py:MarkdownSourceEdit`
- ghost를 `paintEvent`/`_paint_ghost`로 캐럿 뒤에 회색 paint(`_ghost_remaining`, char 단위 wrap). 문서엔 안 들어감 → 사용자는 정상색 실제 텍스트만 타이핑, IME가 ghost를 절대 못 건드림(회색화·desync·사라짐 구조적 불가).
- type-along: `super()`가 입력을 자연 처리한 뒤 `_evaluate_typealong`이 `document_text()[anchor:caret]`을 제안과 비교해 painted remainder를 축소. 조합 중 grapheme은 preedit이 이미 보여주므로 paint에서 skip(`_paint_skip`) → 중복 없음. **grey는 조합 중에도 계속 보임**.
- reflow: `_set_reservation`이 문서 끝에 grey 높이만큼 빈 줄(`_reservation_len`)을 예약 — **유일한 문서 편집이며 조합 중(`self._composing`)엔 no-op**. `document_text()`가 예약분을 strip(저장/미리보기/카운트 전부 `document_text()` 사용 확인). 분기-중-조합으로 남은 예약은 다음 비조합 입력에서 정리.
- accept=remainder 실제 삽입, 첫 글자 분기→reject, ≥1자 후 분기→partial-accept, Tab=remainder 삽입. 제거: in-document grey 일체(`_ghost_text`/recolour/합성 preedit/`ghostProgressed`) — 타이핑이 textChanged를 직접 emit하므로 dirty/preview 동기화는 기존 경로로.
- 검증: offscreen Qt(600×400) — ghost 비문서·document_text clean, IME 조합 내내 remaining 유지+commit마다 축소+정상색, 멀티라인 예약>0+dismiss 시 0, streaming/Tab/분기 전부 통과.

**검증**: `discover -s tests` → **424 OK**(회귀 0). 편집 파일 `py_compile` 통과.

### 2026-06-08 (feat) — RAG 모드에서도 로컬 표(csv/xlsx) 수치 질의 지원 (table_query 사전단계)

**배경(사용자)**: "로컬 폴더 데이터 RAG가 안 됨, csv/xlsx 수치는 어떻게 질의?" 진단: table_query 메커니즘(인덱싱→manifest→list_tables→정확 집계)은 정상이나, **프론트 기본 채팅 모드가 "rag"** → `ask_rag_iter`(임베딩 검색만) → table_query를 절대 안 탐. tool 경로(`ask_auto_iter`, "research" 모드)만 table_query를 썼음. 임베딩엔 정확한 숫자가 없어 RAG 모드 수치 질의가 실패.

**수정** — `agent/chat_agent.py`
- `_collect_tool_outputs(question, allowed_tool_names=None)`로 파라미터화.
- `_collect_table_outputs(question)`: registry에 table_query가 있고 **등록된 표가 있을 때만**(빈 카탈로그면 LLM 라운드 생략) `table_query`만 노출해 단일라운드 tool 결정 실행.
- `ask_rag_iter`: strict RAG 답변 전에 `include_private_local and source_scope_filter in (all,local)`일 때 `_collect_table_outputs` 사전 실행 → 모델이 table_query를 호출했으면 그 정확한 출력으로 `_stream_final_answer`(근거 기반 합성) 후 종료, 아니면 기존 임베딩 RAG로 통과.
- **불변식 준수**: 키워드 라우팅 아님 — 모델이 카탈로그를 받고 table_query 사용 여부를 스스로 결정. web-only(`include_private_local=False`) 스코프에선 로컬 표 미조회. 채팅은 로컬 LLM 전용이라 local_private 노출 없음.
- 검증: 임시 워크스페이스 csv 인덱싱→`3월 합계=99000` 정확, mock LLM으로 RAG 모드 수치질문→table_query 라우팅·비표질문→임베딩 통과·무표시 생략·web-only 생략. 신규 회귀 4건(`RagModeTableQueryTests`).

**검증**: `discover -s tests` → **428 OK**(기존 424+4, 회귀 0). 편집 파일 `py_compile` 통과.

### 2026-06-10 (fix) — 외부 앱 카드 폭주(잠깐 멈춘 사이 5~6개) — 3중 발화 브레이크 + 카드 교체 UI

**배경(사용자)**: 외부 문서 앱에서 한 단락을 수정하고 잠시 멈추면 서로 다른 내용의 suggestion 카드가 5~6개 쌓임. 원인(아키텍처): 외부 카드 표시는 rule-based proactive가 아니라 **legacy screen 파이프라인(scenario scheduler)이 결정**하고 proactive는 shadow 관찰만 함(`screen_bridge.py`). 그 파이프라인의 시나리오 무관 브레이크는 `min_global_fire_interval_sec=5초`뿐이고, 시나리오별 cooldown(37.5~150s)은 *같은* 시나리오 재발화만 막는데 한 단락에서 24개 시나리오 중 다수가 동시 ready → CFS 공정성(vruntime 과금)이 매 발화마다 **다른** 시나리오를 뽑아 "서로 다른 내용"의 카드를 5초마다 1개씩 생산. 카드가 떠 있는 동안(미반응) 새 발화를 막는 게이트도 없었고(`_intervention_pipeline_busy`는 LLM 생성 중만 점유), proactive의 `time_since_last_intervention`은 산출만 되고 미사용. UI(`SuggestionList`)는 무제한 append.

**변경 파일** (3중 브레이크: 발화 간격 ↑ / 단락 단위 cooldown / 미해결 카드 게이트 + UI 교체·상한)
- (edit) `services/screen_tool_funcs/intervention/scenario_scheduler.py` — ① `paragraph_cooldown_sec`(기본 180s): 같은 단락(fingerprint)에는 **시나리오가 달라도** 재발화 금지(`last_fired_paragraphs`, 문서당 32개 bound, 상태 영구화 포함). 단락을 실제 수정하면 fingerprint가 바뀌어 자연 해제. ② `allow_immediate_fire(document_key, scenario_name, paragraph_fingerprint)`: '다시'(retry) 시 전역 throttle 기준점·그 단락 cooldown·그 시나리오 자체 기록만 선별 해제 → retry UX가 상향된 간격에 갇히지 않음. `select_and_charge`/`record_fire`/`is_paragraph_throttled`에 fingerprint 배선, trace에 `paragraph_throttle` 추가.
- (edit) `services/screen_tool_funcs/intervention/intervention_detector.py` — CFS·LLM router 양 경로에 paragraph_fingerprint 전달(라우터 경로는 `is_paragraph_throttled` 게이트).
- (edit) `services/screen_tool_funcs/screen_context_service.py` — ① 전역 throttle 기본 5→**60초**(`VERITAS_SCREEN_MIN_FIRE_INTERVAL_S`), 단락 cooldown `VERITAS_SCREEN_PARAGRAPH_COOLDOWN_S`. ② (new class) `UnresolvedCardGate` — 단일 슬롯 미해결 카드 게이트: 카드의 첫 non-empty 청크가 렌더되는 순간 잠기고, 사용자 feedback(복사/거절/다시/위치다름) 또는 `VERITAS_SCREEN_CARD_RESOLVE_TIMEOUT_S`(기본 90s) 무반응 만료로 풀림. `capture_once`가 `pipeline_busy OR gate.active()`면 스케줄 안 함 — 생성 중은 큐 점유(peek-based consumer)가, 표시 후는 게이트가 막아 빈틈 없음. 빈 답변(스킵된 개입)은 마킹 안 됨. `resolve_card(action="retry")`는 `allow_immediate_fire` 호출.
- (edit) `tools/screen_context_tool/screen_context_tool.py` — `mark_card_shown`(intervention dict)/`resolve_card`(event_id+feedback_action) 액션, status에 `unresolved_card`.
- (edit) `api/services/agent_runtime.py` — `on_answer`에서 카드별 1회 `mark_card_shown` 호출(pd_* rewrite **이후**라 proactive id + legacy id가 alias로 함께 등록). `resolve_screen_card` facade.
- (edit) `api/services/screen_monitor.py` — `resolve_card` facade + 이벤트 payload에 `paragraphFingerprint`/`documentKey`(frontend 교체 정책용).
- (edit) `api/services/screen_monitoring_service.py` — `record_feedback`이 양 경로(pd_*/legacy) 공통으로 게이트를 먼저 resolve(best-effort).
- (edit) `frontend/ui/windows/document_assist_window.py:SuggestionList` — `MAX_CARDS=3` 상한(초과 시 oldest 제거) + 같은 단락(fingerprint) 새 카드는 기존 카드 **교체**(`_index_by_fingerprint`/`_remove_card_at`, `_cards`↔`_suggestions` lockstep 유지). hydrate(`set_suggestions`)도 동일 정책(`_apply_card_policy`). (edit) `document_assist_window`/`frontend/ui/pages/writing_page.py` 호출부에 fingerprint 전달.
- (edit) `api/README.md` — 신규 env 3종 표 추가.

**엔지니어링 결정**: 게이팅 권한을 rule-based proactive로 이전하는 구조 수정(C안)은 별도 작업으로 분리(사용자 결정 — A+B 조합 선택). legacy 파이프라인 안에서 페이스 결정권을 "스케줄러의 시계"에서 "사용자의 반응"으로 옮기는 것이 이번 변경의 핵심: 카드 1개 → 반응/만료까지 정지 → 다음 카드. 키워드 라우팅·proactive 금지 원칙 모두 무접촉.

**테스트**: (new) `tests/test_screen_overload_guard.py` (21) — 단락 cooldown(타 시나리오 차단/타 단락 허용/만료/라우터 경로/bound/영구화 round-trip), retry 브레이크 해제 vs 거절 시 유지, UnresolvedCardGate(이중 id resolve/만료/스트리밍 재마킹 시 shown_at 유지), SuggestionList 교체·상한·스트리밍 in-place 갱신(offscreen Qt).

**검증**: `discover -s tests` → **508 OK**(기존 487+21, 회귀 0). 편집 파일 `py_compile` 통과.

### 2026-06-10 (feat) — 외부 앱 발화 페이스를 고정 60초 → **적응형 페이싱**으로 (반응 이력 + 새 내용 기반)

**배경(사용자)**: 직전 수정의 고정 60초 전역 간격이 "터무니없이 길다, 더 똑똑하게" — 고정 벽시계는 (1) 반응 이력(수락하는 사용자 vs 거절/무시하는 사용자), (2) 새 내용 유무(마지막 카드 이후 아무것도 안 썼는데 발화할 이유도, 새 문단을 썼는데 더 기다릴 이유도 없음), (3) 시간 경과 회복을 전부 무시한다. 특히 카드를 빠르게 처리하며 글을 이어 쓰는 **적극 사용자에게 가장 불리**.

**설계** (사용자 결정: ①+② 조합, floor 20s/base 30s): 발화 허용 = `elapsed ≥ floor` **AND** (`elapsed ≥ base×multiplier` **OR** 새 내용). proactive adaptation이 threshold에 쓰는 원리를 legacy 파이프라인의 *페이스*에 적용.
- **① 참여도 적응형 간격** — `fire_pace_multiplier`(문서당, 영구화): 수락(copy/like) ×0.6 / 다시 ×0.7 / 거절(red_reject/dislike/wrong_anchor) ×1.7 / 무시(카드 90s 만료·timeout) ×1.3, clamp [0.5, ceil/base]. **반감기 감쇠**(기본 600s)로 1.0에 수렴 — 거절 몇 번이 세션을 영구히 얼리지 않음. 결과 간격 clamp [floor 20s, ceil 240s].
- **② 새 내용 조기 해제** — 직전 발화 시점의 정규화 문서 길이(`last_global_fire_doc_chars`)와 현재의 차가 `early_release_min_new_chars`(80자) 이상이면 적응 간격 전에도 발화 허용(floor는 절대 유지). 새 내용 없이 간격만 지난 경우는 기존 시나리오 게이트(idle/정적 리뷰류)가 판단 — 정적 문서 리뷰 시나리오를 죽이지 않기 위해 조기 해제는 **가속기로만** 쓰고 hard 요구조건으로 안 둠.

**변경 파일**
- (edit) `services/screen_tool_funcs/intervention/scenario_scheduler.py` — `min_global_fire_interval_sec`/`is_globally_throttled` 제거 → `fire_interval_floor/base/ceil_sec`·`pace_decay_half_life_sec`·`early_release_min_new_chars` + `_global_gate_locked`(floor→적응 간격→조기 해제 순), `record_card_outcome(outcome)`(감쇠 접어넣고 곱셈 누적), `global_gate_reason()`(router 경로용). 상태에 `fire_pace_multiplier`/`pace_updated_at`/`last_global_fire_doc_chars`/`last_global_fire_paragraph_fp`(영구화+reset 포함). trace `global_throttle`에 `effective_interval_sec`/`pace_multiplier`/`early_release`, snapshot에 페이스 필드. reason 코드: floor 미달=`global_throttle`(연속성 유지), 적응 간격 미달=`adaptive_interval`.
- (edit) `services/screen_tool_funcs/intervention/intervention_detector.py` — router 경로를 `global_gate_reason(doc_chars=…)`로.
- (edit) `services/screen_tool_funcs/screen_context_service.py` — env 5종 배선(`VERITAS_SCREEN_FIRE_FLOOR_S` 20 / `_BASE_S` 30 / `_CEIL_S` 240 / `_DECAY_HALFLIFE_S` 600 / `VERITAS_SCREEN_EARLY_RELEASE_CHARS` 80; `VERITAS_SCREEN_MIN_FIRE_INTERVAL_S` 폐기). `UnresolvedCardGate.poll()` — 만료 카드를 1회 반환 → `capture_once`가 '무시' outcome으로 페이스에 반영. `resolve_card`가 action→outcome 매핑(`_CARD_OUTCOME_BY_ACTION`, 모듈 상수)으로 `record_card_outcome` 호출(retry는 기존 `allow_immediate_fire` 유지).
- (edit) `api/README.md` — env 표 갱신.

**체감 효과**: 반응이 좋고 새 글을 쓰는 사용자는 실질 **20초 페이스**까지 내려가고, 무시/거절이 쌓이면 **최대 4분**까지 물러나며, 안 쓰고 있으면 적응 간격을 채워야만(그리고 시나리오 자체 게이트를 통과해야만) 발화한다.

**테스트**: `tests/test_screen_overload_guard.py` 21→**33** — floor가 새 내용으로도 우회 불가 / 적응 간격 차단·새 내용 조기 해제·간격 경과 허용 / 거절 확대·수락 축소·무시 확대 / 반감기 감쇠 수치 / ceil clamp / router 경로 `global_gate_reason` / 페이스 상태 영구화 round-trip / `poll()` 만료 1회 반환 / resolve_card→페이스 연동.

**검증**: `discover -s tests` → **520 OK**(기존 508, 신규 12, 회귀 0). 편집 파일 `py_compile` 통과.

### 2026-06-10 (fix) — 외부 앱 보조가 (1) 커서 위치에 앵커되도록 + (2) 카드 '복사 가능 제안 + 회색 설명' 분리

**배경(사용자)**: (1) 결론을 작성 중인데도 **서론** 문장에 대한 검사 결과가 돌아옴(진입 직후 action 없이도). (2) 복사 시 바로 붙여넣을 수 있는 이어쓰기형 제안이 main이어야 하고, 부연/설명은 회색·작게·아래에 떠야 하는데 분리가 안 됨.

**원인 1 (서론 앵커링)**: 외부 앱의 "현재 작성 위치"(`current_paragraph_text`)가 caret 미검출 시 엉뚱한 곳으로 fallback. ① `ui_automation.py`가 selection(caret) 문단이 없으면 **마우스 hover 위치 문단**을 대용으로 씀 — 키보드로 결론을 쓰는데 마우스가 상단(서론)에 있으면 서론을 읽음. ② caret 완전 실패 시 `current_paragraph`가 **문서 전체**로 fallback되고, dispatcher의 `_focused_sentence`가 `changed_text[:80]`(diff **머리**=문서 맨 앞=서론)을 포함한 문장을 골라 서론 반환. ③ 진입 첫 캡처엔 `previous` 빈 값→`_diff_suffix`가 **문서 전체**를 changed_text로 반환→머리 매칭 발동.

**원인 2 (카드 분리)**: 카드는 이미 `_body`(복사) + `_note`(회색·12px·아래) 구조 + `"설명:"` 분리가 있으나, 원인1로 리뷰형 시나리오가 (서론에) 발동해 순수 코멘트만 나오고, 로컬 0.8B 모델이 `"설명:"` 라벨을 자주 누락해 설명이 복사 본문에 섞임.

**변경 파일**
- (edit) `services/screen_tool_funcs/capture/ui_automation.py` — `current_paragraph_text`를 **selection(caret) 문단만** 사용. hover는 `hover_text`로 telemetry만 유지하고 앵커로는 쓰지 않음(마우스≠캐럿).
- (edit) `services/screen_tool_funcs/core/content_filter.py` — `_diff_suffix`(append-suffix / 첫 캡처·non-append 시 문서 전체) → **`_diff_region`**(공통 prefix+suffix trim → 중간 편집도 bounded 영역, 첫 캡처/무변경은 `""`). edit_diff 시나리오는 `filtered.changed_text` 비의존(history 자체 diff)이라 영향 없음.
- (edit) `services/screen_tool_funcs/intervention/intervention_dispatcher.py` — **`_resolve_anchor`**(caret 문단[전체-문서 fallback 아닐 때] → 최근 편집영역[문서의 `_CHANGE_REGION_MAX_RATIO=0.6` 미만일 때만] → **문서 꼬리**; 머리는 절대 앵커 안 함). `recent_sentences`/`focused_sentence`를 거기에 앵커. `_focused_sentence`는 changed_text **꼬리**(커서 위치)로 매칭(머리 매칭 폐지).
- (edit) `services/screen_tool_funcs/intervention/intervention_detector.py` — LLM router의 `recent_text`/`focused_text`도 동일 원칙(편집영역→caret 문단→문서 꼬리, 머리 슬라이스 금지)으로.
- (edit) `core/prompts/chat.py` — `SCREEN_INTERVENTION_SYSTEM_PROMPT_TEMPLATE`의 OUTPUT STRUCTURE 규칙을 예시와 함께 강화: 붙여넣기 가능한 prose FIRST(라벨/메타 없음) → `"설명:"` 줄 → 모든 부연. 순수 코멘트면 `"설명:"`부터.
- (edit) `frontend/ui/windows/document_assist_window.py:SuggestionCard` — `_split_content_note` 3계층화: `"설명:"`族 마커(라인 선두 **+ 인라인** 모두) → `[Document …]` citation peel → **보수적 빈줄 fallback**(짧은 prose 리드 + 빈줄 + 후행 블록 → content/note 분리; 불릿/번호 리뷰 리스트·240자 초과 리드는 미분할). `_NOTE_MARKERS`/`_split_on_note_marker`/`_find_mid_marker`/`_peel_trailing_block` 추가.

**엔지니어링 결정**: `content_filter._resolve_current_paragraph`의 전체-문서 fallback은 **유지**(stable_paragraph 게이트·시나리오가 `current_paragraph_text` 비어있음에 의존). 앵커링 정정은 전부 소비측(dispatcher/router)에서 가드해 게이팅 동작 불변. 카드 빈줄 fallback은 이어쓰기 prose만 노리고 리뷰 리스트는 보존하도록 보수적으로(불릿/길이 가드).

**테스트**: (new) `tests/test_screen_cursor_anchor.py` (17) — `_diff_region`(첫 캡처/무변경/append/중간편집 bounded), `_resolve_anchor`(caret 우선·전체문서→편집영역·무caret무변경→꼬리), **결론 작성 중 서론이 focus로 안 잡힘**(핵심 회귀), focus가 편집 꼬리에 앵커, 카드 분리(명시 마커·인라인 마커·순수 이어쓰기·빈줄 fallback·리뷰 리스트 보존·긴 리드 보존·citation peel).

**검증**: `discover -s tests` → **537 OK**(기존 520, 신규 17, 회귀 0). 편집 파일 `py_compile` 통과.

### 2026-06-10 (fix) — 외부 앱 시나리오 **발화 위치**를 커서로 스코프 (서론 약어 발화 근절) + 충고형 출력 붙여넣기-우선

**배경(사용자 스크린샷)**: 메모장 커서가 **결론**(Ln 52)인데 카드는 본문 어딘가 "추론형 AI (AI Reasoning)" **약어**에 대한 충고. 직전 fix(anchor)는 LLM이 보는 *텍스트 창*만 커서로 옮겼고, **시나리오 발화 조건**은 그대로 `active_editor_text`(문서 전체) 스캔이라, 커서에서 먼 트리거(본문 약어)에 계속 발화. 또 acronym guidance가 `"On first use, spell out as …"` 충고문을 강제해 복사 본문에 충고가 들어가고 영어("On")까지 누수.

**원인**: 위치 특정 시나리오 다수(`acronym`/`citation_missing`/`quote_inserted`/`factual_claim`/`repeated_phrase`/`transition`/`weak_modifier`/`heading`/`numbered_list`/`code_block`/`todo`/`many_question_marks`)가 트리거를 **문서 전체**에서 탐지 → 커서 무관 발화. (전체 검토형 `whole_document_review`/`long_static_review`/`blank_document_start`만 전체 문서가 정당.)

**변경 파일** (결정: cursor_scope_text 필드 + 시나리오 전환 / 충고 guidance 붙여넣기-우선)
- (edit) `services/screen_tool_funcs/core/models.py` — `FilteredScreenContext.cursor_scope_text` 추가(사용자가 지금 쓰는 영역).
- (edit) `services/screen_tool_funcs/core/content_filter.py` — module func **`resolve_cursor_scope(full, caret, changed)`**(caret 문단→bounded 편집영역→문서 꼬리, 머리 금지) + `build`에서 `cursor_scope_text` 채움. dispatcher의 anchor 로직을 이 함수로 통합(단일 소스).
- (edit) `services/screen_tool_funcs/intervention/intervention_dispatcher.py` — `_resolve_anchor`를 `resolve_cursor_scope` 위임으로 축소(시나리오 스코프 = LLM 앵커 동일 규칙 보장).
- (edit) `services/screen_tool_funcs/scenario/{markers,text_quality,structure}.py` — 위 위치 시나리오 13종의 트리거 스캔을 `active_editor_text`/`current_paragraph_text` → **`cursor_scope_text`**. 전체 검토형(writing_flow.py)은 `active_editor_text` 유지.
- (edit) `core/prompts/chat.py` — `acronym_introduced` guidance를 **붙여넣을 첫-등장 형태(예: `추론형 AI (AI Reasoning)`) FIRST → `설명:` 뒤 이유**로 재작성(영어 예시·충고 동사 제거).

**테스트**: `tests/test_screen_cursor_anchor.py` 17→**23** — `resolve_cursor_scope`(caret/편집영역/꼬리, content_filter 채움), **본문 약어가 커서(결론)에 없으면 acronym 시나리오 미발화**(핵심 회귀), 커서 영역 약어면 발화.

**검증**: `discover -s tests` → **543 OK**(기존 537, 신규 6, 회귀 0). 편집 파일 `py_compile` 통과.

### 2026-06-10 (fix) — caret 없을 때 diff-offset 윈도우 + OCR 소스 위치 시나리오 억제 (로그 진단)

**배경(사용자 `--screen-debug` 로그)**: 직전 fix에도 두 실패 노출. ① 메모장: `paragraph_source=uia_full_text_fallback`(notepad UIA가 caret 문단 미제공) → `current_paragraph==전체문서` → 내 `resolve_cursor_scope`가 **작은 diff 한 조각**("로")을 scope로 골라 `recent_sentences="로"` → LLM이 한 글자 받음(쓰레기 제안). **직전 fix 전엔 문서 꼬리(결론 실제 텍스트)였으므로 회귀.** ② VS Code: foreground=`Code.exe`, app_text/UIA 실패 → OCR가 화면 통째(우리 `[screen_debug]` 콘솔·코드·nav 링크) 읽음 → `cursor_scope`가 OCR 깨진 글자 → `acronym`이 그 위에 발화.

**변경 파일** (결정: diff-offset 윈도우 / OCR면 위치 시나리오 억제)
- (edit) `services/screen_tool_funcs/core/content_filter.py` — `_diff_region`이 `(region, cursor_offset)` 반환(편집 끝=커서 위치). `resolve_cursor_scope(..., cursor_offset)`: caret 없으면 **`full[offset-600:offset]`**(커서 앞 실제 텍스트)를 scope로 — 작은 diff 조각이 아니라. 우선순위 caret 문단 → **diff-offset 윈도우** → 문서 꼬리. `focus_hint`(문장 선택용)는 작은 diff도 그대로. const `CURSOR_SCOPE_CHANGE_MAX_RATIO` export.
- (edit) `services/screen_tool_funcs/intervention/intervention_dispatcher.py` — `_resolve_anchor`가 ContentFilter가 채운 `filtered.cursor_scope_text`를 **그대로** anchor로(단일 소스; 비면 offset 없이 재계산).
- (edit) `services/screen_tool_funcs/intervention/intervention_detector.py` — `_OCR_SUPPRESSED_SCENARIOS`(위치 prose 14종) + `filter_ocr_suppressed(ready, source)`: `current_paragraph_source`가 `ocr_*`면 위치 시나리오를 ready set에서 제거(OCR은 커서를 신뢰있게 못 잡음). 전체 검토형·작성상태형(idle/churn/blank)은 유지. blocker/trace 기록.

**테스트**: `tests/test_screen_cursor_anchor.py` 23→**27** — `_diff_region` 튜플(offset), **offset 윈도우가 작은 diff "로" 대신 커서 앞 실제 텍스트**(케이스1 회귀), 긴 문서 윈도우가 서론 배제, `_resolve_anchor`가 cursor_scope_text 사용, `filter_ocr_suppressed`(OCR면 acronym/citation drop·whole_doc 유지 / 비OCR 전부 유지).

**검증**: screen 스위트(cursor_anchor 27 + overload_guard 33) OK. `discover -s tests` 547 중 1 실패는 `test_proactive_*`의 **기존 background-thread 타이밍 flake**(매 실행 다른 proactive 테스트; 단독 26 OK; screen 변경과 무관). 편집 파일 `py_compile` 통과.

### 2026-06-10 (refactor) — 외부 앱 보조를 **native editor 모델**로 단순화 (24시나리오 → 커서-로컬 3종) + KB junk 필터 + 답변 토큰 cap

**배경(사용자 제안 #1/#4)**: native editor(우리 Qt 에디터)는 "caret 기준 prefix/suffix → 이어쓰기 1개 + 설명 분리"로 단순·정확. 외부 앱도 같은 모델로 하면? 그리고 "문제를 너무 어렵게 푸는 것 아닌가 — 24개 시나리오 가정 필요한가?". 결론: **맞다, 과설계.** 24-시나리오(acronym/citation/quote/heading/review/...)는 사실 *문서 전역 리뷰* 제품이라 OCR·문서전역 스캔·CFS 복잡도를 끌어들였고 이번 세션 버그(위치 불일치·OCR 쓰레기·오발화) 대부분의 근원. native 모델로 좁히면 그 버그군 전부 소멸.

**변경 파일** (결정: native 모델 재작성 + KB junk + 잘림 같이)
- (edit) `services/screen_tool_funcs/screen_context_service.py` — 등록 시나리오 24 → **커서-로컬 flow 3종**: `IdleAfterWriting`(이어쓰기) / `ParagraphChurn`(막힌 문단 재작성) / `BlankDocumentStart`(빈 문서 시작). 나머지 21종은 클래스만 남기고 미등록(추후 명시적 "문서 검토" 기능으로 재도입 가능).
- (edit) `services/screen_tool_funcs/core/models.py` + `content_filter.py` — `FilteredScreenContext.cursor_located`: OCR-only가 아니고 (진짜 caret 문단 OR 캡처 간 diff)일 때 True. 커서를 신뢰있게 잡았는지.
- (edit) `services/screen_tool_funcs/intervention/intervention_detector.py` — `filter_unlocated`: `cursor_located=False`면 커서-필수 시나리오(idle/churn) 발화 억제(blank 예외). **커서를 모르면 이어쓰기/재작성 제안 안 함** — native 방식.
- (edit) `agent/chat_agent.py` — ① `_drop_nav_junk_documents`/`_is_nav_junk`: KB retrieve 결과 중 nav-menu/link-list 청크(스크랩 boilerplate; 로그의 `* [컨퍼런스](https://...)` 류) 제거 → 프롬프트 오염 차단(전부 junk면 "관련 KB 없음"). ② `_screen_call_request`에 `extra_sampling_params.max_tokens`(`VERITAS_SCREEN_ANSWER_MAX_TOKENS`=320) — chat 프로필 기본 cap이 답변을 문장 중간에 자르던 잘림 방지.

**진단(로그)**: ① notepad가 `uia_full_text_fallback`(caret 미제공)이라 직전 fix가 작은 diff "로"를 scope로 → 잘림형 쓰레기(이미 diff-offset 윈도우로 해결). ② foreground가 Code.exe → OCR이 화면 통째(디버그 콘솔·코드·nav 링크) read → cursor_located=False로 차단. ③ `--screen-debug`의 전체 프롬프트+KB 노출은 정상(디버그 플래그, 로컬 콘솔), 단 KB 내용이 nav junk였던 게 진짜 문제 → 필터.

**테스트**: `tests/test_screen_cursor_anchor.py` 27→**38** — `cursor_located`(caret/diff→True, OCR/무편집→False), `filter_unlocated`(idle/churn drop·blank 유지), `_is_nav_junk`(링크리스트 junk·prose 보존·링크1개 prose 생존), `_screen_call_request` max_tokens 명시.

**검증**: `discover -s tests` → **558 OK**(기존 543, 신규 15, 회귀 0, flake 없음). 편집 파일 `py_compile` 통과.

### 2026-06-10 (fix) — cursor_located가 멈춤(pause) 시 idle을 죽이던 회귀 (sticky cursor offset)

**배경(사용자)**: brief를 notepad에 붙여넣고 `## 결론 …`까지 썼는데 보조가 **안 뜸**. 원인: 직전 refactor의 `cursor_located` 게이트가 idle을 깸. idle(이어쓰기)은 **멈춤** 시 발화 = 현재 캡처에 diff 없음. notepad는 caret 미검출(`uia_full_text_fallback`)이라 멈추면 `change_offset=None` → `cursor_located=False` → `filter_unlocated`가 idle 제거 → **영영 안 뜸**. (caret 주는 앱(Word)은 무관 — real caret으로 located.)

**변경 파일**
- (edit) `services/screen_tool_funcs/core/content_filter.py` — `ContentFilter._sticky_cursor_offset`: 현재 캡처에 변경이 있으면 그 offset 기록, 변경이 없어도 **텍스트가 직전 편집 시점과 동일하면(=멈춤) 기억한 offset 유지**. caret 없는 앱에서도 멈춤 동안 cursor_located·cursor_scope(커서 앞 윈도우)가 유지돼 idle이 발화한다. 텍스트가 (편집 외 이유로) 바뀌면 위치 불명 → None(다른 문서로 안 샘).

**테스트**: `tests/test_screen_cursor_anchor.py` 38→**40** — 편집 후 멈춤 캡처에서 sticky로 located 유지(핵심 회귀), 다른 문서로는 sticky 미적용.

**검증**: `discover -s tests` → **560 OK**(기존 558, 신규 2, 회귀 0). 편집 파일 `py_compile` 통과.

### 2026-06-10 (feat) — 외부 앱 보조를 **native ghostwrite 모델**로: caret-continuation 엔진 (속도 + retry)

**배경(사용자)**: (1) 반응 너무 느림. (2) "다시"가 재제안을 안 함. 요구: native editor ghostwriting과 **동일 로직** — UIA로 caret 위치를 주기적으로 읽고, 최근 N폴링 동안 caret이 안 움직이면 **즉시** 제안(검은 본문 이어쓰기 + 회색 근거).

**진단**: 첫 제안 25~45초 = capture **5s** + dwell **5캡처** + idle **2캡처** + 페이스 floor **20s**. native는 ~1-2초 debounce. dwell/CFS/idle 기계는 느린 OCR 폴링용. **retry는 구조적 불가**: idle 발화 조건이 `changed_before_pause`(히스토리 윈도우 내 최근 편집)라, 카드 뜬 뒤 타이핑 없이 "다시"하면 새 편집이 없어 idle이 재발화 못 함(게다가 ~50초 후 idle 자체 침묵).

**변경 파일** (결정: native caret-poll 엔진 신규, ~2초[1s×2])
- (new) `services/screen_tool_funcs/intervention/caret_continuation.py` — **`CaretContinuationEngine`**: 연속 캡처의 `cursor_scope_text`가 `stable_polls`(2)회 안정 + `cursor_located`(UIA caret/diff) + prefix 충분 → 즉시 `idle_after_writing` InterventionDecision 발화. **spot-dedup**(같은 fingerprint 재발화 금지, 커서/텍스트 변해야 다음). **retry**: `request_retry(avoid_text)`로 같은 자리 즉시 재발화 + 직전 제안을 avoid로 — idle-gate(새 편집 요구) 미사용이 retry 작동의 핵심.
- (edit) `services/screen_tool_funcs/screen_context_service.py` — `capture_once`가 `detector.decide` 대신 **engine.observe**로 발화 결정(시나리오/dwell/CFS/scheduler 우회). `UnresolvedCardGate`에 직전 답변 저장(`mark_shown(answer)`) → `resolve_card(retry)`가 `engine.request_retry(avoid_text=card.answer)`.
- (edit) `services/screen_tool_funcs/intervention/intervention_dispatcher.py` — payload에 `avoid_text`(metadata→consumer) 전달.
- (edit) `agent/chat_agent.py` — answer 콜백마다 `mark_card_shown(answer_text=…)`로 직전 제안 갱신. `answer_screen_intervention`이 `avoid_text` 있으면 "직전 제안 반복 금지 + 다른 이어쓰기" 지시 주입. consumer poll 2→**1초**.
- (edit) `tools/screen_context_tool/screen_context_tool.py` — `mark_card_shown` `answer_text` 파라미터.
- (edit) `api/services/agent_runtime.py` — `VERITAS_SCREEN_INTERVAL` 기본 5→**1.0**. on_answer가 answer 전달.

**효과**: 멈춘 뒤 **~2초** 발화(native 체감). "다시"=같은 자리 즉시 다른 문장. 검은 본문(이어쓰기)+회색 설명(카드 분리 기존). 24시나리오/dwell/CFS/적응페이스는 외부 continuation에서 미사용(코드 잔존). cursor_located로 커서 모르면 발화 안 함.

**테스트**: (new) `tests/test_caret_continuation.py` (9) — N폴 안정 발화·dedup·커서이동 재발화·미확정/짧은prefix 미발화·busy/card 보류·retry 즉시+avoid·문서 독립. (edit) `tests/test_screen_overload_guard.py` `ResolveCardRetryTests` — retry→engine 즉시 재발화(avoid) / 거절→예약 없음 (scheduler-pace 테스트 obsolete 제거).

**검증**: `discover -s tests` → **568 OK**(기존 560, 신규 9 −1 제거, 회귀 0). 편집 파일 `py_compile` 통과.

**후속(같은 날) — retry는 같은 카드 갱신**: "다시" 재발화가 새 카드를 만들지 않고 **원래 카드를 in-place 갱신**하도록. `request_retry(target_event_id)` → 엔진이 `metadata.retry_event_id`로 흘려보냄 → dispatcher payload → `agent_runtime.on_answer`가 retry면 pd_ rewrite를 건너뛰고 **원래 카드 id를 재사용** → `record_assist_answer`가 같은 eventId를 upsert → 프론트가 같은 카드 텍스트만 갱신. 엔진/overload 테스트에 `retry_event_id` 검증 추가. 568 OK 유지.

**후속(같은 날) — 카드 높이 클립 수정 + startup grace**: (a) 스트리밍 갱신 시 카드 본문이 첫 청크 높이에 고정돼 잘려 보이던 문제(상단 새 카드가 "깨져" 보임) — `SuggestionCard.set_text`가 `_apply_parsed` 후 `_sync_height()`로 높이 재계산(upsert가 scroll-to-bottom을 호출하며 높이를 재싱크하던 부수효과를 newest-first 전환 때 잃은 회귀). (b) **진입 직후 1.5초 startup grace** — `start_polling`이 `_monitor_started_at` 기록 + `continuation_engine.reset()`, `capture_once`가 grace 내면 `engine.observe(suppressed=True)`로 **안정만 누적하고 발화 보류**(첫 캡처 caret이 엉뚱한 위치일 수 있어 커서 자리잡을 여유). grace 후 여전히 안정이면 즉시 발화. `VERITAS_SCREEN_START_GRACE_S`(1.5). 테스트 +2(grace suppress·streaming 높이 재싱크). 575 OK.

**후속(같은 날) — 실시간 보조창 카드 UX**: (1) **최신 제안 최상단**(newest-first) — `add_suggestion`이 `insertWidget(0)`로 맨 위 삽입, 지난 제안은 아래로 이력 누적(스크롤로 확인), `MAX_CARDS` 3→**50**(메모리 bound; 초과 시 맨 아래=가장 오래된 것 제거), `set_suggestions`는 store 시간순을 reverse해 표시. 같은 단락 fingerprint 교체 폐기(이력 보존). 스크롤은 top으로. (2) **거절 시 카드 제거** — `SuggestionList._on_card_feedback`이 `red_reject`/`dislike`면 해당 카드 제거 후 host로 bubble(HTTP). (3) **"다시" 즉시 "(재제안 중...)"** — `SuggestionCard.show_regenerating()`가 클릭 즉시 본문을 비우고 placeholder 표시(작동 신호), 백엔드 재생성이 같은 event_id로 새 내용 upsert하면 덮어씀. `frontend/ui/windows/document_assist_window.py`. 테스트 `SuggestionListCardPolicyTests` 재작성(최신최상단·이력·cap·거절제거·다시placeholder). 573 OK.

**후속(같은 날) — 섹션 헤딩 주입 (native와 동일한 문서 구조 인식)**: 커서가 속한 섹션을 이어쓰기에 명시 주입. `ContentFilter._nearest_heading`(native `ChatAgent._nearest_heading`과 동일 규칙)이 **문서 머리~커서**에서 가장 가까운 마크다운 `#`헤딩을 추출 → `FilteredScreenContext.section_heading`(cursor_located일 때만, 전체 prefix 스캔이라 600자 윈도우 밖이어도 유지) → dispatcher writing_context → consumer `_screen_prompt_writing_context`의 `section_heading` 필드 → `SCREEN_INTERVENTION_SYSTEM_PROMPT`에 "section_heading 있으면 그 섹션 역할/주제 범위 안에서 이어쓰라(결론은 결론답게), 헤딩 텍스트 자체는 반복 금지" 규칙. → "## 결론" 아래에서 쓰면 LLM이 결론 작성 중임을 안다. 테스트 `test_screen_cursor_anchor.py` +4(`SectionHeadingTests`). **572 OK**.
