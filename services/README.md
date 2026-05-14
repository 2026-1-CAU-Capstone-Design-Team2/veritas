# services/

## 최신 RAG/문서 표시 동작

- AutoSurvey 완료 후 API runtime은 `RAGService.index_autosurvey_output(clean_md_dir, index_path)`를 호출해 `clean_md/<doc_id>.md`(요약이 아닌 Crawl4AI 정제 원문)를 chunking하고 embedding server에 batch embedding을 요청합니다. 요약은 lossy하므로 RAG 답변 근거는 clean_md에서 가져옵니다.
- API/CLI 기본 embedding endpoint는 `127.0.0.1:8081/v1/embeddings`입니다.
- `RunStoreService.index_path`는 `summary/index.json`이며, UI 조사 결과의 문서 제목/링크/문서 수는 이 파일의 records를 기준으로 표시됩니다.
- `RunStoreService.final_path`는 `final.md`이며, UI 문서 화면의 요약본은 이 markdown 내용을 API가 저장한 workspace document state에서 읽어 표시합니다.
- API research runtime은 workflow 시작 전에 lightweight term-grounding으로 workspace 이름을 정하고, 곧바로 `runs/<workspace>` 폴더에 `RunStoreService`와 ChromaDB를 생성합니다. Windows에서 열린 `chroma.sqlite3` 때문에 폴더 이동이 실패하지 않도록 pending 폴더 이동을 사용하지 않습니다.

> RAG service update: `services/rag_service.py` owns indexing, retrieval, query rewriting, and document-grounded answer generation. `tools/rag_tool/` is a thin `rag_search` adapter for LLM tool-calling only.

**역할**: 도구(Tool)들이 공유하는 비즈니스 로직, 상태 관리, 유틸리티 함수 제공

---

## 📋 개요

`services/` 디렉토리는 여러 도구에서 재사용되는 공통 로직을 모듈화하여 제공합니다. 특히 `RunStoreService`는 리서치 실행의 모든 상태를 관리하는 핵심 서비스입니다.

---

## 📁 디렉토리 구조

```
services/
├── __init__.py
├── rag_service.py                  # RAGService: 인덱싱/검색/근거 기반 답변
│
├── fetch_webpage_tool_funcs/       # fetch_webpage 도구 전용 함수
│   ├── __init__.py
│   └── crawl4ai_fetch.py           # Crawl4AI HTTP 전용 in-process fetch
│
├── run_store_tool_funcs/           # 리서치 실행 상태 관리 서비스
│   ├── __init__.py
│   ├── path_manager.py             # 파일 경로 관리
│   ├── record_serializer.py        # DocRecord 직렬화/역직렬화
│   └── run_store_service.py        # 핵심 상태 관리 서비스
│
└── screen_tool_funcs/              # 화면 캡처/개입 감지 (screen_context 도구용)
```

> 과거의 `services/hints.py`, `fetch_webpage_tool_funcs/hints.py`,
> `html_document_preprocessing.py`(BeautifulSoup 휴리스틱 추출)는 fetch가
> Crawl4AI 단일 경로로 전환되면서 제거되었습니다.

---

## 📦 서비스 모듈 상세

### 1. `run_store_tool_funcs/` - 리서치 실행 상태 관리

리서치 파이프라인의 모든 상태(계획, 문서, 요약, 레코드)를 관리하는 핵심 서비스입니다.

#### `RunStoreService` 클래스

```python
class RunStoreService:
    def __init__(self, root: str | Path):
        """출력 디렉토리를 기준으로 모든 경로 초기화"""
        
    # 요청/계획 관리
    def save_request(self, user_request: str) -> None
    def load_request(self) -> str
    def save_plan(self, payload: dict) -> None
    def load_plan(self) -> dict
    def plan_exists(self) -> bool
    
    # 문서 레코드 관리
    def load_records(self) -> list[DocRecord]
    def save_records(self, records: list[DocRecord]) -> None
    def list_non_duplicate_records(self) -> list[DocRecord]
    def list_duplicate_records(self) -> list[DocRecord]
    
    # 문서 저장/읽기
    def write_fetched_record(...) -> None
    def write_duplicate_record(...) -> None
    def write_fetch_error_note(...) -> None
    def write_document_summary(record, content) -> None
    def write_batch_summary(batch_index, content) -> None
    
    # 중복 검사
    def find_duplicate(text, threshold=0.82) -> tuple[bool, float, str | None]
    def jaccard_similarity(a: str, b: str) -> float
    
    # 최종 보고서
    def save_final_report(content: str) -> None
    def load_all_batch_summaries() -> list[str]
```

#### `RunPathManager` 클래스

```python
class RunPathManager:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.corpus_dir = root / "corpus"
        self.raw_html_dir = corpus_dir / "raw_html"
        self.clean_md_dir = root / "clean_md"   # Crawl4AI 정제 Markdown
        self.summary_dir = root / "summary"
        self.vector_dir = root / "chromadb"
        
        # 주요 파일 경로
        self.index_path = summary_dir / "index.json"
        self.request_path = summary_dir / "request.md"
        self.plan_path = summary_dir / "plan.json"
        self.final_path = root / "final.md"
```

**출력 디렉토리 구조** — 문서 텍스트 산출물은 항상 `.md`입니다 (raw HTML만 `.html`, 구조화 데이터만 `.json`):
```
output_dir/
├── final.md                    # 최종 보고서
├── chromadb/                   # 벡터 저장소 (RAG 인덱스는 clean_md 기준)
├── clean_md/                   # Crawl4AI 정제 Markdown (000.md, 001.md, ...)
│                               #   = RAG 답변 근거 + per-doc/batch 요약 입력
├── corpus/
│   └── raw_html/               # 원본 HTML 아카이브 (000.html, 001.html, ...)
└── summary/
    ├── index.json              # 문서 레코드 인덱스
    ├── request.md              # 사용자 요청
    ├── plan.json               # 리서치 계획
    ├── doc_000.md              # 개별 문서 요약 (조사 종료 시 일괄 생성)
    ├── doc_001.md
    ├── batch_001.md            # 배치 요약 (수집 루프 중 clean_md에서 생성)
    └── batch_002.md
```

---

### 2. `rag_service.py` - RAG 서비스

`RAGService`가 인덱싱·검색·쿼리 재작성·근거 기반 답변 생성을 모두 소유합니다.
`tools/rag_tool/`은 LLM tool-calling용 얇은 `rag_search` 어댑터일 뿐입니다.
RAG는 요약이 아닌 **clean_md**(`clean_md/<doc_id>.md`)를 인덱싱·검색합니다.

```python
class RAGService:
    def __init__(
        self,
        llm,                        # LLMClient 인스턴스
        vector_store: VectorStore,  # 벡터 저장소
        *,
        n_results: int = 5,         # 검색 결과 수
        max_context_chars: int = 12000,
        max_embed_chars: int = 900,
        chunk_overlap_chars: int = 120,
        max_history_turns: int = 3,
    ):
        
    # 인덱싱 (clean_md 기준 — 요약이 아닌 정제 원문을 RAG 근거로 사용)
    def index_autosurvey_output(clean_md_dir, index_path, clear_first=True) -> int
    def index_all_markdown(base_dir, clear_first=True) -> int
    def clear_index() -> None
    
    # 검색 및 응답
    def retrieve(query: str, use_history=True) -> list[dict]
    def answer(question: str, stream=False) -> str
    
    # 대화 루프
    def chat_loop() -> None
```

**특징**:
- **문서 청킹**: 긴 문서를 `max_embed_chars` 단위로 분할
- **히스토리 컨텍스트**: 이전 대화를 반영한 쿼리 재작성
- **출처 인용**: 응답에 문서 ID 인용 형식 적용

---

### 3. `fetch_webpage_tool_funcs/` - Crawl4AI 페이지 수집

문서 수집은 Crawl4AI HTTP 전용 크롤러 단일 경로입니다. BeautifulSoup 휴리스틱
추출(`hints.py`, `html_document_preprocessing.py`)은 제거되었습니다.

#### `crawl4ai_fetch.py`

| 함수 | 역할 |
|------|------|
| `crawl4ai_available()` | `crawl4ai` 패키지 import 가능 여부 |
| `fetch_with_crawl4ai(url, timeout_sec, max_chars)` | `AsyncHTTPCrawlerStrategy`(브라우저 없음)로 fetch → `DefaultMarkdownGenerator` + `PruningContentFilter`로 clean Markdown 추출. 성공/실패를 `dict`로 반환 |

Crawl4AI가 가져오지 못하는 URL은 실패로 처리되어 수집 대상에서 제외됩니다 —
따라서 저장되는 모든 문서는 clean Markdown(`clean_md/<doc_id>.md`)으로 보관됩니다.

---

## 🔧 사용 예시

### RunStoreService 사용

```python
from services.run_store_tool_funcs import RunStoreService

store = RunStoreService(root="./output")

# 요청 및 계획 저장
store.save_request("AI 윤리에 대한 최신 연구")
store.save_plan({"search_queries": ["AI ethics", "AI safety"], ...})

# 문서 레코드 관리
records = store.load_records()
store.write_fetched_record(
    doc_id="001",
    title="AI Ethics Overview",
    url="https://example.com/ai-ethics",
    ...
)

# 중복 검사
is_dup, score, dup_of = store.find_duplicate(new_text)
```

### RAGTool 사용

```python
from storage.vector_store import VectorStore
from tools.rag_tool import RAGTool

vector_store = VectorStore(persist_dir=output_dir / "chromadb")
rag = RAGTool(llm=llm, vector_store=vector_store)

# 문서 인덱싱 (clean_md 기준)
rag.index_autosurvey_output(clean_md_dir=output_dir / "clean_md")

# 질문 응답
answer = rag.answer("AI 윤리의 주요 쟁점은?", stream=True)

# 대화 루프
rag.chat_loop()
```

---

## 🛠️ 새 서비스 추가 가이드

### Step 1: 서비스 디렉토리 생성

```bash
mkdir services/my_service
```

### Step 2: 서비스 클래스 작성

```python
# services/my_service/my_service.py
from pathlib import Path

class MyService:
    def __init__(self, config_path: Path):
        self.config_path = config_path
    
    def process(self, data: dict) -> dict:
        """데이터 처리 로직"""
        return {"processed": True, **data}
```

### Step 3: `__init__.py` 작성

```python
# services/my_service/__init__.py
from .my_service import MyService

__all__ = ["MyService"]
```

### Step 4: 상위 `__init__.py`에 등록 (선택)

```python
# services/__init__.py
from .my_service import MyService
```

---

## 📐 설계 원칙

1. **상태 캡슐화**: 파일 I/O, 경로 관리 등을 서비스 내부로 캡슐화
2. **단일 책임**: 각 서비스는 하나의 도메인만 담당
3. **의존성 최소화**: `core/` 모델만 의존, 다른 서비스는 의존 금지
4. **테스트 용이성**: 파일 시스템 의존성을 `root` 파라미터로 주입

---

## 🔗 의존성 관계

```
tools/
├── query_plan_tool ──────────┐
├── document_summarize_tool ──┼──▶ services/run_store_tool_funcs/
├── final_report_tool ────────┘          │
└── fetch_webpage_tool ──────▶ services/fetch_webpage_tool_funcs/

services/
├── run_store_tool_funcs/ ──▶ core/models.DocRecord
├── rag_service.py ──────────▶ storage/vector_store.VectorStore (ChromaDB)
└── fetch_webpage_tool_funcs/ ──▶ crawl4ai (HTTP 전용 크롤러)
```

**핵심 규칙**:
- `services/`는 `core/`와 외부 라이브러리만 의존
- `tools/`가 `services/`를 사용하는 단방향 의존성
- 서비스 간 직접 의존 금지
