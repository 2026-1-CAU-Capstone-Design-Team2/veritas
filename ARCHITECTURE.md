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
| `tools/` | 역량 | `tool.py`(BaseTool/ToolResult), `registry.py`, `loader.py`, `llm_tooling.py` | 각 `*_tool/` 하위에 `tool_schema.json` + `BaseTool` 구현. AutoSurvey 내부 tool(web_search·fetch_webpage·term_grounding·query_plan·document_summarize·final_report)과 chat 노출 tool(current_time·rag·autosurvey·screen_context) |
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

---

## 상태(State)는 어디에 사는가

| 저장소 | 위치 | 내용 |
|---|---|---|
| 워크스페이스 산출물 | `runs/<workspace>/` (또는 `--output-dir`) | 원본 HTML/텍스트, 문서·배치 요약, plan/grounding/index JSON, `final.md` |
| 벡터 인덱스 | `runs/<workspace>/chromadb/` | RAG용 임베딩 (ChromaDB SQLite) |
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
| 데스크톱 화면 추가/수정 | `frontend/ui/pages/` + `controllers/agent_controller.py`에 HTTP 호출 |
| LLM 호출 방식 변경 | `llm/llama_server_llm.py` |
| 영속 데이터 스키마 변경 | 파일 산출물 → `services/run_store_tool_funcs/`, SQLite → `db/schema.py` |
| **Proactive 후보 type 추가** | `proposal_models.py`의 `TaskType` Literal + `candidates.py`에 `_maybe_*` 빌더 + `evaluator.py`의 `_need_signal`/`_task_fit` 분기 + `core/prompts/proactive.py`의 lead-in. README.md §3 업데이트 |
| **Proactive 게이트 조정** | hard gate → `evaluator.py:check_hard_gates`. score 계수 → `evaluator.py:ScoreBreakdown.total`. threshold → `BASE_SHOW_THRESHOLD` 또는 `adjusted_threshold`. native reject ladder 상수 → `orchestrator.py:NATIVE_ANCHOR_REJECT_LIMIT` / `_COOLDOWN_S` |
| **Proactive 영구화 layout 변경** | `policy_store.py`의 경로 + `adaptation.py`의 dataclass. 회귀 가드: raw text 누출 / bandit import 금지 테스트 깨지지 않게 주의 |

피해야 할 패턴:
- `chat_agent.py`의 키워드/정규식 라우터, user 메시지 단어 기반 tool 강제 호출 — 의도 판단은 프롬프트·스키마로.
- **Proactive 분기에서 hard-coded 어휘 키워드 추가** ("근거"/"출처"/"통계" 같은 도메인 단어 리스트로 feature 산출) — 회귀 가드 테스트(`NoKeywordModulesTests`)가 fail.
- **`services/proactive/legacy_bandit/`의 코드를 production에서 import** — 동일하게 가드 테스트가 fail.
- **`services/proactive/generator.py`에 prompt 문자열 인라인** — 모든 prompt copy는 `core/prompts/proactive.py`로.
- **Proactive 영구화 JSONL/JSON에 raw document text 저장** — 영구화는 char counts + anchor hash만.
