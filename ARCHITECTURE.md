# Veritas — 아키텍처 개요

> AI 에이전트가 프로젝트 구조를 빠르게 파악하기 위한 지도입니다.
> 기능별 상세/변경 이력은 루트 [`README.md`](README.md), 디렉터리별 세부는 각 폴더의 `README.md` 참고.

Veritas는 **로컬 LLM(llama-server) 기반의 리서치 어시스턴트**입니다. 핵심 기능 3가지:
- **AutoSurvey** — 계획 → 수집 → 요약 → gap 분석 → 재계획 반복으로 웹을 조사해 보고서 생성
- **RAG 채팅** — AutoSurvey가 만든 마크다운 산출물을 ChromaDB에 인덱싱해 근거 기반 답변
- **스키마 기반 툴 채팅** — LLM이 프롬프트/스키마만 보고 어떤 tool을 쓸지 결정

설계 원칙: **의도 판단은 LLM(프롬프트·스키마)의 몫, 코드는 실행 경계만 강제한다.** (키워드 기반 라우팅 금지)

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
| `services/` | 도메인 서비스 | `rag_service.py`, `run_store_tool_funcs/`, `fetch_webpage_tool_funcs/`, `screen_tool_funcs/` | 상태/로직 소유자. RAG 인덱싱·검색, 워크스페이스 산출물 저장(`RunStoreService`/`path_manager`/`record_serializer`), Crawl4AI 페이지 수집, 화면 OCR/UIA 캡처 |
| `llm/` | 인프라 | `llama_server_llm.py` | `LLMClient`: OpenAI 호환 llama-server 클라이언트. `ask`/`ask_json`/`iter_ask`(스트리밍)/`embed`. 시작 시 `/props`로 `n_ctx` 자동 감지 |
| `storage/` | 인프라 | `vector_store.py` | `VectorStore`: ChromaDB `PersistentClient` 래퍼. 워크스페이스별 `runs/<id>/chromadb/` |
| `db/` | 인프라 | `db.py`, `schema.py`, `workspace_sync.py` | 로컬 SQLite (`%LOCALAPPDATA%/VERITAS/veritas.db`). 워크스페이스/문서/활동로그/app_state 테이블. `workspace_sync`가 `runs/` 디스크와 DB 동기화·삭제 |
| `core/` | 공유 | `prompts.py`, `models.py` | 모든 LLM 프롬프트(시스템·grounding·planning·summary·RAG·chat), 공용 데이터 모델(`DocRecord`) |
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

---

## 상태(State)는 어디에 사는가

| 저장소 | 위치 | 내용 |
|---|---|---|
| 워크스페이스 산출물 | `runs/<workspace>/` (또는 `--output-dir`) | 원본 HTML/텍스트, 문서·배치 요약, plan/grounding/index JSON, `final.md` |
| 벡터 인덱스 | `runs/<workspace>/chromadb/` | RAG용 임베딩 (ChromaDB SQLite) |
| 앱 메타데이터 | `%LOCALAPPDATA%/VERITAS/veritas.db` | 워크스페이스 목록, 문서, 활동 로그, `app_state`(현재 워크스페이스 등) |
| 서버 런타임 상태 | 인메모리 (`AgentRuntime` 싱글톤) | 현재 워크스페이스의 LLM/registry/workflow/chat_agent, 조사 진행 ring buffer |

워크스페이스 = `runs/` 아래 폴더 하나. `db/workspace_sync.py`가 디스크 폴더와 SQLite 행을 부팅 시 동기화하고 사용자 삭제를 처리.

---

## 코드 변경 시 어디를 봐야 하나

| 하고 싶은 일 | 보는 곳 |
|---|---|
| 새 tool 추가 | `tools/<new_tool>/`에 `tool_schema.json`+`BaseTool` → `tools/loader.py`에 등록 (절차는 `tools/README.md`) |
| 조사 파이프라인 단계 수정 | `workflows/autosurvey_workflow.py` |
| 프롬프트 수정 | `core/prompts.py` (코드에 프롬프트 인라인 금지) |
| 새 API 엔드포인트 | `api/api_routes/`에 라우터 + `api/services/`에 로직 |
| 데스크톱 화면 추가/수정 | `frontend/ui/pages/` + `controllers/agent_controller.py`에 HTTP 호출 |
| LLM 호출 방식 변경 | `llm/llama_server_llm.py` |
| 영속 데이터 스키마 변경 | 파일 산출물 → `services/run_store_tool_funcs/`, SQLite → `db/schema.py` |

피해야 할 패턴: `chat_agent.py`의 키워드/정규식 라우터, user 메시지 단어 기반 tool 강제 호출 — 의도 판단은 프롬프트·스키마로.
