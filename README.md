# VERITAS

> **로컬에서 동작하는 AI 리서치 · 문서 작성 어시스턴트**
> 웹 자료조사, 보고서 초안 작성, 근거 정합성 검증, 작성 중 능동형 보조까지 — 데이터를 외부로 보내지 않고 내 PC 안에서 처리합니다.

VERITAS는 *Veritas(라틴어 "진실")* 라는 이름 그대로, **수집한 근거에 끝까지 책임지는 글쓰기**를 목표로 합니다. 기본 구성은 로컬 GGUF 모델(llama.cpp)만으로 동작하므로 API 키도, 데이터 유출 걱정도 없습니다. 필요한 경우 자료조사 단계에 한해 OpenAI API를 선택적으로 연결해 속도를 높일 수 있습니다.

---

## ✨ 핵심 가치

- **로컬 우선(Local-first)** — LLM 추론·임베딩·검색·저장이 모두 사용자 PC에서 실행됩니다. 외부로 나가는 것은 자료조사를 위한 웹 검색뿐입니다.
- **데스크톱 앱** — PySide6(Qt6) 기반 GUI와 FastAPI 백엔드가 HTTP로 통신합니다.
- **워크스페이스 중심** — 하나의 주제 = 하나의 워크스페이스. 수집 문서·요약·검증 결과·초안이 한 폴더에 모입니다.
- **내 문서도 근거로** — 로컬 폴더를 연결하면 내 PC의 문서(PDF·DOCX·XLSX 등)도 채팅·초안·검증의 근거로 활용됩니다. 로컬 문서는 외부 API로 절대 전송되지 않습니다.
- **검증 가능한 리서치** — 단순 요약이 아니라, 보고서의 근거 정합성·출처 합의·신뢰도를 알고리즘으로 재검증합니다.

---

## 🚀 주요 기능

| 기능 | 설명 |
|---|---|
| 🔍 **자료조사 (AutoSurvey)** | 주제어 추출 → 검색 계획 → 웹 수집 → 요약 → gap 분석 → 재계획을 반복하는 **자율 리서치 파이프라인**. 결과를 마크다운 보고서(`final.md`)로 산출하고, 진행 상황을 실시간으로 보여줍니다. |
| 💬 **근거 기반 채팅 (RAG)** | 수집한 문서를 ChromaDB에 벡터 색인하고, 그 근거 위에서만 답하는 RAG 채팅. 토큰 단위 스트리밍 응답. |
| 🧠 **스키마 기반 툴 채팅** | 키워드/정규식 라우팅 없이, LLM이 프롬프트와 스키마만 보고 어떤 tool을 호출할지 결정합니다. |
| 📂 **로컬 문서 연결 (Local Corpus)** | 지정한 로컬 폴더의 문서(`.md` `.txt` `.pdf` `.docx` `.xlsx` `.csv`)를 자동 스캔·색인해 RAG·초안·검증의 근거로 활용. 변경된 파일만 증분 재색인하며, 로컬 문서는 외부 LLM으로 전송되지 않습니다. |
| ✅ **정합성 검증 (Verify)** | 보고서를 **임베딩 + IR/NLP 알고리즘**(BM25·RRF·커뮤니티 탐지·PageRank)과 **LLM 신뢰도 판정**으로 재검증. 섹션별 근거, 출처 간 합의/충돌, 문서별 신뢰도를 산출합니다. 로컬 문서가 등록된 경우 **Cross-check**가 내부 문서와 웹 출처를 비교해 수치 불일치·모순을 탐지합니다. |
| 📝 **초안 작성 (Draft)** | 워크스페이스 지식베이스(웹 + 로컬 문서)를 근거로, 선택한 **양식·목차·톤**에 맞춰 실제 문서(주간 보고, 회의록, 사업 제안서 등)를 생성. 기존 양식 파일(.docx/.hwp/.pdf)에서 구조만 추출해 템플릿으로 재사용 가능. |
| 🪄 **Proactive 보조 + 화면 모니터링** | 활성 윈도우(워드·파워포인트·에디터)의 텍스트를 OCR/UI Automation으로 읽어 작성 맥락을 감지하고, **룰 기반 파이프라인**(anchor → CandidateFactory → RuleEvaluator → UserAdaptationMemory)으로 ghostwrite/제안을 먼저 띄웁니다. (Windows 전용) |
| ⌨️ **인라인 문장 예측** | 작성 중 커서 앞뒤 맥락을 바탕으로 다음 문장을 예측해 SSE 스트리밍으로 제안. |
| 🗂 **피드백 분석** | 업로드한 문서(PDF·DOCX·PPTX·HWP 등)를 분석해 약점과 개선안을 제시. |
| 📤 **문서 내보내기** | 산출물을 Markdown / DOCX / HTML / PDF 로 내보내기(pandoc 기반). |
| ⚡ **OpenAI 가속 (선택)** | 자료조사 단계에만 OpenAI API를 선택적으로 사용해 조사 속도를 높일 수 있습니다. 채팅·RAG·임베딩·로컬 문서 처리는 항상 로컬에서 수행됩니다. |
| 📊 **대시보드 · 설정** | 워크스페이스·문서 통계, 모델 라이브 전환, 로컬 접근 폴더 등록, OpenAI API key 관리. |

---

## 🏗 아키텍처

세 개의 진입점이 같은 코어를 공유합니다.

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
   상태:       runs/<workspace>/ · SQLite · ChromaDB
```

### 계층 구성

```
표현(Presentation)   frontend/             PySide6 UI · 컨트롤러 · HTTP 클라이언트
경계(API)            api/                  FastAPI 라우터 · API 서비스
오케스트레이션        agent/  workflows/     대화 루프 / 결정론적 조사 파이프라인
역량(Capability)     tools/                호출 가능한 단위 기능 + ToolRegistry
도메인 서비스         services/             RAG · 로컬 문서 · 검증 · 능동형 제안 · 화면 캡처
인프라(Infra)        llm/  storage/  db/    LLM 클라이언트 / 벡터DB / SQLite
공유(Shared)         core/                 프롬프트(core/prompts/) · 공용 데이터 모델
```

- `frontend/`는 코어를 직접 호출하지 않고 **HTTP로 `api/`만 호출**합니다.
- `api/services/agent_runtime.py`의 `AgentRuntime` 싱글톤이 LLM·tool registry·workflow·chat agent·proactive orchestrator를 들고 있으며 `_workspace_lock`으로 동기화됩니다.
- 긴 작업이 들어가는 FastAPI 핸들러는 **plain `def`**(스레드풀 실행)으로 작성해 이벤트 루프를 막지 않습니다.

### 디렉터리

```
veritas/
├─ main.py                  # CLI 진입점
├─ launcher.py              # 통합 런처 (모델 선택·다운로드 → 서버 기동 → UI 실행)
├─ agent/                   # ChatAgent: 멀티턴 채팅 루프 · 스키마 기반 tool 호출
├─ workflows/               # AutoSurveyWorkflow: 조사 파이프라인
├─ tools/                   # 단위 기능 + ToolRegistry
│                           #   (web_search, fetch_webpage, term_grounding,
│                           #    query_plan, document_summarize, final_report,
│                           #    rag, autosurvey, current_time, screen_context …)
├─ services/                # 도메인 서비스
│  ├─ rag_service.py        #   RAG 색인/검색/근거 기반 답변
│  ├─ local_corpus/         #   로컬 폴더 스캔 · 파싱 · 증분 색인
│  ├─ knowledge/            #   청킹 · 색인 · 검색 · 초안용 지식팩 빌더
│  ├─ proactive/            #   룰 기반 능동형 제안 파이프라인
│  ├─ verification/         #   정합성 검증 (sections · reliability · consensus · crosscheck)
│  ├─ screen_tool_funcs/    #   화면 OCR/UIA 캡처
│  ├─ run_store_tool_funcs/ #   워크스페이스 산출물 저장
│  └─ memory_tools_funcs/   #   working context · FIFO · recall
├─ llm/                     # llama-server 클라이언트 · 모델 카탈로그/다운로드
│                           #   + OpenAI API 어댑터 (AutoSurvey 전용)
├─ storage/                 # ChromaDB 벡터 스토어 래퍼
├─ db/                      # 로컬 SQLite (워크스페이스·문서·활동로그·app_state)
├─ core/                    # 프롬프트(core/prompts/) · 공용 데이터 모델
├─ api/                     # FastAPI 앱
│  ├─ api_routes/           #   기능별 라우터 (research, chat, verify, draft,
│  │                        #     feedback, local_corpus, document_assist,
│  │                        #     write, workspaces, screen-monitoring …)
│  └─ services/             #   라우터 뒤 로직 (agent_runtime 싱글톤 등)
├─ frontend/                # PySide6 데스크톱 앱
│  ├─ controllers/          #   HTTP 클라이언트 · JobManager · ChatBus
│  └─ ui/pages/, ui/windows/#   화면별 페이지 · 플로팅 보조창/에디터
└─ runs/<workspace>/        # 워크스페이스별 산출물 (corpus·summary·local·knowledge·final.md …)
```

---

## 🧰 기술 스택

| 영역 | 사용 기술 |
|---|---|
| **LLM 추론 (로컬)** | [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` (OpenAI 호환 API) · GGUF 양자화 모델 |
| **언어 모델** | Qwen3.5 (0.8B / 2B / 4B / 9B, 사용자 선택) — 채팅 포트 `8080` |
| **임베딩 모델** | Granite Embedding 97M Multilingual R2 — 임베딩 포트 `8081` |
| **LLM 추론 (선택)** | OpenAI API (`gpt-5-mini` 등) — AutoSurvey 조사 단계 전용 |
| **백엔드** | Python 3.13 · [FastAPI](https://fastapi.tiangolo.com/) · Uvicorn |
| **데스크톱 UI** | [PySide6](https://doc.qt.io/qtforpython/) (Qt6) |
| **벡터 검색** | [ChromaDB](https://www.trychroma.com/) (워크스페이스별 PersistentClient) |
| **로컬 메타DB** | SQLite (`%LOCALAPPDATA%/VERITAS/veritas.db`) |
| **웹 리서치** | [ddgs](https://pypi.org/project/ddgs/) (DuckDuckGo 검색) · [Crawl4AI](https://github.com/unclecode/crawl4ai) (HTTP 크롤링 → 정제 마크다운) |
| **검증 / NLP** | NumPy · NetworkX · rank-bm25 · scikit-learn · [Kiwi](https://github.com/bab2min/kiwipiepy) (한국어 형태소 분석) |
| **문서 입출력** | pypdf · python-docx · python-pptx · openpyxl(XLSX) · olefile(HWP) · markdown · [pypandoc](https://pypi.org/project/pypandoc/) |
| **화면 캡처 (Windows)** | pywin32 · uiautomation · winsdk · Pillow |

---

## ⚙️ 설치 & 실행

### 사전 요구사항
- Windows 10/11
- [Miniconda](https://docs.conda.io/projects/miniconda/) 또는 Anaconda
- (선택) pandoc — 문서 내보내기용

### 의존성 설치

본 프로젝트는 conda env **`agent`** (Python 3.13)에서 동작합니다. base env에는 의존성이 없으니 반드시 `agent` env의 인터프리터를 사용하세요.

```powershell
conda create -n agent python=3.13 -y
conda run -n agent python -m pip install -r requirements.txt
```

### 데스크톱 앱 실행 (권장)

```powershell
conda run -n agent python launcher.py
```

런처가 하는 일:

1. **첫 실행 시** 초기 설정 화면을 띄워 Qwen3.5 GGUF 모델을 선택받고, 없는 모델은 Hugging Face에서 진행률과 함께 다운로드합니다.
2. **`llama-server` 2개**를 기동 — 채팅(`8080`) · 임베딩(`8081`).
3. **FastAPI 서버**(`8000`)를 기동.
4. **PySide6 데스크톱 UI**를 띄웁니다.

런처가 종료되면(정상 종료·창 닫기·강제 종료 포함) Windows Job Object가 모든 자식 프로세스를 함께 정리하므로, 포트를 잡고 남는 좀비 `llama-server`가 생기지 않습니다.

디버그 옵션:

```powershell
python launcher.py --console-logs       # 모든 자식 stdout을 콘솔로
python launcher.py --screen-debug       # [screen_debug] 라인만 (화면 캡처 파이프라인)
python launcher.py --proactive-debug    # [proactive] 라인만 (proactive 결정 추적)
```

### FastAPI 백엔드만 단독 실행

```powershell
conda run -n agent python -m api --api --port 8000
```

API 프로세스는 `VERITAS_MANAGE_LLAMA=1`이면 `llama-server` 수명도 직접 관리합니다(설정에서 모델을 바꾸면 재기동 가능).

### CLI 파이프라인 실행

GUI 없이 AutoSurvey/RAG를 그대로 사용합니다. (실행 중인 채팅·임베딩 `llama-server`가 필요합니다.)

```powershell
# 전체 AutoSurvey 후 스키마 기반 채팅
conda run -n agent python main.py "research topic" --output-dir ./output --phase all

# 특정 참조 사이트 강제
conda run -n agent python main.py "research topic site:https://example.com" --output-dir ./output --phase all

# 문서 근거 기반 RAG 채팅
conda run -n agent python main.py --output-dir ./output --phase rag

# AutoSurvey 단계별 실행
conda run -n agent python main.py "research topic" --output-dir ./output --phase plan
conda run -n agent python main.py --output-dir ./output --phase collect
conda run -n agent python main.py --output-dir ./output --phase summarize
conda run -n agent python main.py --output-dir ./output --phase final
```

`--output-dir`은 모든 실행에서 필수입니다.

---

## 🔬 AutoSurvey 파이프라인

```
term_grounding → query_plan(초기) → scout 수집 → 요약
   → gap 분석 → query_plan(재계획) → [수집 → 요약 → 재계획] 반복
   → final_report → ChromaDB 색인(RAG)
```

각 단계는 `tools/`의 tool을 `ToolRegistry`로 호출하고, 진행 이벤트를 `progress_callback`으로 흘려보내 프론트엔드가 실시간으로 렌더링합니다.

내부 tool 역할:

| Tool | 책임 |
|---|---|
| `term_grounding` | LLM이 사용자 요청에서 중요한 리터럴 용어만 추출 (검색 쿼리 생성 X) |
| `query_plan` | LLM이 검색 쿼리·커버리지 포인트 생성 |
| `web_search` | DuckDuckGo HTML 검색 (`ddgs` 폴백). API 키·Docker·외부 서비스 불필요 |
| `fetch_webpage` | Crawl4AI HTTP-only 크롤러로 페이지 → 정제 마크다운 |
| `document_summarize` | `clean_md/`에서 문서별·배치 요약 생성 |
| `final_report` | 최종 마크다운 보고서 산출 |

배치 요약(replan 구동)과 문서별 요약(UX 디스크립터)은 **각각 `clean_md/<doc_id>.md`를 독립적으로 소비**합니다. 배치 요약은 수집 루프 안에서 매 사이클 실행되고, 문서별 요약은 루프 종료 후 1회만 생성됩니다.

### 사이트 제약 (`site:`)

사용자 요청에 `site:` 제약이 있으면 AutoSurvey는 이를 **필수 참조 출처**로 처리합니다.

```
research topic site:https://example.com site:docs.python.org/3/
```

워크플로는 제약을 정규화해 직접 fetch·요약하고, 사이트 한정 검색 쿼리를 주입합니다.

```
site:example.com research topic
site:docs.python.org/3 research topic
```

이는 LLM의 의도 추측이 아니라 사용자가 명시한 출처 제약이므로 의도적으로 워크플로 코드에 구현되어 있습니다.

### Term Grounding 입출력

```json
// 입력
{ "user_request": "...", "max_terms": 8 }

// 출력
{
  "request_language": "ko",
  "grounded_terms": ["..."],
  "candidate_entities": ["..."],
  "disambiguation_notes": ["..."]
}
```

LLM이 사용 불가능하면 규칙 기반 폴백 없이 오류를 반환합니다. 검색 쿼리 생성 책임은 전적으로 `query_plan`에 있습니다.

### Web Fetching

`fetch_webpage_tool.py`는 Crawl4AI의 HTTP-only 전략(`AsyncHTTPCrawlerStrategy`, aiohttp 기반 — Playwright 브라우저 불필요)을 **유일한 fetch 경로**로 사용합니다. 폴백 추출기는 없으며, Crawl4AI가 가져오지 못한 URL은 실패로 보고하고 다음 결과로 넘어갑니다.

```
1. DuckDuckGo가 결과 URL을 반환
2. fetch_webpage가 각 URL을 Crawl4AI HTTP-only로 가져옴
3. DefaultMarkdownGenerator + PruningContentFilter 가 HTML → 정제 마크다운
4. clean_md/<doc_id>.md 로 저장
5. 원본 HTTP 응답 HTML은 corpus/raw_html/<doc_id>.html 에 무절단 보관
6. 실패 URL은 건너뛰고 수집 루프 계속
```

---

## 💬 채팅 & 슬래시 명령

채팅 모드에서 LLM에 노출되는 high-level tool은 다음 3개뿐입니다.

```
current_time   현재 시각 / 타임존 시각
rag_search     워크스페이스 인덱스를 검색해 근거 기반 답변
autosurvey     AutoSurveyWorkflow를 high-level tool로 실행
```

명시적 슬래시 명령은 LLM의 tool 선택을 건너뜁니다.

```
/autosurvey <새 리서치 요청>
/rag <색인된 문서에 대한 질문>
```

프론트엔드의 채팅 모드 선택기도 동일한 강제 경로를 사용합니다 — **자료조사** → `/autosurvey`, **RAG** → `/rag`.

### 채팅 턴 처리

```
1. 현재 사용자 메시지를 수신
2. 채팅 allowlist 스키마를 LLM에 노출
3. LLM이 기본적으로 최대 1개의 tool 호출 여부를 결정
4. ToolRegistry를 통해 선택된 tool 실행
5. LLM이 현재 메시지 + tool 결과로 최종 답변 합성
6. memory runtime이 (user, assistant) 턴을 workspace FIFO + recall로 기록
```

### 채팅 기반 AutoSurvey 캡

채팅에서 호출되는 AutoSurvey는 **새로 수집되는 문서 수**가 의도적으로 제한됩니다.

```
chat autosurvey new-doc cap = 5
CLI AutoSurvey default max_docs = 15
```

채팅 기반 AutoSurvey는 워크스페이스 메모리를 **계획용 preference로만** 활용합니다. `AutoSurveyTool`이 `preference` / `profile` / `constraint` / `project` 라벨이 붙은 working context 레코드로 짧은 `memory_brief`를 만들어 **초기 `QueryPlanTool` 호출에만** 전달합니다. 이 brief는 문서 정제·요약·최종 보고서·근거 채택·RAG 색인에 일절 들어가지 않습니다.

---

## 🧠 Memory Runtime

워크스페이스 메모리는 활성 워크스페이스의 `memory/memory.sqlite3`에 저장됩니다.

| 영역 | 목적 | 프롬프트 동작 |
|---|---|---|
| Working context | 안정적인 사용자 사실/선호 | 채팅에 컴팩트한 컨텍스트로 주입 가능; AutoSurvey는 계획용으로만 선택 카테고리 읽음 |
| FIFO | 최근 대화 턴 | 메모리 인식 채팅에 단기 recency 컨텍스트로 제공 |
| Recall | 검색 가능한 장기 턴 메모리 | 메모리 인식 채팅에서 관련 시 검색 |

`MemoryAwareLLMClient.call()` / `iter_call()` / `call_json()`만 메모리를 prepare·inject·commit 하는 경로입니다. `ask()` / `ask_json()` / `embed()` / `embed_batch()` 같은 raw passthrough는 메모리를 주입하거나 기록하지 않습니다.

AutoSurvey의 source-processing tool은 의도적으로 passthrough만 사용하므로 워크스페이스 메모리가 근거를 오염시킬 수 없습니다.

상세는 [`MEMORY_ARCHITECTURE.md`](MEMORY_ARCHITECTURE.md).

---

## 📂 로컬 문서 연결 (Local Corpus)

내 PC의 문서를 워크스페이스 지식베이스에 연결하는 기능입니다.

1. **설정 → 로컬 접근 폴더 설정**에서 접근을 허용할 폴더를 추가하고 저장합니다.
2. 폴더 안의 지원 문서가 자동으로 스캔·파싱·색인됩니다.
   - 지원 형식: `.md` `.txt` `.pdf` `.docx` `.xlsx` `.csv` (파일당 최대 50MB, 폴더당 최대 300개)
   - 표 형식 파일(CSV/XLSX)은 열 통계·샘플을 요약한 프로필로 변환되어 색인됩니다.
3. 이후 **RAG 채팅 · 초안 작성 · 정합성 검증**에서 웹 자료와 함께 로컬 문서가 근거로 사용됩니다.
4. **표 데이터 질의(`table_query`)** — 채팅에서 로컬 CSV/XLSX의 **수치·집계·정렬 질문**(예: "3월 매출 합계")을 하면, 요약 프로필이 아닌 **원본 파일 전체를 직접 읽어** 행 수 제한 없이 정확한 값을 계산합니다.
5. **Cross-check (내부↔외부 교차 검증)** — 검증 페이지에서 "검증 시작"을 누르면 내부 문서의 주장과 웹 조사 결과를 비교해, 같은 주제를 다루면서 **수치가 다른 항목**을 찾아냅니다. 결과는 "Cross-check 결과" 카드에 불일치 건별로 표시됩니다 (예: "내부 결산: 영업이익 15.8조원 ↔ 외부 발표: 16.4조원").

### 색인 파이프라인

```
폴더 등록 → 스캔(FileScanner) → 파싱(PDF/DOCX/XLSX/CSV/MD/TXT)
   → 청킹 → 임베딩 → ChromaDB 색인 → RAG·초안·검증에서 검색
```

파일 내용 해시를 manifest로 관리하여, 다시 색인할 때 **변경된 파일만** 재처리합니다.

### 프라이버시 보장

로컬 문서에서 추출된 내용은 `local_private` 라벨로 관리되며, OpenAI 가속이 켜져 있어도 **외부 API로 전송되지 않습니다**. 로컬 문서가 근거에 포함되는 작업은 항상 로컬 LLM으로만 수행됩니다 — 코드 레벨에서 차단됩니다.

---

## ⚡ OpenAI 가속 (선택)

자료조사(AutoSurvey)의 속도를 높이고 싶을 때, 조사 파이프라인에 한해 OpenAI API를 사용할 수 있습니다.

- **설정 → 고급 설정 → OpenAI API**에서 API key를 등록하면 활성화되고, 삭제하면 로컬 LLM으로 돌아갑니다.
- 기본 모델은 `gpt-5-mini`이며, 환경 변수로 변경할 수 있습니다.
- OpenAI가 사용되는 범위는 **조사 단계(주제어 추출·검색 계획·문서 요약·최종 보고서)뿐**입니다. 채팅·RAG·임베딩·검증·로컬 문서 처리는 항상 로컬에서 수행됩니다.
- OpenAI 사용 시 문서별 정제(cleanup) LLM 호출이 **사이클당 1회의 배치 메타데이터 호출로 통합**되어, 같은 조사 기준 LLM 호출 수가 약 절반으로 줄어듭니다. (로컬 LLM 사용 시에는 기존 문서별 정제 경로가 그대로 유지됩니다.)

---

## 💾 데이터 위치

하나의 조사 주제 = `runs/` 아래 폴더 하나(= 워크스페이스)입니다.

| 저장소 | 위치 | 내용 |
|---|---|---|
| 워크스페이스 산출물 | `runs/<workspace>/` | 수집 원문, 문서·배치 요약, 검증 결과, `final.md`, 초안 |
| 벡터 인덱스 | `runs/<workspace>/chromadb/` | RAG용 임베딩 (웹 + 로컬 문서) |
| 로컬 문서 색인 | `runs/<workspace>/local/`, `knowledge/` | 로컬 파일 manifest, 추출 텍스트, 표 프로필, 출처 목록 |
| 정합성 검증 | `runs/<workspace>/verification/` | sections / reliability / consensus / `crosscheck.json` |
| Proactive 적응 | `runs/<workspace>/proactive_policy/` | `user_adaptation.json`, append-only `*.jsonl` (원문 미저장) |
| 채팅 메모리 | `runs/<workspace>/memory/memory.sqlite3` | working / FIFO / recall / summary 계층 + `invocations.jsonl` |
| 앱 메타데이터 | `%LOCALAPPDATA%/VERITAS/veritas.db` | 워크스페이스·문서·활동 로그·`app_state` |
| 모델 파일 | `%LOCALAPPDATA%/VERITAS/models/` | GGUF LLM·임베딩 모델 |
| 로그 | `%LOCALAPPDATA%/VERITAS/logs/` | 자식 프로세스 로그 |

`db/workspace_sync.py`가 부팅 시 `runs/` 디스크 폴더와 SQLite 행을 동기화합니다.

---

## 🔧 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `VERITAS_LLM_HOST` / `VERITAS_LLM_PORT` | `127.0.0.1` / `8080` | 채팅 LLM 서버 |
| `VERITAS_EMBED_HOST` / `VERITAS_EMBED_PORT` | LLM과 동일 / `8081` | 임베딩 서버 |
| `VERITAS_API_BASE_URL` | `http://127.0.0.1:8000` | 프론트엔드가 연결할 API 주소 |
| `VERITAS_OUTPUT_DIR` | `runs` | 산출물·인덱스 저장 루트 |
| `VERITAS_LLM_PARALLEL` | `1` | 배치 작업 동시 LLM 요청 수 (llama-server `-np`와 일치시킬 것) |
| `VERITAS_MAX_DOCS` | `15` | AutoSurvey 1회 최대 수집 문서 수 |
| `VERITAS_ENABLE_SCREEN_CONTEXT` | `1` | 화면 모니터링 기능 on/off |
| `VERITAS_MANAGE_LLAMA` | `0` | 1이면 API 프로세스가 llama-server 수명 관리 |
| `VERITAS_PROACTIVE_LOG` | `0` | 1이면 `[proactive]` 로그를 콘솔로 |
| `VERITAS_AUTOSURVEY_LLM_PROVIDER` | `local` | AutoSurvey LLM 백엔드 (`local` / `openai`) |
| `OPENAI_API_KEY` | — | OpenAI 인증 키 (provider가 `openai`일 때 필수) |
| `VERITAS_AUTOSURVEY_OPENAI_MODEL` | `gpt-5-mini` | 사용할 OpenAI 모델 |
| `VERITAS_AUTOSURVEY_OPENAI_SERVICE_TIER` | (자동) | `priority` 설정 시 응답 지연 감소 (추가 비용) |
| `VERITAS_AUTOSURVEY_CLEANUP_MODE` | `auto` | 문서 정제 경로: `auto` / `per_doc` / `batch` |

---

## 🎛 CLI 옵션

| 옵션 | 의미 | 기본값 |
|---|---|---|
| `instruction` | 자연어 리서치 요청/질문 | optional |
| `--output-dir` | 산출물·상태 저장 루트 | **필수** |
| `--host` | llama-server 호스트 | `127.0.0.1` |
| `--port` | llama-server 채팅 포트 | `8080` |
| `--embed-host` | 임베딩 서버 호스트 | 채팅 호스트와 동일 |
| `--embed-port` | 임베딩 서버 포트 | 채팅 포트와 동일 |
| `--parallel` | 배치 작업 동시 LLM 요청 수 (llama-server `-np` 일치) | `VERITAS_LLM_PARALLEL` 또는 `1` |
| `--phase` | `all`, `plan`, `collect`, `summarize`, `final`, `rag`, `chat` | `all` |
| `--max-docs` | CLI AutoSurvey 문서 캡 | `15` |
| `--batch-size` | 수집/요약 배치 크기 | `5` |
| `--scout-docs` | scout-cycle 문서 수 | `3` |
| `--max-context` | 요약 컨텍스트 예산 | `16384` |
| `--rag-results` | RAG 검색 결과 수 | `5` |
| `--force-plan` | 저장된 plan 재사용 대신 재구성 | false |
| `--overwrite-summaries` | 기존 요약 덮어쓰기 | false |
| `--stream-summary` | 문서 요약 호출 스트리밍 | false |
| `--stream-reasoning` | 가능 시 reasoning 콘텐츠 스트리밍 | false |
| `--no-trace-latency` | LLM 지연 로그 비활성화 | false |
| `--markdown-root` | RAG 색인 대상 마크다운 디렉토리 | `--output-dir` |
| `--no-rag` | `--phase all` 완료 후 채팅 스킵 | false |
| `--reindex` | 벡터 인덱스 재구축 | false |
| `--no-screen-context` | 화면 컨텍스트 폴링/proactive 개입 비활성화 | false |
| `--screen-interval` | 채팅 모드 foreground-window 캡처 주기(초) | `5.0` |
| `--screen-debug` / `--screen-debug-log` | 화면 캡처 텍스트 미리보기·개입 결정·드롭·assist 생성 로그 출력 | false |

### RAG 색인 동작 메모

- `--markdown-root`를 생략하면 `--output-dir`을 사용합니다.
- AutoSurvey `clean_md/` 문서가 있고 markdown root가 `--output-dir`이면, **lossy 요약이 아닌 clean Markdown 원문**이 색인되어 RAG 답변의 근거가 됩니다.
- 그 외에는 `--markdown-root` 아래 마크다운 파일이 색인됩니다.
- `--reindex`는 벡터 인덱스를 비우고 재구축합니다.
- `--rag-results`는 `RAGService.n_results`를 제어합니다.
- `--phase rag`는 색인 문서가 없으면 세션이 중단됩니다. `--phase chat`은 채팅이 직접 답할 수 있으므로 경고만 하고 진행합니다.

---

## 🧪 테스트

`unittest`만 사용합니다(pytest 미사용).

```powershell
# 전체
conda run -n agent python -m unittest discover -s tests

# 특정 파일
conda run -n agent python -m unittest tests.test_proactive_evaluator -v
```

규칙:
- 파일명 `tests/test_<topic>.py`, 클래스명 `<Topic>Tests(unittest.TestCase)`
- 외부 의존성(LLM, FastAPI)은 **callable injection**으로 모의 (patching 미사용)
- `tests/bench_*.py`는 벤치마크 — 단위 테스트가 아님
- 공유 데이터는 `tests/fixtures/`

---

## 🧱 설계 불변식 (위반 금지)

코드/회귀 테스트가 강제하는 핵심 규칙입니다.

1. **Tool 선택은 LLM이 한다.** 사용자 메시지의 키워드/정규식으로 tool을 분기하지 않습니다. 코드는 리소스 상한, 허용 tool 경계, 영속화, 결정론적 워크플로 단계만 담당합니다. (예외: `/autosurvey`·`/rag` 슬래시 명령과 `site:` 출처 제약은 의도적인 결정론 우회)
2. **LLM 프롬프트는 `core/prompts/`에 집중**합니다. 도메인/생성기 코드에 인라인 문자열 금지.
3. **로컬 문서(`local_private`)는 외부 API로 절대 전송되지 않습니다** — 코드 레벨에서 차단. 로컬 문서가 근거에 포함되면 OpenAI 가속이 켜져 있어도 로컬 LLM만 사용.
4. **OpenAI는 선택 사항이며 AutoSurvey 전용**입니다 (term grounding, query plan, doc summary, final report). 채팅·RAG·임베딩·검증·로컬 코퍼스 처리는 항상 로컬.
5. **Proactive 가드레일** — `services/proactive/README.md` + 회귀 테스트로 보장:
   - 하드코딩된 어휘 키워드 feature 금지 (`"근거"`/`"출처"` 등 단어 리스트 금지)
   - `services/proactive/legacy_bandit/`를 production 코드에서 import 금지
   - proactive JSONL/JSON에 원본 문서 텍스트 미저장 (char count + anchor hash만; `raw_text_saved` 는 항상 `false`)
6. **검증 레이어 신호는 산출물 텍스트 + 알고리즘**(BM25/RRF/임베딩/그래프)에서 도출하며, 키워드 사전이나 도메인 가정을 코드에 박지 않습니다.

---

## 🛠 변경 레시피 (자주 하는 작업)

- **Tool 추가**: `tools/<name>/tool_schema.json` + `BaseTool` 구현 → `tools/<name>/__init__.py` export → `tools/loader.py`에 등록 → 노출이 필요한 stage allowlist에만 추가. 상세는 [`tools/README.md`](tools/README.md).
- **리서치 파이프라인 변경**: `workflows/autosurvey_workflow.py` (term_grounding → query_plan → collect → summarize → gap/replan → final_report). 각 단계는 `progress_callback`로 이벤트 발신.
- **API 엔드포인트 추가/수정**: `api/api_routes/<feature>.py` 라우터 + `api/services/<feature>_service.py` 로직.
- **데스크톱 화면 추가/수정**: `frontend/ui/pages/` + `frontend/controllers/agent_controller.py`의 HTTP 호출.
- **검증/Cross-check 변경**: 알고리즘은 `services/verification/` (sections · reliability · consensus · crosscheck), API 셰이핑은 `api/services/verify_view.py`, UI는 `frontend/ui/pages/verify_page.py`.
- **Proactive 태스크 타입 추가**: [`ARCHITECTURE.md`](ARCHITECTURE.md)의 9-step 워크스루를 그대로 따르세요 — `proposal_models.TaskType` → `core/prompts/proactive.py` lead-in → `candidates._maybe_*` → `evaluator` 분기 → `generator` 액션 → 회귀 테스트.

---

## 📚 추가 문서

- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — 권위 있는 아키텍처 지도: 계층·스레딩 모델·데이터 흐름·디렉터리 책임·"X를 바꾸려면 어디?" 표 + Proactive 서브시스템 전체 스펙
- **[`MEMORY_ARCHITECTURE.md`](MEMORY_ARCHITECTURE.md)** — 채팅 메모리 계층 (working/FIFO/recall/summary, `memory.sqlite3`, budget, profiles, flush)
- **[`PROACTIVE_RULE.md`](PROACTIVE_RULE.md)** — 룰 기반 proactive 파이프라인 설계 문서
- **디렉터리별 `README.md`** — `tools/`, `services/`, `services/proactive/`, `api/`, `frontend/`, `llm/` 각각 존재
