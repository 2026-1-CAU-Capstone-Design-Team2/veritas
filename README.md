# VERITAS

> **로컬에서 동작하는 AI 리서치 · 문서 작성 어시스턴트**
> 웹 자료조사, 보고서 초안 작성, 근거 정합성 검증, 작성 중 능동형 보조까지 — 데이터를 외부로 보내지 않고 내 PC 안에서 처리합니다.

VERITAS는 *Veritas(라틴어 "진실")* 라는 이름 그대로, **수집한 근거에 끝까지 책임지는 글쓰기**를 목표로 합니다. 기본 구성은 로컬 GGUF 모델(llama.cpp)만으로 동작하므로 API 키도, 데이터 유출 걱정도 없습니다. 필요한 경우에만 자료조사 단계에 한해 OpenAI API를 선택적으로 연결해 속도를 높일 수 있습니다.

---

## 👥 프로젝트 정보

VERITAS는 **2026-1 중앙대학교 캡스톤 디자인2** 프로젝트입니다.

<table>
  <tr align="center">
    <td>박상우</td>
    <td>이정재</td>
    <td>조상원</td>
  </tr>
  <tr align="center">
    <td><a target="_blank" href="https://github.com/sweetholly"><img src="https://avatars.githubusercontent.com/u/80771455?v=4" width="100px"><br>@sweetholly</a>  </td>
    <td><a target="_blank" href="https://github.com/lee1026td"><img src="https://avatars.githubusercontent.com/u/29156773?v=4" width="100px"><br>@lee1026td</a></td>
    <td><a target="_blank" href="https://github.com/sangwon5579"><img src="https://avatars.githubusercontent.com/u/81066249?v=4" width="100px"><br>@sangwon5579</a> </td>
  </tr>
</table>

---

## 📌 한눈에 보기

- **로컬 우선(Local-first)** — LLM 추론·임베딩·검색·저장이 모두 사용자 PC에서 실행됩니다. 외부로 나가는 것은 자료조사를 위한 웹 검색뿐입니다.
- **데스크톱 앱** — PySide6(Qt6) 기반 GUI와 FastAPI 백엔드가 HTTP로 통신합니다.
- **워크스페이스 중심** — 하나의 주제 = 하나의 워크스페이스. 수집 문서·요약·검증 결과·초안이 한 폴더에 모입니다.
- **내 문서도 근거로** — 로컬 폴더를 연결하면 내 PC의 문서(PDF·DOCX·XLSX 등)도 채팅·초안·검증의 근거로 활용됩니다. 로컬 문서는 외부 API로 절대 전송되지 않습니다.
- **검증 가능한 리서치** — 단순 요약이 아니라, 보고서의 근거 정합성·출처 합의·신뢰도를 알고리즘으로 재검증합니다.

---

## ✨ 핵심 기능

| 기능 | 설명 |
|---|---|
| 🔍 **자료조사 (AutoSurvey)** | 주제어 추출 → 검색 계획 → 웹 수집 → 요약 → 부족분(gap) 분석 → 재계획을 반복하는 **자율 리서치 파이프라인**. 결과를 마크다운 보고서(`final.md`)로 산출하고, 진행 상황을 실시간으로 보여줍니다. |
| 💬 **근거 기반 채팅 (RAG)** | 수집한 문서를 ChromaDB에 벡터 색인하고, 그 근거 위에서만 답하는 RAG 채팅. 토큰 단위 스트리밍 응답. |
| 📂 **로컬 문서 연결 (Local Corpus)** | 지정한 로컬 폴더의 문서(`.md` `.txt` `.pdf` `.docx` `.xlsx` `.csv`)를 자동 스캔·색인해 RAG·초안·검증의 근거로 활용. 파일이 바뀐 부분만 증분 재색인하며, 로컬 문서는 외부 LLM으로 전송되지 않습니다. |
| ✅ **정합성 검증 (Verify)** | 보고서를 **임베딩 + IR/NLP 알고리즘**(BM25·RRF·커뮤니티 탐지·PageRank)과 **LLM 신뢰도 판정**으로 재검증. 섹션별 근거, 출처 간 합의/충돌, 문서별 신뢰도를 산출합니다. 로컬 문서가 등록된 경우 **Cross-check**가 내부 문서와 웹 출처를 비교해 수치 불일치·모순을 탐지하고, 검증 페이지에 불일치 건별로 "내부 주장 vs 외부 주장 + 출처"를 표시합니다. |
| 📝 **초안 작성 (Draft)** | 워크스페이스 지식베이스(웹 + 로컬 문서)를 근거로, 선택한 **양식·목차·톤**에 맞춰 실제 문서(주간 보고, 회의록, 사업 제안서 등)를 생성. 기존 양식 파일(.docx/.hwp/.pdf)에서 구조만 추출해 템플릿으로 재사용 가능. |
| 🪄 **AI 보조창 + 화면 모니터링** | 떠 있는 보조 창이 활성 윈도우(워드·파워포인트·에디터)의 텍스트를 OCR/UI Automation으로 읽어, 작성 맥락을 감지하고 **룰 기반 파이프라인**으로 먼저 제안합니다. (Windows 전용) |
| ⌨️ **인라인 문장 예측** | 작성 중 커서 앞뒤 맥락을 바탕으로 다음 문장을 예측해 제안(SSE). |
| 🗂 **피드백 분석** | 업로드한 문서(PDF·DOCX·PPTX·HWP 등)를 분석해 약점과 개선안을 제시. |
| 📤 **문서 내보내기** | 산출물을 Markdown / DOCX / HTML / PDF 로 내보내기(pandoc 기반). |
| ⚡ **OpenAI 가속 (선택)** | 자료조사(AutoSurvey) 단계에만 OpenAI API를 선택적으로 사용해 조사 속도를 높일 수 있습니다. 채팅·RAG·임베딩·로컬 문서 처리는 항상 로컬에서 수행됩니다. |
| 📊 **대시보드 · 설정** | 워크스페이스·문서 통계, 모델 라이브 전환, 로컬 접근 폴더 등록, OpenAI API key 관리. |

---

## 🧰 기술 스택

| 영역 | 사용 기술 |
|---|---|
| **LLM 추론 (로컬)** | [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` (OpenAI 호환 API) · GGUF 양자화 모델 |
| **언어 모델** | Qwen3.5 (0.8B / 2B / 4B / 9B, 사용자 선택) — 채팅 포트 `8080` |
| **임베딩 모델** | Granite Embedding 97M Multilingual R2 — 임베딩 포트 `8081` |
| **LLM 추론 (선택)** | OpenAI API (`gpt-5-mini` 등) — AutoSurvey 조사 단계 전용 |
| **백엔드** | Python · [FastAPI](https://fastapi.tiangolo.com/) · Uvicorn |
| **데스크톱 UI** | [PySide6](https://doc.qt.io/qtforpython/) (Qt6) |
| **벡터 검색** | [ChromaDB](https://www.trychroma.com/) (워크스페이스별 PersistentClient) |
| **로컬 메타DB** | SQLite (`%LOCALAPPDATA%/VERITAS/veritas.db`) |
| **웹 리서치** | [ddgs](https://pypi.org/project/ddgs/) (DuckDuckGo 검색) · [Crawl4AI](https://github.com/unclecode/crawl4ai) (HTTP 크롤링 → 정제 마크다운) |
| **검증 / NLP** | NumPy · NetworkX · rank-bm25 · scikit-learn · [Kiwi](https://github.com/bab2min/kiwipiepy) (한국어 형태소 분석) |
| **문서 입출력** | pypdf · python-docx · python-pptx · openpyxl(XLSX) · olefile(HWP) · markdown · [pypandoc](https://pypi.org/project/pypandoc/) |
| **화면 캡처 (Windows)** | pywin32 · uiautomation · winsdk · Pillow |

---

## 🏗 시스템 구조

VERITAS는 **3개의 진입점**이 **하나의 공유 코어**를 사용하는 구조입니다.

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

- **`frontend/`** — PySide6 데스크톱 앱. 코어를 직접 호출하지 않고 **HTTP로 `api/`를 호출**합니다.
- **`api/`** — FastAPI 서버. `AgentRuntime` 싱글톤이 LLM·툴 레지스트리·워크플로·채팅 에이전트를 보유합니다.
- **`main.py`** — 동일 코어를 쓰는 CLI 진입점(개발/디버깅·배치 실행용).

### 계층(Layer)

```
표현(Presentation)   frontend/             PySide6 UI · 컨트롤러 · HTTP 클라이언트
경계(API)            api/                  FastAPI 라우터 · API 서비스 · 리포지토리
오케스트레이션        agent/  workflows/     대화 루프 / 결정론적 조사 파이프라인
역량(Capability)     tools/                호출 가능한 단위 기능 + ToolRegistry
도메인 서비스         services/             RAG · 로컬 문서 · 검증 · 능동형 제안 · 화면 캡처
인프라(Infra)        llm/  storage/  db/    LLM 클라이언트 / 벡터DB / SQLite
공유(Shared)         core/                 프롬프트 · 공용 데이터 모델
```

> 핵심 어휘: **Tool**(실행 가능한 단위 기능) · **Workflow**(tool을 묶은 결정론적 파이프라인) · **Service**(상태/비즈니스 로직 소유자) · **Agent**(LLM과 툴을 잇는 대화 루프).
> 더 자세한 구조와 import 규칙은 [`ARCHITECTURE.md`](ARCHITECTURE.md) 참고.

### AutoSurvey 파이프라인

```
term_grounding → query_plan(초기) → scout 수집 → 요약
   → gap 분석 → query_plan(재계획) → [수집 → 요약 → 재계획] 반복
   → final_report → ChromaDB 색인(RAG)
```

각 단계는 `tools/`의 tool을 `ToolRegistry`로 호출하고, 진행 이벤트를 콜백으로 흘려보내 프론트엔드가 실시간으로 렌더링합니다.

### 로컬 문서 색인 파이프라인

```
폴더 등록 → 스캔(FileScanner) → 파싱(PDF/DOCX/XLSX/CSV/MD/TXT)
   → 청킹 → 임베딩 → ChromaDB 색인 → RAG·초안·검증에서 검색
```

파일 내용의 해시를 manifest로 관리하여, 다시 색인할 때 **변경된 파일만** 재처리합니다.

---

## 📁 디렉터리 구조

```
veritas/
├─ main.py                  # CLI 진입점
├─ launcher.py              # 통합 런처 (모델 선택·다운로드 → 서버 기동 → UI 실행)
├─ agent/                   # ChatAgent: 멀티턴 채팅 루프 · 스키마 기반 tool 호출
├─ workflows/               # AutoSurveyWorkflow: 조사 파이프라인
├─ tools/                   # 단위 기능 + ToolRegistry (web_search, fetch_webpage,
│                           #   term_grounding, query_plan, document_summarize,
│                           #   final_report, rag, autosurvey, screen_context …)
├─ services/                # 도메인 서비스
│  ├─ rag_service.py        #   RAG 색인/검색/근거 기반 답변
│  ├─ local_corpus/         #   로컬 폴더 스캔 · 파싱 · 증분 색인
│  ├─ knowledge/            #   청킹 · 색인 · 검색 · 초안용 지식팩 빌더
│  ├─ run_store_tool_funcs/ #   워크스페이스 산출물 저장
│  ├─ fetch_webpage_tool_funcs/  # Crawl4AI 수집
│  ├─ screen_tool_funcs/    #   화면 OCR/UIA 캡처
│  ├─ proactive/            #   룰 기반 능동형 제안 파이프라인
│  └─ verification/         #   정합성 검증 (sections · reliability · consensus · crosscheck)
├─ llm/                     # llama-server 클라이언트 · 모델 카탈로그/다운로드
│                           #   + OpenAI API 어댑터 (AutoSurvey 전용)
├─ storage/                 # ChromaDB 벡터 스토어 래퍼
├─ db/                      # 로컬 SQLite (워크스페이스·문서·활동로그·app_state)
├─ core/                    # 프롬프트(core/prompts/) · 공용 데이터 모델
├─ api/                     # FastAPI 앱
│  ├─ api_routes/           #   기능별 라우터 (research, verify, draft, feedback,
│  │                        #     local_corpus, document_assist, write, workspaces …)
│  └─ services/             #   라우터 뒤 로직 (agent_runtime 싱글톤 등)
├─ frontend/                # PySide6 데스크톱 앱
│  ├─ controllers/          #   HTTP 클라이언트 · JobManager · ChatBus
│  └─ ui/pages/, ui/windows/#   화면별 페이지 · 플로팅 보조창/에디터
└─ runs/<workspace>/        # 워크스페이스별 산출물 (corpus·summary·local·knowledge·final.md …)
```

각 디렉터리에는 자체 `README.md`가 있어 세부 동작을 설명합니다. (`agent/`, `api/`, `core/`, `db/` 등)

---

## 🚀 시작하기

### 요구 사항

- **OS**: Windows 우선 (화면 모니터링·통합 런처는 Windows 전용 의존성을 사용)
- **Python**: 3.10+
- **`llama-server` 실행 파일**: llama.cpp 빌드 산출물. `bin/llama-server.exe` 에 두거나 환경 변수 `VERITAS_LLAMA_SERVER_BIN` 으로 경로 지정.
- **디스크**: 선택 모델에 따라 0.6GB~6GB (GGUF 모델 다운로드 용량)

### 설치

```powershell
python -m pip install -r requirements.txt
```

> PDF 내보내기는 별도의 PDF 엔진이 필요합니다 — `conda install -c conda-forge wkhtmltopdf` 또는 xelatex/pdflatex 가 있는 TeX 배포판.

### 실행 — ① 통합 런처(권장)

```powershell
python launcher.py
```

런처가 다음을 순서대로 처리합니다.

1. **첫 실행 시** 초기 설정 화면을 띄워 Qwen3.5 GGUF 모델을 선택받고, 없는 모델은 Hugging Face에서 진행률과 함께 다운로드합니다.
2. **`llama-server` 2개**를 기동 — 채팅(`8080`) · 임베딩(`8081`).
3. **FastAPI 서버**(`8000`)를 기동.
4. **PySide6 데스크톱 UI**를 띄웁니다.

런처가 종료되면(정상 종료·창 닫기·강제 종료 포함) Windows Job Object가 모든 자식 프로세스를 함께 정리하므로, 포트를 잡고 남는 좀비 `llama-server`가 생기지 않습니다.

개발 중 로그를 한 터미널에서 보려면:

```powershell
python launcher.py --console-logs
```

### 실행 — ② 구성 요소 수동 기동(개발자)

각 `llama-server`를 직접 띄운 뒤:

```powershell
# API 서버
python -m api --api --host 127.0.0.1 --port 8000

# 데스크톱 UI (별도 터미널)
python -m frontend.main
```

---

## 📂 로컬 문서 연결 (Local Corpus)

내 PC의 문서를 워크스페이스 지식베이스에 연결하는 기능입니다.

1. **설정 → 로컬 접근 폴더 설정**에서 접근을 허용할 폴더를 추가하고 저장합니다.
2. 폴더 안의 지원 문서가 자동으로 스캔·파싱·색인됩니다.
   - 지원 형식: `.md` `.txt` `.pdf` `.docx` `.xlsx` `.csv` (파일당 최대 50MB, 폴더당 최대 300개)
   - 표 형식 파일(CSV/XLSX)은 열 통계·샘플을 요약한 프로필로 변환되어 색인됩니다.
3. 이후 **RAG 채팅 · 초안 작성 · 정합성 검증**에서 웹 자료와 함께 로컬 문서가 근거로 사용됩니다.
4. 표 데이터 질의(`table_query`) — 채팅에서 로컬 CSV/XLSX의 **수치·집계·정렬 질문**(예: "3월 매출 합계")을 하면, 요약 프로필이 아닌 **원본 파일 전체를 직접 읽어** 행 수 제한 없이 정확한 값을 계산합니다.
5. **Cross-check (내부↔외부 교차 검증)** — 검증 페이지에서 "검증 시작"을 누르면 내부 문서의 주장과 웹 조사 결과를 비교해, 같은 주제를 다루면서 **수치가 다른 항목**을 찾아냅니다. 결과는 검증 페이지의 "Cross-check 결과" 카드에 불일치 건별로 표시됩니다 (예: "내부 결산: 영업이익 15.8조원 ↔ 외부 발표: 16.4조원").

**프라이버시 보장** — 로컬 문서에서 추출된 내용은 `local_private` 라벨로 관리되며, OpenAI 가속이 켜져 있어도 **외부 API로 전송되지 않습니다**. 로컬 문서가 근거에 포함되는 작업은 항상 로컬 LLM으로만 수행됩니다.

---

## ⚡ OpenAI 가속 (선택)

자료조사(AutoSurvey)의 속도를 높이고 싶을 때, 조사 파이프라인에 한해 OpenAI API를 사용할 수 있습니다.

- **설정 → 고급 설정 → OpenAI API**에서 API key를 등록하면 활성화되고, 삭제하면 로컬 LLM으로 돌아갑니다.
- 기본 모델은 `gpt-5-mini`이며, 환경 변수로 변경할 수 있습니다.
- OpenAI가 사용되는 범위는 **조사 단계(주제어 추출·검색 계획·문서 요약·최종 보고서)뿐**입니다. 채팅·RAG·임베딩·검증·로컬 문서 처리는 항상 로컬에서 수행됩니다.
- OpenAI 사용 시 문서별 정제(cleanup) LLM 호출이 **사이클당 1회의 배치 메타데이터 호출로 통합**되어, 같은 조사 기준 LLM 호출 수가 약 절반으로 줄어듭니다. (로컬 LLM 사용 시에는 기존 문서별 정제 경로가 그대로 유지됩니다.)

| 환경 변수 | 기본값 | 설명 |
|---|---|---|
| `VERITAS_AUTOSURVEY_LLM_PROVIDER` | `local` | `openai` 로 설정 시 AutoSurvey에 OpenAI 사용 |
| `OPENAI_API_KEY` | — | OpenAI 인증 키 (provider가 `openai`일 때 필수) |
| `VERITAS_AUTOSURVEY_OPENAI_MODEL` | `gpt-5-mini` | 사용할 OpenAI 모델 |
| `VERITAS_AUTOSURVEY_OPENAI_SERVICE_TIER` | (자동) | `priority` 설정 시 응답 지연 감소 (추가 비용 발생) |
| `VERITAS_AUTOSURVEY_CLEANUP_MODE` | `auto` | 문서 정제 경로: `auto`(LLM 종류로 자동 결정) / `per_doc`(문서별 정제) / `batch`(사이클당 배치 처리) |

---

## 💾 워크스페이스 & 데이터 위치

하나의 조사 주제 = `runs/` 아래 폴더 하나(= 워크스페이스)입니다.

| 저장소 | 위치 | 내용 |
|---|---|---|
| 워크스페이스 산출물 | `runs/<workspace>/` | 수집 원문, 문서·배치 요약, 검증 결과, `final.md`, 초안 |
| 로컬 문서 색인 | `runs/<workspace>/local/`, `knowledge/` | 로컬 파일 manifest, 추출 텍스트, 표 프로필, 출처 목록 |
| 벡터 인덱스 | `runs/<workspace>/chromadb/` | RAG용 임베딩 (웹 + 로컬 문서) |
| 앱 메타데이터 | `%LOCALAPPDATA%/VERITAS/veritas.db` | 워크스페이스·문서·활동 로그·현재 상태 |
| 모델 파일 | `%LOCALAPPDATA%/VERITAS/models/` | GGUF LLM·임베딩 모델 |
| 로그 | `%LOCALAPPDATA%/VERITAS/logs/` | 자식 프로세스 로그 |

디스크의 `runs/` 폴더와 SQLite는 앱 부팅 시 자동으로 동기화됩니다 — 앱 밖에서 폴더를 지워도 대시보드에 잔존하지 않습니다.

---

## 🖥 CLI 사용법 (개발자용)

데스크톱 UI 없이 코어 워크플로만 돌릴 수 있습니다. `--output-dir`은 필수입니다.

```powershell
# 전체 AutoSurvey 실행 후, 스키마 기반 채팅으로 진입
python main.py "조사할 주제" --output-dir ./output --phase all

# 특정 참고 사이트를 강제로 포함
python main.py "조사할 주제 site:https://example.com" --output-dir ./output --phase all

# 일반 채팅 (LLM이 tool 사용 여부를 스스로 결정)
python main.py --output-dir ./output --phase chat

# 문서 근거에 엄격히 한정된 RAG 채팅
python main.py --output-dir ./output --phase rag
```

`--phase`는 `all` 외에 `plan` / `collect` / `summarize` / `final` / `rag` / `chat` 단계를 개별 실행할 수 있습니다. 전체 CLI 옵션은 `python main.py --help` 참고.

### 주요 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `VERITAS_LLM_HOST` / `VERITAS_LLM_PORT` | `127.0.0.1` / `8080` | 채팅 LLM 서버 |
| `VERITAS_EMBED_HOST` / `VERITAS_EMBED_PORT` | LLM과 동일 / `8081` | 임베딩 서버 |
| `VERITAS_API_BASE_URL` | `http://127.0.0.1:8000` | 프론트엔드가 연결할 API 주소 |
| `VERITAS_OUTPUT_DIR` | `runs` | 산출물·인덱스 저장 루트 |
| `VERITAS_LLM_PARALLEL` | `1` | 배치 작업 동시 LLM 요청 수 (llama-server `-np`와 일치시킬 것) |
| `VERITAS_MAX_DOCS` | `15` | AutoSurvey 1회 최대 수집 문서 수 |
| `VERITAS_ENABLE_SCREEN_CONTEXT` | `1` | 화면 모니터링 기능 on/off |
| `VERITAS_AUTOSURVEY_LLM_PROVIDER` | `local` | AutoSurvey LLM 백엔드 (`local` / `openai`) |

전체 환경 변수 및 REST 엔드포인트 명세는 [`api/README.md`](api/README.md)에 정리되어 있습니다.

---

## 📐 설계 원칙

> **의도 판단은 LLM(프롬프트·스키마)의 몫, 코드는 실행 경계만 강제한다.**

- 사용자 메시지의 키워드/정규식으로 tool을 분기하지 않습니다. 어떤 tool을 쓸지는 LLM이 프롬프트와 스키마를 보고 결정합니다.
- 코드는 리소스 상한, 허용 tool 경계, 영속화, 결정론적 워크플로 단계를 담당합니다.
- 모든 프롬프트는 `core/prompts/`에 중앙화되어 있습니다(코드에 인라인 금지).
- 정합성 검증 레이어는 외부 키워드 사전·도메인 가정을 코드에 박지 않고, 신호를 산출물 텍스트와 알고리즘에서 도출합니다.
- 로컬 문서(`local_private`)는 어떤 경우에도 외부 API로 전송하지 않습니다 — 코드 레벨에서 차단됩니다.

---
