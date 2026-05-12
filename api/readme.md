# VERITAS API

`api` 폴더는 VERITAS 데스크톱 UI와 연결될 FastAPI 백엔드의 mock API 계층입니다. 현재는 실제 LLM 서버, RAG 엔진, 외부 웹 조사, 영구 저장소와 직접 연결하지 않고 `api_common.STATE` 기반 인메모리 상태로 request/response 계약을 검증합니다.

## 실행

프로젝트 루트(`veritas`)에서 실행합니다.

```powershell
python -m pip install -r requirements.txt
python -m api --api --host 127.0.0.1 --port 8000
```

또는:

```powershell
python -m uvicorn api.api:app --host 127.0.0.1 --port 8000 --reload
```

확인:

- API 문서: `http://127.0.0.1:8000/docs`
- Health check: `http://127.0.0.1:8000/api/v1/health`

## 공통

- Base URL: `http://127.0.0.1:8000`
- API prefix: `/api/v1`
- JSON 요청: `Content-Type: application/json`
- 파일 업로드: `multipart/form-data`
- 현재 상태 저장은 서버 프로세스 메모리 기반입니다. 재시작하면 초기화됩니다.

오류 응답:

```json
{
  "error": {
    "code": "HTTP_ERROR",
    "message": "workspace 'unknown' not found",
    "traceId": "tr_ab12cd34"
  }
}
```

검증 오류는 `code: "VALIDATION_ERROR"`와 `details`를 포함합니다.

## System

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/api/v1/health` | API 상태 확인 |

응답:

```json
{
  "status": "ok",
  "service": "be"
}
```

## Dashboard

| Method | Path | Query | 설명 |
| --- | --- | --- | --- |
| GET | `/api/v1/dashboard/summary` | `workspaceId?` | 대시보드 통계와 최근 작업 목록 |
| GET | `/api/v1/dashboard/recent-workspaces` | `limit=10` | 최근 워크스페이스 목록 |
| GET | `/api/v1/dashboard/recent-documents` | `limit=10` | 최근 문서/피드백 목록 |

`GET /api/v1/dashboard/summary` 응답은 기존 camelCase와 프론트 화면용 snake_case를 함께 제공합니다.

주요 필드:

- `processedDocs`
- `verifiedWorkspaces`
- `feedbackCompletionRate`
- `processed_docs`
- `validated_workspaces`
- `feedback_rate`
- `recent_workspaces`
- `recent_activities`

## Workspaces

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/api/v1/workspaces` | 워크스페이스 목록 조회. `status` query 필터 가능 |
| POST | `/api/v1/workspaces/switch` | 현재 워크스페이스 전환 |

`GET /api/v1/workspaces` 응답 item:

```json
{
  "workspaceId": "ws_001",
  "name": "기후 정책 검증 워크스페이스",
  "detail": "웹 조사 12건 · 검증 완료 8건",
  "status": "verified",
  "lastWorkedAt": "2026-04-08T12:30:00Z"
}
```

`POST /api/v1/workspaces/switch` 요청:

```json
{
  "workspaceId": "ws_001"
}
```

## Settings

설정 화면의 모델 선택과 로컬 접근 폴더 목록에 대응합니다.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/api/v1/settings` | 현재 설정 조회 |
| PUT | `/api/v1/settings/model` | 모델 선택 저장 |
| PUT | `/api/v1/settings/local-access` | 로컬 접근 허용 폴더 목록 저장 |

`PUT /api/v1/settings/model` 요청:

```json
{
  "modelName": "9B"
}
```

`modelName`은 현재 `"0.8B"` 또는 `"9B"`만 허용합니다.

`PUT /api/v1/settings/local-access` 요청:

```json
{
  "folderPaths": [
    "C:/VERITAS/docs",
    "D:/research"
  ]
}
```

응답:

```json
{
  "localAccess": {
    "folderPaths": [
      "C:/VERITAS/docs",
      "D:/research"
    ]
  },
  "updated": true
}
```

중복 폴더 경로는 저장 시 제거됩니다.

## Research

조사 화면의 조사 내용 입력과 레퍼런스 URL 입력에 대응합니다.

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/api/v1/research/jobs` | 조사 작업 생성 |
| GET | `/api/v1/research/jobs` | 조사 작업 목록 조회. `limit` query 지원 |
| GET | `/api/v1/research/jobs/{jobId}` | 조사 작업 상세 조회 |

요청:

```json
{
  "workspaceId": "ws_001",
  "instruction": "2026년 AI 규제 동향을 산업별로 조사해줘.",
  "referenceUrls": [
    "https://example.com/report"
  ]
}
```

`workspaceId`는 생략 가능하며, 생략하면 현재 워크스페이스를 사용합니다.

응답 주요 필드:

- `jobId`
- `workspaceId`
- `workspaceName`
- `instruction`
- `referenceUrls`
- `status`
- `summary`
- `collectedDocuments`

## Verify

검증 화면의 등급 필터, 페이지네이션, 상세 보기 흐름에 대응합니다.

| Method | Path | Query | 설명 |
| --- | --- | --- | --- |
| GET | `/api/v1/verify/results` | `workspaceId?`, `level?`, `page=1`, `pageSize=10` | 검증 결과 목록 |
| GET | `/api/v1/verify/results/{docId}` | - | 검증 결과 상세 |

목록 item:

```json
{
  "docId": "doc_11",
  "title": "AI 안전성 백서",
  "matchRate": 92,
  "level": "높음",
  "issues": [
    "출처 표기 형식 불일치"
  ]
}
```

상세 응답:

```json
{
  "docId": "doc_11",
  "workspaceId": "ws_001",
  "title": "AI 안전성 백서",
  "matchRate": 92,
  "level": "높음",
  "issues": [
    "출처 표기 형식 불일치"
  ]
}
```

## Draft / Chat

초안 생성 화면과 채팅 화면에 대응합니다.

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/api/v1/draft/generate` | 워크스페이스와 프롬프트 기반 초안 생성 |
| POST | `/api/v1/draft/{draftId}/regenerate` | 기존 초안 재생성 |
| POST | `/api/v1/chat/messages` | 워크스페이스 기반 채팅 메시지 전송 |
| GET | `/api/v1/chat/sessions/{sessionId}/messages` | 채팅 이력 조회 |

`POST /api/v1/draft/generate` 요청:

```json
{
  "workspaceId": "ws_001",
  "prompt": "고객 보고용 3문단 초안을 작성해줘."
}
```

`POST /api/v1/chat/messages` 요청:

```json
{
  "workspaceId": "ws_001",
  "message": "근거를 확인해줘.",
  "mode": "rag"
}
```

`mode`는 `"research"` 또는 `"rag"`입니다. 생략하면 `"research"`로 처리합니다.

응답:

```json
{
  "messageId": "msg_ab12cd34",
  "assistant": "저장 문서와 검증 결과를 기준으로 근거를 찾아 답변하겠습니다.",
  "mode": "rag"
}
```

## Documents

문서 화면의 요약본과 스크랩 합본 뷰어에 대응합니다.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/api/v1/documents/{workspaceId}/summary` | 워크스페이스 요약본 조회 |
| GET | `/api/v1/documents/{workspaceId}/merged` | 워크스페이스 스크랩 합본 조회 |

## Feedback

피드백 화면의 파일 업로드, 분석, 결과 확인에 대응합니다.

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/api/v1/feedback/files` | 파일 업로드. form field는 `files` 또는 `files[]` |
| POST | `/api/v1/feedback/analyze` | 업로드된 파일 ID 목록 분석 |
| GET | `/api/v1/feedback/results/{fileId}` | 파일별 피드백 결과 |
| DELETE | `/api/v1/feedback/session` | 피드백 세션 삭제. `sessionId` query 필요 |

`POST /api/v1/feedback/analyze` 요청:

```json
{
  "fileIds": [
    "file_ab12cd34"
  ]
}
```

피드백 결과 주요 필드:

- `fileId`
- `name`
- `charCount`
- `lineCount`
- `weakPoints`
- `suggestions`

## Write

문서 작성 중 실시간 예측 스트림에 대응합니다.

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/api/v1/write/typing-context` | 현재 커서와 주변 문맥 전달 |
| GET | `/api/v1/write/predictions/stream` | SSE 예측 스트림 구독. `sessionId`, `workspaceId` query 필요 |
| POST | `/api/v1/write/predictions/{predictionId}/ack` | 예측 수락/무시 처리 |

`POST /api/v1/write/typing-context` 요청:

```json
{
  "sessionId": "session_ws_001",
  "workspaceId": "ws_001",
  "cursor": 120,
  "prefix": "앞 문맥",
  "suffix": "뒤 문맥"
}
```

`POST /api/v1/write/predictions/{predictionId}/ack` 요청:

```json
{
  "action": "accept"
}
```

`action`은 `"accept"` 또는 `"dismiss"`입니다.

## Document Assist

문서 보조 페이지의 실시간 수정 결과와 AI 보조창의 문서 채팅 흐름에 대응합니다.

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/api/v1/document-assist/analyze` | 문서 분석, 경고, 추천 문장 생성 |
| POST | `/api/v1/document-assist/chat/messages` | 문서 보조 채팅 메시지 전송 |
| GET | `/api/v1/document-assist/sessions/{sessionId}` | 문서 보조 분석 세션 조회 |

`POST /api/v1/document-assist/analyze` 요청:

```json
{
  "workspaceId": "ws_001",
  "text": "분석할 문서 본문",
  "cursor": 42
}
```

응답 주요 필드:

- `sessionId`
- `workspaceId`
- `workspaceName`
- `analysis`
- `warnings`
- `recommendations`
- `suggestions`

`suggestions`는 프론트의 실시간 수정 결과 카드에 바로 사용할 수 있는 형태입니다.

```json
{
  "suggestions": [
    {
      "category": "경고",
      "text": "출처 표기가 보이지 않습니다.",
      "tone": "warning"
    },
    {
      "category": "추천",
      "text": "핵심 주장을 첫 문단 앞쪽에 배치하세요.",
      "tone": "idle"
    }
  ]
}
```

`POST /api/v1/document-assist/chat/messages` 요청:

```json
{
  "workspaceId": "ws_001",
  "message": "이 문단 근거가 충분한지 확인해줘.",
  "mode": "research"
}
```

`mode`는 `"research"` 또는 `"rag"`입니다. 생략하면 `"research"`로 처리합니다.

응답:

```json
{
  "messageId": "msg_ab12cd34",
  "workspaceId": "ws_001",
  "workspaceName": "기후 정책 검증 워크스페이스",
  "mode": "research",
  "reply": "새로 확인할 쟁점과 출처 후보를 먼저 정리한 뒤 답변하겠습니다."
}
```

## Frontend State

프론트 초기화, 라우트 상태, 토스트, 예측 팝업 상태 동기화를 위한 보조 API입니다.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/api/v1/fe/bootstrap` | 메뉴, 워크스페이스, 설정 정보 초기화 |
| POST | `/api/v1/fe/actions/navigate` | 현재 라우트 상태 저장 |
| POST | `/api/v1/fe/actions/workspace-sync` | 프론트 워크스페이스 상태 동기화 |
| POST | `/api/v1/fe/actions/toast` | 토스트 큐잉 |
| POST | `/api/v1/fe/actions/prediction/show` | 예측 팝업 표시 |
| POST | `/api/v1/fe/actions/prediction/hide` | 예측 팝업 숨김 |
| POST | `/api/v1/fe/actions/prediction/apply` | 예측 적용 |
| GET | `/api/v1/fe/state/snapshot` | 현재 프론트 상태 스냅샷 |

`GET /api/v1/fe/bootstrap` 응답에는 다음이 포함됩니다.

- `defaultRoute`
- `menus`
- `workspaces`
- `currentWorkspaceId`
- `settings`

## 현재 한계

- 모든 데이터는 mock/in-memory 상태입니다.
- 인증/인가가 없습니다.
- 파일 업로드 분석은 텍스트 기반 mock 처리입니다.
- 실제 LLM 서버, 웹 조사, RAG, 로컬 폴더 접근 권한 검증, DB 영속화 연동은 아직 연결되어 있지 않습니다.
- AI 서버 연동 예정 API는 현재 request/response 계약 검증용 mock 응답을 반환합니다.
