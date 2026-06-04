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
