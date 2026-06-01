# VERITAS API 명세서

VERITAS의 FastAPI 백엔드입니다. PySide6 데스크톱 프론트엔드는 모든 백엔드
호출을 이 명세에 따라 수행합니다.

- Base URL: `http://127.0.0.1:8000` (기본값)
- API prefix: 모든 비-system 엔드포인트는 `/api/v1/`로 시작
- 요청 본문: `application/json` (파일 업로드만 `multipart/form-data`)
- 응답 본문: `application/json` (스트리밍 엔드포인트만 `text/event-stream`)
- 인증: 없음 (단일 사용자 로컬 데스크톱 환경 가정)
- OpenAPI 자동 문서: `http://127.0.0.1:8000/docs`

## 목차

1. [실행 및 환경 변수](#실행-및-환경-변수)
2. [공통 규칙](#공통-규칙)
3. [핸들러 비동기 패턴](#핸들러-비동기-패턴)
4. [엔드포인트 카탈로그](#엔드포인트-카탈로그)
    - [System](#system)
    - [Frontend bootstrap](#frontend-bootstrap-feactions)
    - [Workspaces](#workspaces)
    - [Dashboard](#dashboard)
    - [Research](#research)
    - [Documents](#documents)
    - [Verify](#verify)
    - [Chat](#chat)
    - [Document Assist](#document-assist)
    - [Draft](#draft)
    - [Feedback](#feedback)
    - [Write / Predictions](#write--predictions)
    - [Settings](#settings)
    - [Screen Monitoring](#screen-monitoring)

---

## 실행 및 환경 변수

### 실행

프로젝트 루트에서:

```powershell
python -m pip install -r requirements.txt
python -m api --api --host 127.0.0.1 --port 8000
```

또는 직접 uvicorn:

```powershell
python -m uvicorn api.api:app --host 127.0.0.1 --port 8000 --reload
```

### 의존 외부 서버

- OpenAI 호환 LLM 서버 (예: `llama-server`)가 chat completions와 embeddings를
  제공해야 함. 기본 위치는 chat `127.0.0.1:8080`, embeddings `127.0.0.1:8081`.

### 환경 변수

| 변수 | 기본값 | 설명 |
|---|---:|---|
| `VERITAS_OUTPUT_DIR` | `runs` | AutoSurvey 결과, RAG index, chat history 저장 루트 |
| `VERITAS_API_BASE_URL` | `http://127.0.0.1:8000` | **frontend** 가 연결할 API 서버 주소. API 를 다른 포트로 띄웠다면 이 값을 맞춰야 함 |
| `VERITAS_EMBED_API` | `0` | `1`이면 frontend 가 외부 API 를 못 찾을 때 UI 프로세스 안에 API 서버를 자동 기동(in-process). 기본 `0`: 외부 API 가 없으면 조용히 임베드하지 않고 명확히 에러 표시 |
| `VERITAS_LLM_HOST` | `127.0.0.1` | OpenAI 호환 LLM 서버 host |
| `VERITAS_LLM_PORT` | `8080` | LLM 서버 port |
| `VERITAS_EMBED_HOST` | LLM과 동일 | embedding 서버 host (없으면 LLM과 같이 사용) |
| `VERITAS_EMBED_PORT` | `8081` | embedding 서버 port |
| `VERITAS_LLM_PARALLEL` | `1` | per-doc cleanup/summary·embedding 배치의 동시 LLM 요청 수. llama-server `-np` 슬롯 수와 맞출 것. `1`이면 직렬(기존 동작) |
| `VERITAS_TRACE_LATENCY` | `1` | LLM 호출 지연 시간 로그 출력 |
| `VERITAS_MAX_DOCS` | `15` | AutoSurvey 한 회 실행당 최대 수집 문서 수 |
| `VERITAS_BATCH_SIZE` | `5` | collect/summarize 배치 크기 |
| `VERITAS_MAX_CONTEXT` | `16384` | 문서 요약 시 컨텍스트 토큰 budget |
| `VERITAS_SCOUT_DOCS` | `3` | 초기 scout 단계 문서 수 |
| `VERITAS_API_AUTOSURVEY_MAX_DOCS` | `5` | chat tool로서의 autosurvey 캡 |
| `VERITAS_ENABLE_SCREEN_CONTEXT` | `1` | screen_context tool 등록 여부 (`0`이면 비활성) |
| `VERITAS_SCREEN_INTERVAL` | `5.0` | screen context polling 주기(초) |
| `VERITAS_SCREEN_DEBUG` | `0` | screen context 디버그 로그 |

### Agent runtime

API는 LLM 서버에 의존하므로 runtime을 **lazy initialize**합니다. LLM 서버가
준비되지 않은 경우 임의 응답을 만들지 않고 `503` 또는 `502` 오류를 반환합니다.

부팅 시 runtime은:

1. `runs/_pending_*` 디렉토리 정리
2. 빈 `runs/api/` 디렉토리 정리 (이전 세션의 잔존 폴더)
3. `runs/` 하위에 실제 연구 산출물(`final.md` · `summary/index.json` ·
   `doc_*.md`)이 있는 가장 최근 워크스페이스를 탐색
4. 찾으면 그 폴더에 attach. 없으면 `runs/api/`를 임시 home으로 사용
5. 이후 첫 AutoSurvey가 완료되면 새 워크스페이스로 자동 이동, 빈 `api/`는 제거

---

## 공통 규칙

### 성공 응답

각 엔드포인트의 응답 스키마는 아래 카탈로그 항목 참조. 별도 wrapper는 없습니다.

### 오류 응답

모든 오류는 다음 구조로 통일됩니다:

```json
{
  "error": {
    "code": "HTTP_ERROR",
    "message": "Agent runtime is not available: ...",
    "traceId": "tr_ab12cd34"
  }
}
```

- `HTTPException` 발생 시: `code = "HTTP_ERROR"`
- pydantic validation 실패 시: `code = "VALIDATION_ERROR"`, 추가 `details` 필드 포함

대표 status codes:

- `400` — 필수 입력 누락 (예: feedback 업로드 파일 없음)
- `404` — 식별자(workspaceId, draftId, fileId, sessionId, jobId 등) 미존재
- `409` — 분석 결과가 아직 준비되지 않은 자원 조회 (feedback)
- `422` — 입력값 검증 실패 (`message must not be empty` 등)
- `502` — agent/AutoSurvey workflow 호출 실패
- `503` — agent runtime을 초기화할 수 없음 (LLM 서버 미준비 등)

### Server-Sent Events (SSE)

스트리밍 엔드포인트 응답은 `text/event-stream`이며 다음 프레임 포맷을 사용:

```
event: <event-name>
data: <json-payload>

```

비어 있는 줄(`\n\n`)이 프레임 경계입니다. 각 엔드포인트의 이벤트 이름과
data 스키마는 카탈로그 항목에 명시됩니다.

---

## 핸들러 비동기 패턴

FastAPI의 동작 특성에 맞춰 두 가지 핸들러 스타일을 의도적으로 구분합니다:

| 스타일 | 사용 시점 |
|---|---|
| `async def` | 빠른 state 읽기/쓰기, SSE 스트림 (StreamingResponse는 sync generator도 threadpool에서 iterate함) |
| 평문 `def` | 동기 블로킹 작업 (LLM 호출, AutoSurvey 실행, ChromaDB/registry 재구축). FastAPI가 자동으로 thread pool에 dispatch하여 event loop를 점유하지 않음 |

따라서 `POST /api/v1/research/jobs`(수 분 단위 AutoSurvey)나 `POST /api/v1/workspaces/switch`(registry 재구축)가 진행 중이어도 `GET /api/v1/research/progress`, `POST /api/v1/screen-monitoring/start` 등 동시 요청이 정상 처리됩니다.

프론트엔드는 `frontend/controllers/job_manager.py`의 `JobManager` 싱글톤을 통해 모든 무거운 요청을 QThread로 디스패치하고, 상호 배제 매트릭스에 따라 충돌하는 작업의 UI 입력을 자동으로 비활성화합니다(예: AutoSurvey 실행 중에는 채팅 입력창이 비활성).

---

## 엔드포인트 카탈로그

### System

#### `GET /`

서비스 식별용 root. 인증 없이 호출 가능한 self-check.

응답:
```json
{ "service": "veritas", "status": "running", "docs": "/docs" }
```

#### `GET /api/v1/health`

응답:
```json
{ "status": "ok", "service": "be" }
```

---

### Frontend bootstrap (`/fe/...`)

프론트엔드 PySide 클라이언트가 부팅·내비게이션·UI state 동기화에 사용하는 헬퍼 엔드포인트 묶음.

#### `GET /api/v1/fe/bootstrap`

쿼리: `userId?` (현재 무시됨, 추후 멀티 사용자 확장 대비 reserve)

응답 (`dict[str, Any]`):
- `workspaces: list[Workspace]` — 사이드바 드롭다운에 사용
- `currentWorkspaceId: str`
- `settings: { model: {...}, localAccess: {...} }`
- `uiState: { route, workspaceId, workspaceName, predictionPopup }`

`Workspace` 스키마:
```json
{
  "workspaceId": "ai_regulation_2026",
  "name": "ai_regulation_2026",
  "detail": "documents 15 · /abs/path",
  "status": "completed",
  "lastWorkedAt": "2026-05-13T08:21:33Z"
}
```

#### `POST /api/v1/fe/actions/navigate`

요청 `{ route: "dashboard" | "research" | "verify" | "draft" | "document_assist" | "write" | "document" | "feedback" | "settings" }`

응답: `{ route, ok: true }` — UI state에 현재 route 기록.

#### `POST /api/v1/fe/actions/workspace-sync`

요청 `{ workspaceId, workspaceName }` — UI state에 표시용 이름 동기화.

#### `POST /api/v1/fe/actions/toast`

`202 Accepted`. 요청 `{ level: "info" | "success" | "warning" | "error", message: str }`. UI state의 `toast` 슬롯에 기록.

#### `POST /api/v1/fe/actions/prediction/show`

요청 `{ predictionId, text, confidence: float, anchor: str }` — 예측 팝업 표시 메타데이터 기록.

#### `POST /api/v1/fe/actions/prediction/hide`

요청 `{ predictionId, reason: str }` — 팝업 비표시 사유 기록.

#### `POST /api/v1/fe/actions/prediction/apply`

요청 `{ predictionId, insertMode: str }` — 사용자가 예측 결과를 적용했음을 기록.

#### `GET /api/v1/fe/state/snapshot`

쿼리: `route?` — 특정 route에 한정해 UI state snapshot을 반환. 디버깅 용도.

---

### Workspaces

#### `GET /api/v1/workspaces`

쿼리: `status?` (예: `completed`, `running`)

응답:
```json
{
  "items": [
    { "workspaceId": "...", "name": "...", "detail": "...", "status": "completed", "lastWorkedAt": "..." }
  ]
}
```

내부적으로 `runs/` 하위 폴더를 스캔해 `final.md` / `summary/index.json` / `summary/doc_*.md` / `summary/request.md` 중 하나라도 있는 디렉토리를 워크스페이스로 인정. `request.md`만 있는 경우(조사가 막 시작되어 아직 final이 없는 상태)는 `status: "running"`으로 표시되어 사이드바 드롭다운에 즉시 나타남. `default` (= `runs/api/`) 는 실제 워크스페이스가 하나라도 있으면 목록에서 제외됨.

#### `POST /api/v1/workspaces/switch`

**비동기**: 평문 `def` — registry/ChromaDB 핸들 재구축이 event loop을 점유하지 않도록 threadpool에 dispatch.

요청 `{ workspaceId: str }`

응답 `{ workspaceId, name }`

부작용:
- `repo.set_current_workspace`, `app_state` 영구 저장
- `AgentRuntime.set_workspace`로 활성 workspace 전환 (chat agent, RAG store 재구축)
- 활성 screen monitoring이 있었다면 새 workspace 기준으로 재시작
- `default` 요청은 실제 최근 워크스페이스로 자동 resolve (phantom `runs/api/` 방지)

#### `DELETE /api/v1/workspaces/{workspaceId}`

**비동기**: 평문 `def` — 폴더 트리 삭제는 잠재적으로 느리므로 thread pool dispatch.

응답:
```json
{
  "workspaceId": "ai_regulation",
  "name": "AI 규제",
  "diskRemoved": true,
  "diskError": null
}
```

부작용:
- 활성 workspace였다면 가장 최근의 다른 워크스페이스(또는 `"default"`)로 자동 전환 후 삭제 (활성 registry를 사용 중인 폴더를 삭제하지 않음)
- `runs/<workspaceId>/` 디렉토리 전체 제거 (`shutil.rmtree`)
- `appdata/VERITAS/veritas.db`의 다음 row 삭제: `workspaces`, `workspace_id`가 일치하는 `documents` · `activity_logs`, `app_state.current_workspace_id`가 가리키고 있었다면 해당 row도 제거
- API 프로세스 메모리의 workspace catalog에서 제외

`diskError`는 디스크 삭제가 실패했을 때만 채워지며 (예: 다른 프로세스가 ChromaDB 파일을 잡고 있는 경우), DB row는 어떤 경우에도 제거되어 dashboard에 더 이상 표시되지 않습니다.

#### 부팅 시 reconcile

이 엔드포인트와는 별도로, `AgentRuntime.__init__`(API 부팅)과 `frontend/ui/main.py`(데스크톱 앱 부팅) 양쪽이 `db.workspace_sync.reconcile_workspaces_with_disk(runs_root)`를 호출합니다. 사용자가 앱 외부에서 `runs/<id>/`를 수동 삭제했어도, 다음 부팅 때 DB의 해당 workspace row(+ 종속 documents/activity_logs)가 자동 정리되어 대시보드 "최근 작업"에 잔존하지 않습니다. 데모 시드(경로가 `runs/` 밖)는 보존됩니다.

---

### Dashboard

#### `GET /api/v1/dashboard/summary`

쿼리: `workspaceId?` — (현재 미사용, reserve)

응답: `{ processedDocs: int, verifiedWorkspaces: int, feedbackCompletionRate: int }`

#### `GET /api/v1/dashboard/recent-workspaces`

쿼리: `limit?` (1~100, 기본 10) — 최근 workspace 목록

#### `GET /api/v1/dashboard/recent-documents`

쿼리: `limit?` (1~100, 기본 10) — 최근 수집된 문서 목록

---

### Research

#### `POST /api/v1/research/jobs`  ·  `201 Created`

**비동기**: 평문 `def` (AutoSurvey 실행은 수 분 단위 — event loop에 두면 다른 요청이 모두 큐잉됨). 프론트엔드는 `JobManager.submit(RESEARCH, ...)`로 호출.

요청:
```json
{
  "workspaceId": "optional-current-workspace-id",
  "instruction": "2026 AI 규제 동향을 산업별로 조사하고 핵심 리스크와 대응 전략을 정리해줘",
  "referenceUrls": ["https://example.com/report"],
  "maxDocs": 15
}
```

- `maxDocs?` (1~50) — AutoSurvey가 수집할 최대 문서 수. 생략하면 `VERITAS_MAX_DOCS` 환경변수 기본값(15)을 사용. 프론트엔드 조사 페이지의 "최대 조사 문서 수" 입력값이 그대로 전달됨.

응답 (workspace 자동 생성 후):
```json
{
  "jobId": "rs_8f3a2b1c",
  "workspaceId": "ai_regulation",
  "workspaceName": "ai_regulation",
  "instruction": "...",
  "referenceUrls": ["..."],
  "maxDocs": 15,
  "status": "completed",
  "submittedAt": "...",
  "completedAt": "...",
  "summary": "최종 보고서 발췌 (최대 6000자)",
  "finalPath": "/abs/path/runs/ai_regulation/final.md",
  "finalMarkdown": "전체 final.md 본문",
  "indexedChunks": 312,
  "documents": [
    {
      "docId": "001",
      "title": "...",
      "url": "https://...",
      "domain": "...",
      "searchQuery": "...",
      "duplicateOf": null
    }
  ],
  "documentCount": 15,
  "nonDuplicateDocumentCount": 14,
  "elapsedSeconds": 184.2,
  "workflowResult": {
    "grounding": {},
    "initial_plan": {},
    "active_plan": {},
    "iteration_count": 3,
    "final_result": {}
  }
}
```

실패 시 `502`와 함께:
```json
{ "error": { "code": "HTTP_ERROR", "message": "AutoSurvey workflow failed: ...", "traceId": "..." } }
```

#### `GET /api/v1/research/jobs`

쿼리: `limit?` (1~100, 기본 10)

응답 `{ items: [Job, ...] }` — `submittedAt` 내림차순. 현재 `runs/` 폴더를 스캔해 트래커에 미등록된 외부 워크스페이스도 자동 동기화 후 반환.

#### `GET /api/v1/research/jobs/{jobId}`

응답: 위 Job 객체. 없으면 `404`.

#### `GET /api/v1/research/progress`

**진행 중인 AutoSurvey의 단계별 이벤트를 polling**하는 엔드포인트. 프론트엔드 `ResearchProgressPoller(QThread)`가 0.8초 간격으로 호출.

쿼리:
- `since: int = 0` — 마지막으로 받은 `seq` (0이면 처음부터)
- `limit: int = 50` (1~500)

응답:
```json
{
  "items": [
    {
      "seq": 12,
      "stage": "doc_fetched",
      "message": "문서 수집 완료: 제목",
      "detail": {
        "doc_id": "003",
        "title": "...",
        "url": "...",
        "final_url": "...",
        "domain": "...",
        "summary_path": "/abs/path/runs/<workspace>/summary/doc_003.md"
      },
      "timestamp": "2026-05-13T08:21:33Z"
    }
  ],
  "nextCursor": 12,
  "latestSeq": 12,
  "activeJob": {
    "jobId": "rs_...",
    "workspaceId": "...",
    "instruction": "...",
    "startedAt": "...",
    "status": "running"
  }
}
```

`stage` 가능 값과 의미:

| stage | 발생 시점 | `detail` 핵심 필드 |
|---|---|---|
| `term_grounding` | 주제어 추출 시작 | — |
| `workspace_created` | term grounding 직후 새 `runs/<id>/` 폴더가 reserve되고 `summary/request.md`가 기록된 직후 | `workspaceId`, `name`, `path` |
| `query_plan` | 검색 계획 생성/재구성 | `mode: "initial" | "replan"` |
| `web_search` | 쿼리별 웹 검색 직전 | `query`, `phase` |
| `fetch_webpage` | URL fetch 직전 | `url`, `title` |
| `doc_fetched` | 중복 아닌 문서 저장 완료 | `doc_id`, `title`, `url`, `final_url`, `domain` |
| `document_summarize` | 요약 배치 시작 | `doc_count`, `doc_ids` |
| `doc_summarized` | 한 문서 요약 완료 | `doc_id`, `summary_path` |
| `final_report` | 최종 보고서 작성 시작 | — |
| `indexing` | RAG 색인 생성 시작 | — |
| `completed` | 전체 종료 | — |

---

### Documents

#### `GET /api/v1/documents/{workspaceId}/summary`

응답 `{ summary: "<final.md markdown 본문>" }`

#### `GET /api/v1/documents/{workspaceId}/merged`

응답 `{ mergedText: "Collected documents\n1. Title\n   url\n..." }`

---

### Verify

#### `GET /api/v1/verify/results`

쿼리: `workspaceId?`, `level?`, `page: int = 1`, `pageSize: int = 10` (1~100)

응답: `{ items: [...], page, pageSize, total }`

#### `GET /api/v1/verify/results/{docId}`

특정 문서 검증 상세. 없으면 `404`.

---

### Chat

채팅은 두 가지 진입점이 있고 **동일한 워크스페이스 메모리**(`<workspace>/memory/memory.sqlite3` — MemoryRuntime FIFO/Recall)를 공유합니다 — 메인 채팅 페이지와 떠 있는 보조 창이 같은 대화를 봅니다. UI 렌더링용 history는 `draft_chat_service.get_chat_history`가 recall tier를 그대로 projection해서 반환합니다. (메모리 도입 이전 워크스페이스의 `chat_history.json`은 워크스페이스를 처음 열 때 한 번 memory.sqlite3로 import되고 `.legacy.json`으로 보관됩니다.)

#### `POST /api/v1/chat/messages`

**비동기**: 평문 `def` (LLM 호출 차단). 동기 호출 결과 한 번에 반환.

요청 `{ workspaceId, message, mode: "research" | "autosurvey" | "rag" }`

응답 `{ messageId, assistant: "<full text>", mode }`

mode 의미:
- `research` → `ChatAgent.ask_auto` (tool decision 포함, autosurvey/rag 등 자유롭게 호출 가능)
- `autosurvey` → `ChatAgent.ask_explicit_tool("autosurvey", ...)` (강제 autosurvey)
- `rag` → `ChatAgent.ask_explicit_tool("rag", ...)` (RAG 검색만)

#### `POST /api/v1/chat/messages/stream`  ·  SSE

요청 본문은 위와 동일. 응답은 `text/event-stream`.

이벤트 시퀀스:

```
event: start
data: { "messageId": "msg_...", "workspaceId": "...", "mode": "research" }

event: delta
data: { "text": "토큰 청크" }

event: delta
data: { "text": "..." }

event: done
data: { "messageId": "msg_...", "assistant": "전체 답변 본문", "mode": "research" }
```

오류 시:

```
event: error
data: { "error": "..." }
```

`done` 후 백엔드는 `(user, assistant)` 한 턴을 워크스페이스 `memory.sqlite3` (FIFO + recall)에 자동 persist — MemoryAwareLLMClient의 `prepare/commit` 라이프사이클 안에서 일어납니다.

#### `GET /api/v1/chat/sessions/{sessionId}/messages`

`sessionId`는 관례적으로 `session_<workspaceId>`.

쿼리: `cursor?` (현재 미사용, reserve)

응답:
```json
{
  "items": [
    { "role": "user", "text": "..." },
    { "role": "assistant", "text": "..." }
  ],
  "nextCursor": null
}
```

---

### Document Assist

문서 보조 창 전용 진입점. 내부적으로 `chat` 파이프라인을 재사용해 채팅 기록을 공유합니다.

#### `POST /api/v1/document-assist/analyze`

**비동기**: 평문 `def`.

요청 `{ workspaceId, text, cursor?: int | null }`

응답:
```json
{
  "sessionId": "da_...",
  "workspaceId": "...",
  "workspaceName": "...",
  "cursor": 42,
  "analysis": "분석 텍스트",
  "warnings": [],
  "recommendations": ["분석 텍스트"],
  "suggestions": [
    { "category": "analysis", "text": "...", "tone": "idle" }
  ],
  "updatedAt": "..."
}
```

#### `POST /api/v1/document-assist/chat/messages`

`/chat/messages`와 동일하지만 응답 키가 `reply` (메인 채팅 페이지와 동일 history를 공유합니다).

요청 `{ workspaceId, message, mode }`
응답 `{ messageId, workspaceId, workspaceName, mode, reply }`

#### `POST /api/v1/document-assist/chat/messages/stream`  ·  SSE

`/chat/messages/stream`와 동일한 SSE 프로토콜.

#### `GET /api/v1/document-assist/sessions/{sessionId}`

`analyze`가 만든 session 스냅샷 조회. 없으면 `404`.

---

### Draft

초안(deliverable 문서)은 `final.md`(조사 결과를 사용자에게 *보고*하는 형식)와 **별개 산출물**입니다. 빌트인 양식 경로는 워크스페이스의 지식베이스(`summary/batch_*.md` + `final.md`)를 근거로, 선택한 양식·목차·톤에 맞춰 실제 문서를 생성합니다. 생성물과 설정은 `runs/<workspace>/drafts/` 아래에 `draft_<n>.md` / `draft_<n>_settings.json` 으로 저장됩니다 (`draft_<n>` 는 에디터 docId 로도 사용 가능).

#### `GET /api/v1/draft/forms`

빌트인 양식 카탈로그(5 대분류 × 3 소분류 + 기본 섹션)와 톤/분량 옵션. 위저드가 렌더링하는 단일 소스.
응답 `{ categories: [...], tones: [{key,label}], defaultTone, lengths: [...], defaultLength }`

#### `POST /api/v1/draft/forms/import`  ·  `multipart/form-data`

업로드한 양식 파일(.docx/.doc/.hwp/.hwpx/.pdf, 평문 포함)에서 **구조만** 추출합니다. 본문 산문은 휴리스틱으로 제거하고 제목·글머리표·표만 남겨 md 템플릿으로 변환합니다. 폼 필드 `files`(공유 업로드 클라이언트가 리스트로 전송 — 첫 파일만 사용).
응답 `{ markdown, outline: [string], format, note }`. 빈 파일은 `400`.

#### `POST /api/v1/draft/builtin/generate`

**비동기**: 평문 `def`. 톤(`격식체`/`중립`/`캐주얼`)을 샘플링 전략(temperature/top_p/top_k …)으로 매핑해 생성.

요청 `{ workspaceId, source: "custom"|"file", category?: {key,label}, subtype?: {key,label}, outline: [string], tone, length, audience, keyPoints, formMarkdown? }`
응답 `{ draftId, draftNumber, title, content, tone, hasKnowledgeBase, settingsFileName, settingsPath, draftFileName, draftPath }`. `outline` 가 비면 `422`. `source="file"` + `formMarkdown` 이면 추출된 양식 템플릿(제목·표 구조)을 따라 생성.

#### `POST /api/v1/draft/builtin/regenerate`

**비동기**: 평문 `def`. 저장된 `draft_<n>_settings.json` 을 다시 읽어 동일 설정으로 같은 번호 위에 재생성.

요청 `{ workspaceId, draftNumber }`
응답 `{ draftId, draftNumber, title, content, ... }` (generate 와 동일). 설정 파일이 없으면 `404`.

#### `GET /api/v1/draft/builtin/list?workspaceId=...`

워크스페이스에 저장된 빌트인 초안(설정 파일) 목록을 번호 내림차순으로 반환.
응답 `{ workspaceId, items: [{ draftNumber, draftId, title, docType, tone, length, updatedAt, settingsFileName }] }`

#### `POST /api/v1/draft/generate`

**비동기**: 평문 `def`. 평문 프롬프트 경로(업로드 양식 폴백).

요청 `{ workspaceId, prompt }`
응답 `{ draftId, title, content }`

#### `POST /api/v1/draft/{draftId}/regenerate`

**비동기**: 평문 `def`.

요청 `{ prompt }`
응답 `{ draftId, content }`. `draftId`가 없으면 `404`.

---

### Feedback

#### `POST /api/v1/feedback/files`  ·  `201 Created`  ·  `multipart/form-data`

폼 필드 `files` 또는 `files[]`로 파일을 1개 이상 첨부. 파일 텍스트는 API 프로세스 메모리에 저장됩니다.

응답:
```json
{ "items": [ { "fileId": "fb_...", "name": "report.pdf", "contentType": "application/pdf" } ] }
```

400: 첨부 없음.

#### `POST /api/v1/feedback/analyze`

**비동기**: 평문 `def`.

요청 `{ fileIds: ["fb_a", "fb_b"] }`
응답 `{ analysisId, status: "completed" }`

저장된 파일별로 agent 분석을 실행하고 결과를 메모리에 저장합니다.

#### `GET /api/v1/feedback/results/{fileId}`

응답:
```json
{
  "fileId": "fb_...",
  "name": "report.pdf",
  "charCount": 12340,
  "lineCount": 187,
  "weakPoints": ["..."],
  "suggestions": ["..."]
}
```

분석 전에 호출 시 `409`.

#### `DELETE /api/v1/feedback/session?sessionId=<id>`  ·  `204 No Content`

세션 캐시 정리.

---

### Write / Predictions

작성 중 prefix/suffix 컨텍스트 기반 inline 예측 SSE 채널.

#### `POST /api/v1/write/typing-context`  ·  `202 Accepted`

요청:
```json
{ "sessionId": "wr_...", "workspaceId": "...", "cursor": 120, "prefix": "이전 문장", "suffix": "이후 문장" }
```

응답: `{ accepted: true, traceId }`

#### `GET /api/v1/write/predictions/stream`  ·  SSE

쿼리: `sessionId`, `workspaceId` (둘 다 필수)

이벤트:

```
event: prediction
data: { "sessionId": "...", "workspaceId": "...", "predictionId": "pr_...", "text": "삽입할 문장", "confidence": 0.0 }
```

저장된 typing context가 비어있거나 LLM 응답이 비면 이벤트 없이 stream을 종료합니다.

#### `POST /api/v1/write/predictions/{predictionId}/ack`

요청 `{ action: "accept" | "dismiss" }`
응답 `{ predictionId, action }`

---

### Settings

#### `GET /api/v1/settings`

응답:
```json
{
  "model": { "modelName": "0.8B" },
  "localAccess": { "folderPaths": ["..."] },
  "documentTools": {
    "custom": [
      { "name": "Obsidian", "identifier": "obsidian.exe" }
    ]
  }
}
```

#### `PUT /api/v1/settings/model`

요청 `{ modelName: "0.8B" | "9B" }`
응답: 갱신된 `model` 객체.

#### `PUT /api/v1/settings/local-access`

요청 `{ folderPaths: ["C:/path", "..."] }`
응답: 갱신된 `localAccess` 객체.

#### `PUT /api/v1/settings/document-tools`

사용자가 "문서 작업 도구"로 인식시키고 싶은 편집기/협업 툴 목록을 저장합니다. 프론트엔드 설정 페이지의 "새로운 문서 작업 도구 추가" 섹션에서 호출.

요청 `{ customTools: [{ name: "Obsidian", identifier?: "obsidian.exe" }, ...] }`
- `name` — 도구 표시 이름 (필수)
- `identifier?` — 프로세스명 또는 URL/제목 키워드 (선택)

응답: 갱신된 `documentTools` 객체. `name`이 빈 항목은 제거되고 (name, identifier) 완전 중복은 합쳐집니다.

---

### Screen Monitoring

사용자의 포어그라운드 윈도우(워드, 파워포인트, 코드 에디터 등) 텍스트를 OCR/UI Automation/app text로 캡처하여 proactive 어시스턴스 답변을 생성합니다.

라이프사이클은 명시적 opt-in: 보조 창이 열릴 때 `start`, 닫힐 때 `stop`을 호출합니다.

#### `POST /api/v1/screen-monitoring/start`

**비동기**: 평문 `def` (모니터 thread 시작 시 runtime을 다시 잡을 수 있어 event loop을 점유하지 않도록 함).

요청 (선택): `{ workspaceId?: str }` — 모니터링을 특정 워크스페이스 기준으로 시작.

응답: 아래 `status` 응답과 동일.

#### `POST /api/v1/screen-monitoring/stop`

응답: status.

#### `GET /api/v1/screen-monitoring/status`

응답:
```json
{
  "registered": true,
  "polling": true,
  "monitoringStartedAt": "2026-05-13T08:21:33Z",
  "workspaceId": "ai_regulation",
  "lastPollError": null,
  "latestCaptureEventId": "20260513_082345_123456",
  "latestCapturedAt": "2026-05-13T08:23:45.123456",
  "latestDiagnostics": {
    "has_foreground_window": true,
    "has_text": true,
    "text_source": "ui_automation",
    "confidence": 0.92,
    "active_text_chars": 1240,
    "current_paragraph_chars": 320,
    "browser_url": null,
    "usable_for_llm": true,
    "intervention_queued": false,
    "intervention_blockers": ["typing_pause"],
    "errors": { "window": null, "app_text": null, "ui_automation": null, "ocr": null }
  },
  "pendingInterventionCount": 0,
  "captureLogPath": "/abs/.../screen_context/capture_logs/2026-05-13.jsonl",
  "eventBufferSize": 3,
  "latestEventSeq": 7
}
```

#### `GET /api/v1/screen-monitoring/events`

쿼리:
- `since: int = 0` — 마지막으로 받은 `seq`
- `limit: int = 20` (1~100)

응답:
```json
{
  "items": [
    {
      "seq": 4,
      "eventId": "20260513_082345_123456",
      "workspaceId": "ai_regulation",
      "answer": "proactive 어시스턴스 답변 본문",
      "category": "proactive",
      "tone": "working",
      "createdAt": "2026-05-13T08:23:46Z",
      "capturedAt": "2026-05-13T08:23:45.123456",
      "triggerText": "사용자가 작성한 직전 문장",
      "appContext": {
        "title": "보고서_초안.docx - Word",
        "processName": "WINWORD.EXE",
        "activeAppType": "document"
      },
      "writingContext": {
        "focusedSentence": "...",
        "recentSentences": "...",
        "paragraphSource": "ui_automation",
        "fullTextChars": 1240,
        "confidence": 0.92
      }
    }
  ],
  "nextCursor": 4,
  "latestSeq": 7,
  "workspaceId": "ai_regulation"
}
```

이벤트 큐는 최근 100개 ring buffer입니다.

---

## 참고

- 상호 배제 모델(예: 조사 중 채팅 차단)은 프론트엔드의 `JobManager`가 담당합니다. 자세한 내용은 루트 `README.md`의 "2026-05-13" 섹션 참조.
- 모든 SSE 응답에는 `Cache-Control: no-cache`, `X-Accel-Buffering: no` 헤더가 붙어 reverse proxy의 버퍼링을 방지합니다.
- 응답 시간이 LLM 서버 처리량에 좌우되므로 SSE 클라이언트는 충분히 큰 timeout(예: 600초)을 사용해야 합니다 — 프론트엔드의 `urllib.request.urlopen` 호출도 동일하게 설정됨.
