# VERITAS API

## 최신 통합 동작

- API agent runtime은 기본적으로 LLM main server를 `127.0.0.1:8080`, embedding server를 `127.0.0.1:8081`로 사용합니다.
- `VERITAS_EMBED_PORT`가 없으면 8081을 사용하므로 RAG indexing이 chat model server 8080으로 embedding 요청을 보내지 않습니다.
- `POST /api/v1/research/jobs`는 AutoSurvey 실행 후 `summary/index.json`을 읽어 `documents`, `documentCount`, `nonDuplicateDocumentCount`, `indexedChunks`, `elapsedSeconds`, `finalPath`, `finalMarkdown`를 반환합니다.
- `GET /api/v1/documents/{workspaceId}/summary`는 최신 AutoSurvey `final.md` 내용을 markdown 문자열로 반환합니다.
- `GET /api/v1/documents/{workspaceId}/merged`는 수집 문서 제목과 링크 목록을 반환합니다.
- 각 AutoSurvey API 실행은 `runs/` 아래 독립 폴더로 저장됩니다. API는 먼저 lightweight term-grounding을 수행해 `grounded_terms` 첫 문자열로 workspace 폴더명을 정한 뒤, 그 최종 폴더에서 workflow와 ChromaDB indexing을 시작합니다.
- `GET /api/v1/workspaces`는 `runs/` 하위 폴더를 스캔해 workspace dropdown에 사용할 목록을 반환합니다.

`api` 폴더는 VERITAS UI와 agent/runtime을 연결하는 FastAPI 백엔드입니다. 현재 연결 가능한 agent 기능만 실제 코드에 붙였고, 연결 가능한 구현이 없는 endpoint는 임의 분석/문서를 만들지 않습니다.

## 실행

프로젝트 루트에서 실행합니다.

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

UI와 함께 실행할 때는 별도 터미널을 사용합니다.

```powershell
# Terminal 1: OpenAI-compatible LLM server를 먼저 실행
# 예: llama-server가 127.0.0.1:8080에서 /v1/chat/completions, /v1/embeddings를 제공해야 함

# Terminal 2: API
cd C:\Users\qwer\Desktop\veritas
python -m api --api --host 127.0.0.1 --port 8000

# Terminal 3: Frontend
cd C:\Users\qwer\Desktop\veritas
python -m frontend.main
```

API와 frontend를 같은 명령으로 동시에 띄우지는 않습니다. API는 `--api`를 붙여 실행하고, PySide frontend는 별도 프로세스로 실행합니다.

## Agent Runtime

API는 agent runtime을 lazy initialize합니다. LLM 서버가 준비되지 않았거나 agent runtime을 만들 수 없으면 임의 응답을 만들지 않고 `503` 또는 `502` 오류를 반환합니다.

환경 변수:

| 변수 | 기본값 | 설명 |
|---|---:|---|
| `VERITAS_OUTPUT_DIR` | `runs/api` | AutoSurvey 결과와 RAG index 저장 위치 |
| `VERITAS_LLM_HOST` | `127.0.0.1` | OpenAI-compatible LLM 서버 host |
| `VERITAS_LLM_PORT` | `8080` | OpenAI-compatible LLM 서버 port |
| `VERITAS_EMBED_HOST` | unset | embedding 서버 host. 없으면 LLM host 사용 |
| `VERITAS_EMBED_PORT` | unset | embedding 서버 port. 없으면 LLM port 사용 |
| `VERITAS_MAX_DOCS` | `15` | AutoSurvey workflow 최대 문서 수 |
| `VERITAS_BATCH_SIZE` | `5` | workflow collect/summarize batch 크기 |
| `VERITAS_SCOUT_DOCS` | `3` | AutoSurvey scout 문서 수 |
| `VERITAS_ENABLE_SCREEN_CONTEXT` | `1` | screen context tool 등록 여부 |
| `VERITAS_SCREEN_INTERVAL` | `5.0` | screen context polling 주기 |

## 연결된 기능

### Chat / Draft

- `POST /api/v1/chat/messages`
  - `mode="research"`: `ChatAgent.ask_auto()`에 연결됩니다.
  - `mode="rag"`: RAG index가 있으면 `ChatAgent.ask_rag()`에 연결됩니다.
- `GET /api/v1/chat/sessions/{sessionId}/messages`
  - API 프로세스 내 chat history를 반환합니다.
- `POST /api/v1/draft/generate`
  - agent에게 초안 작성 요청을 보내고 결과를 draft로 저장합니다.
- `POST /api/v1/draft/{draftId}/regenerate`
  - agent에게 재작성 요청을 보내고 기존 draft를 갱신합니다.

### Research

- `POST /api/v1/research/jobs`
  - 초기 자료조사용 endpoint이므로 채팅용 `autosurvey` tool adapter를 거치지 않고 `AutoSurveyWorkflow.run_all()`을 직접 실행합니다.
  - 사용자 instruction을 기준으로 term grounding, plan, collect, summarize, final report 생성을 수행합니다.
  - `referenceUrls`는 `site:` reference scope로 instruction에 포함되어 workflow가 참고 사이트로 처리할 수 있게 합니다.
  - 실행 결과의 final report excerpt, final path, indexed chunk 수, workflow summary를 job에 저장합니다.
- `GET /api/v1/research/jobs`
- `GET /api/v1/research/jobs/{jobId}`

### Document Assist

- `POST /api/v1/document-assist/analyze`
  - agent에게 문서 검토 요청을 보내고 analysis/suggestion 형태로 반환합니다.
- `POST /api/v1/document-assist/chat/messages`
  - 문서 보조 채팅을 `ChatAgent`에 연결합니다.
- `GET /api/v1/document-assist/sessions/{sessionId}`
  - API 프로세스 내 document assist session을 반환합니다.

### Feedback

- `POST /api/v1/feedback/files`
  - 업로드 파일의 텍스트를 API 프로세스 메모리에 저장합니다.
- `POST /api/v1/feedback/analyze`
  - 저장된 파일 텍스트를 agent에 전달해 피드백 분석 결과를 저장합니다.
- `GET /api/v1/feedback/results/{fileId}`
  - 분석된 결과만 반환합니다. 분석 전이면 `409`를 반환합니다.
- `DELETE /api/v1/feedback/session`

### Write

- `POST /api/v1/write/typing-context`
  - 현재 작성 문맥을 API 프로세스 메모리에 저장합니다.
- `GET /api/v1/write/predictions/stream`
  - 저장된 typing context를 agent에 전달해 실제 예측 문장을 하나 생성하고 SSE로 보냅니다.
- `POST /api/v1/write/predictions/{predictionId}/ack`

## 연결하지 않은 기능

아래 기능은 현재 repo의 agent 구현에 직접 대응되는 실제 backend가 없어 임의 데이터를 만들지 않습니다.

- Dashboard 통계
- Workspace 목록/영구 저장
- Verify 결과
- Documents summary/merged text
- 영구 DB 저장

현재 이 데이터들은 API 프로세스 메모리에 실제 요청으로 생성된 값만 담습니다. 프로세스를 재시작하면 초기화됩니다.

## 공통

- Base URL: `http://127.0.0.1:8000`
- API prefix: `/api/v1`
- JSON 요청: `Content-Type: application/json`
- 파일 업로드: `multipart/form-data`

오류 응답:

```json
{
  "error": {
    "code": "HTTP_ERROR",
    "message": "Agent runtime is not available: ...",
    "traceId": "tr_ab12cd34"
  }
}
```

검증 오류는 `code: "VALIDATION_ERROR"`와 `details`를 포함합니다.
