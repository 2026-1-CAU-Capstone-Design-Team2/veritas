# services/

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
├── hints.py                        # HTML 힌트 패턴 re-export
├── rag_service.py                  # RAGService 호환 alias (구현은 tools/rag_tool/)
│
├── fetch_webpage_tool_funcs/       # fetch_webpage 도구 전용 함수들
│   ├── __init__.py
│   ├── hints.py                    # HTML 콘텐츠 판별 정규식 패턴
│   └── html_document_preprocessing.py  # HTML 전처리 함수들
│
└── run_store_tool_funcs/           # 리서치 실행 상태 관리 서비스
    ├── __init__.py
    ├── path_manager.py             # 파일 경로 관리
    ├── record_serializer.py        # DocRecord 직렬화/역직렬화
    └── run_store_service.py        # 핵심 상태 관리 서비스
```

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
        self.raw_text_dir = corpus_dir / "raw_text"
        self.summary_dir = root / "summary"
        self.vector_dir = root / "chromadb"
        
        # 주요 파일 경로
        self.index_path = summary_dir / "index.json"
        self.request_path = summary_dir / "request.txt"
        self.plan_path = summary_dir / "plan.json"
        self.final_path = root / "final.md"
```

**출력 디렉토리 구조**:
```
output_dir/
├── final.md                    # 최종 보고서
├── chromadb/                   # 벡터 저장소
├── corpus/
│   ├── raw_html/               # 원본 HTML (000.html, 001.html, ...)
│   └── raw_text/               # 추출된 텍스트 (000.txt, 001.txt, ...)
└── summary/
    ├── index.json              # 문서 레코드 인덱스
    ├── request.txt             # 사용자 요청
    ├── plan.json               # 리서치 계획
    ├── doc_000.md              # 개별 문서 요약
    ├── doc_001.md
    ├── batch_001.md            # 배치 요약
    └── batch_002.md
```

---

### 2. `rag_service.py` - RAG 호환 경로

현재 RAG의 실제 구현은 `tools/rag_tool/RAGTool`로 이동했습니다. 이 파일은 기존 코드의 `from services.rag_service import RAGService` import를 깨지 않기 위한 얇은 호환 계층입니다. 신규 RAG 기능, LLM-facing schema, multi-turn chat tool-use 정책은 `tools/rag_tool/`에서 관리합니다.

수집된 문서를 기반으로 대화형 Q&A를 제공하는 서비스입니다.

```python
class RAGService(RAGTool):
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
        
    # 인덱싱
    def index_autosurvey_output(summary_dir, index_path, clear_first=True) -> int
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

### 3. `fetch_webpage_tool_funcs/` - HTML 전처리 함수

웹페이지에서 본문 콘텐츠를 추출하기 위한 전처리 함수 모음입니다.

#### `hints.py` - 콘텐츠 판별 패턴

```python
# 본문 콘텐츠 힌트 (높은 점수)
MAIN_CONTENT_HINT = re.compile(
    r"article|content|post|entry|story|main|markdown|blog|news|body|text",
    re.IGNORECASE,
)

# 보일러플레이트 힌트 (감점)
BOILERPLATE_HINT = re.compile(
    r"nav|menu|header|footer|sidebar|breadcrumb|share|social|comment|ads|promo",
    re.IGNORECASE,
)
```

#### `html_document_preprocessing.py` - 전처리 함수

| 함수 | 역할 |
|------|------|
| `_strip_noise_tags(root)` | 불필요한 태그 제거 (script, nav, footer 등) |
| `_candidate_nodes(root)` | 본문 후보 노드 선별 (article, main, section 등) |
| `_content_score(node)` | 노드의 본문 점수 계산 |
| `_select_main_content_node(root)` | 최적의 본문 노드 선택 |
| `_extract_meaningful_text(node, max_chars)` | 의미 있는 텍스트만 추출 |

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

# 문서 인덱싱
rag.index_autosurvey_output(summary_dir=output_dir / "summary")

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
├── rag_service.py ──────────▶ tools/rag_tool.RAGTool (compat alias)
└── fetch_webpage_tool_funcs/ ──▶ bs4 (BeautifulSoup)
```

**핵심 규칙**:
- `services/`는 `core/`와 외부 라이브러리만 의존
- `tools/`가 `services/`를 사용하는 단방향 의존성
- 서비스 간 직접 의존 금지
