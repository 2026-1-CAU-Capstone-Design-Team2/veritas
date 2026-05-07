# VERITAS API

`api` 폴더는 VERITAS 데스크톱 UI와 연결될 FastAPI 백엔드의 mock API 계층입니다. 현재는 실제 LLM, RAG, 외부 수집, 영구 저장소와 직접 연결하지 않고, 프론트엔드 화면이 기대하는 역할을 검증할 수 있도록 인메모리 상태(`api_common.STATE`)를 기반으로 동작합니다.

## 역할

- 대시보드, 워크스페이스, 조사, 검증, 초안, 채팅, 문서, 피드백, 문서 보조 화면에 필요한 API 계약 제공
- 실제 서버 연동 전 UI 흐름과 요청/응답 형태 검증
- FastAPI 자동 문서(`/docs`, `/redoc`) 제공
- 공통 에러 응답 래핑

## 폴더 구조

```text
api/
  api.py                 FastAPI app 생성, 미들웨어, 예외 핸들러, 라우터 등록
  main.py                UI/API 실행용 CLI 진입점
  api_models.py          요청 Body 모델
  api_common.py          mock 상태, ID/시간 유틸리티
  api_routes/            FastAPI 라우터
  services/              화면/도메인별 mock 비즈니스 로직
  repositories/          인메모리 상태 접근 계층
```

## 실행 방법

프로젝트 루트(`veritas`)에서 실행합니다.

```powershell
python -m pip install -r requirements.txt
python -m api --api --host 127.0.0.1 --port 8000
```

브라우저에서 확인:

- API 문서: `http://127.0.0.1:8000/docs`
- Health check: `http://127.0.0.1:8000/api/v1/health`

Uvicorn으로 직접 실행할 수도 있습니다.

```powershell
python -m uvicorn api.api:app --host 127.0.0.1 --port 8000 --reload
```

## 공통 사항

- Base URL: `http://127.0.0.1:8000`
- API prefix: `/api/v1`
- Content-Type: JSON 요청은 `application/json`
- 파일 업로드: `multipart/form-data`
- 상태 저장: 서버 프로세스 메모리에만 저장되며 재시작하면 초기화됩니다.

### 에러 응답

`HTTPException`과 요청 검증 오류는 아래 형식으로 반환됩니다.

```json
{
  "error": {
    "code": "HTTP_ERROR",
    "message": "workspace 'unknown' not found",
    "traceId": "tr_ab12cd34"
  }
}
```

검증 오류는 `code`가 `VALIDATION_ERROR`이고 `details`가 포함됩니다.

## API 명세

### System

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/api/v1/health` | API 상태 확인 |

응답 예:

```json
{
  "status": "ok",
  "service": "be"
}
```

### Dashboard

| Method | Path | Query | 설명 |
| --- | --- | --- | --- |
| GET | `/api/v1/dashboard/summary` | `workspaceId?` | 처리 문서 수, 검증 완료 워크스페이스 수, 피드백 완료율 |
| GET | `/api/v1/dashboard/recent-workspaces` | `limit=10` | 최근 워크스페이스 목록 |
| GET | `/api/v1/dashboard/recent-documents` | `limit=10` | 최근 문서/피드백 목록 |

### Workspaces

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/api/v1/workspaces` | 워크스페이스 목록 조회. `status` query로 필터 가능 |
| POST | `/api/v1/workspaces/switch` | 현재 워크스페이스 전환 |

`POST /api/v1/workspaces/switch` 요청:

```json
{
  "workspaceId": "ws_001"
}
```

### Research

조사 화면의 "조사 내용 입력", "레퍼런스 사이트 입력" 흐름에 대응합니다.

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/api/v1/research/jobs` | 조사 작업 생성 |
| GET | `/api/v1/research/jobs` | 조사 작업 목록 조회. `limit` query 지원 |
| GET | `/api/v1/research/jobs/{jobId}` | 조사 작업 상세 조회 |

`POST /api/v1/research/jobs` 요청:

```json
{
  "workspaceId": "ws_001",
  "instruction": "2026년 AI 규제 동향을 산업별로 조사해줘.",
  "referenceUrls": [
    "https://example.com/report"
  ]
}
```

응답 주요 필드:

- `jobId`
- `workspaceId`
- `status`
- `summary`
- `collectedDocuments`

### Verify

검증 화면의 등급 필터와 상세 보기 흐름에 대응합니다.

| Method | Path | Query | 설명 |
| --- | --- | --- | --- |
| GET | `/api/v1/verify/results` | `workspaceId?`, `level?`, `page=1`, `pageSize=10` | 검증 결과 목록 |
| GET | `/api/v1/verify/results/{docId}` | - | 검증 결과 상세 |

`level` 값은 `전체`, `높음`, `중간`, `낮음`을 사용합니다.

### Draft / Chat

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
  "message": "보고용으로 정리해줘."
}
```

### Documents

문서 화면의 요약본/스크랩 합본 뷰어에 대응합니다.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/api/v1/documents/{workspaceId}/summary` | 워크스페이스 요약본 조회 |
| GET | `/api/v1/documents/{workspaceId}/merged` | 워크스페이스 스크랩 합본 조회 |

### Feedback

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
  "fileIds": ["file_ab12cd34"]
}
```

피드백 결과 주요 필드:

- `fileId`
- `name`
- `charCount`
- `lineCount`
- `weakPoints`
- `suggestions`

### Write

문서 작성 중 실시간 예측 스트림에 대응합니다.

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/api/v1/write/typing-context` | 현재 커서와 앞/뒤 문맥 전달 |
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

`action`은 `accept` 또는 `dismiss`입니다.

### Document Assist

문서 보조 페이지와 보조 창의 분석/채팅 흐름에 대응합니다.

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
- `analysis`
- `warnings`
- `recommendations`

### Frontend State

프론트엔드 부트스트랩, 라우팅, 토스트, 예측 팝업 상태 동기화를 위한 보조 API입니다.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/api/v1/fe/bootstrap` | 프론트 초기화에 필요한 메뉴, 워크스페이스, 설정 정보 |
| POST | `/api/v1/fe/actions/navigate` | 현재 라우트 상태 저장 |
| POST | `/api/v1/fe/actions/workspace-sync` | 프론트 워크스페이스 상태 동기화 |
| POST | `/api/v1/fe/actions/toast` | 토스트 큐잉 |
| POST | `/api/v1/fe/actions/prediction/show` | 예측 팝업 표시 |
| POST | `/api/v1/fe/actions/prediction/hide` | 예측 팝업 숨김 |
| POST | `/api/v1/fe/actions/prediction/apply` | 예측 적용 |
| GET | `/api/v1/fe/state/snapshot` | 현재 프론트 상태 스냅샷 |

## 현재 한계

- 모든 데이터는 mock/in-memory 상태입니다.
- 인증/인가가 없습니다.
- 파일 업로드는 텍스트 기반 mock 분석만 수행합니다.
- LLM, 외부 웹 조사, RAG, 데이터베이스 연동은 아직 연결되어 있지 않습니다.
