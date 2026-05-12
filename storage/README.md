# storage/

> RAG storage update: `VectorStore` is now consumed by `tools/rag_tool/RAGTool` rather than a service-owned RAG implementation. `storage/` still only owns persistence and vector retrieval; prompt construction, chat behavior, and tool exposure belong outside this layer.

**역할**: 데이터 영속성 계층 - 벡터 저장소 및 기타 데이터 저장 기능 제공

---

## 📋 개요

`storage/` 디렉토리는 애플리케이션의 데이터 저장 및 검색 기능을 담당합니다. 현재는 ChromaDB 기반의 벡터 저장소를 제공하며, RAG(Retrieval-Augmented Generation)를 위한 문서 임베딩 저장과 유사도 검색을 지원합니다.

---

## 📁 디렉토리 구조

```
storage/
└── vector_store.py    # VectorStore 클래스 (ChromaDB 래퍼)
```

---

## 🏗️ 핵심 컴포넌트

### `VectorStore` 클래스

```python
class VectorStore:
    """ChromaDB-based vector store for document embeddings."""
    
    def __init__(
        self,
        persist_dir: Path,                      # 영구 저장 경로
        collection_name: str = "research_docs", # 컬렉션 이름
        embedding_fn: Callable[[str], list[float]] | None = None,  # 임베딩 함수
    ): ...
```

---

## 🔧 주요 메서드

### 1. 문서 추가

```python
# 단일 문서 추가
def add_document(
    self,
    doc_id: str,              # 고유 식별자
    content: str,             # 문서 내용
    embedding: list[float] | None = None,  # 사전 계산된 임베딩
    metadata: dict[str, Any] | None = None,
) -> None:

# 배치 추가
def add_documents(
    self,
    doc_ids: list[str],
    contents: list[str],
    embeddings: list[list[float]] | None = None,
    metadatas: list[dict[str, Any]] | None = None,
) -> None:
```

**사용 예**:
```python
from storage.vector_store import VectorStore

store = VectorStore(persist_dir=Path("./data/vectors"))

# 문서 추가
store.add_document(
    doc_id="doc_001",
    content="인공지능의 역사와 발전...",
    metadata={"source": "wikipedia", "date": "2024-01-01"}
)

# 배치 추가 (임베딩 포함)
store.add_documents(
    doc_ids=["doc_002", "doc_003"],
    contents=["첫 번째 문서 내용", "두 번째 문서 내용"],
    embeddings=[[0.1, 0.2, ...], [0.3, 0.4, ...]],
    metadatas=[{"type": "article"}, {"type": "blog"}]
)
```

### 2. 유사도 검색

```python
def query(
    self,
    query_text: str | None = None,           # 검색 텍스트
    query_embedding: list[float] | None = None,  # 사전 계산된 임베딩
    n_results: int = 5,                       # 반환 결과 수
    where: dict[str, Any] | None = None,      # 메타데이터 필터
) -> list[dict[str, Any]]:
```

**반환 형식**:
```python
[
    {
        "doc_id": "doc_001",
        "content": "문서 내용...",
        "metadata": {"source": "wikipedia", ...},
        "distance": 0.123  # 코사인 거리 (작을수록 유사)
    },
    ...
]
```

**사용 예**:
```python
# 텍스트 검색 (임베딩 함수 필요)
results = store.query(query_text="AI 윤리", n_results=3)

# 임베딩으로 직접 검색
query_embedding = llm.embed("검색어")
results = store.query(query_embedding=query_embedding)

# 메타데이터 필터링
results = store.query(
    query_embedding=embedding,
    where={"source": "academic"},
    n_results=5
)
```

### 3. 관리 메서드

```python
def get_document_count(self) -> int:
    """저장된 문서 수 반환"""

def clear(self) -> None:
    """모든 문서 삭제"""

def delete_documents(self, doc_ids: list[str]) -> None:
    """특정 문서 삭제"""
```

---

## ⚙️ 기술 스펙

| 항목 | 내용 |
|------|------|
| **백엔드** | ChromaDB (PersistentClient) |
| **유사도 측정** | Cosine Similarity (`hnsw:space: cosine`) |
| **인덱싱** | HNSW (Hierarchical Navigable Small World) |
| **영속성** | 디스크 저장 (지정된 `persist_dir`) |

---

## 🔌 RAGTool과의 연동

`VectorStore`는 `tools/rag_tool`의 `RAGTool`과 함께 사용됩니다. `services.rag_service.RAGService`는 기존 import 호환을 위한 alias입니다:

```python
from llm.llama_server_llm import LLMClient
from storage.vector_store import VectorStore
from tools.rag_tool import RAGTool

# 초기화
llm = LLMClient(host="127.0.0.1", port=8080)
vector_store = VectorStore(
    persist_dir=Path("./output/chromadb"),
    collection_name="research_docs",
)

# RAGTool 생성
rag = RAGTool(
    llm=llm,
    vector_store=vector_store,
    n_results=5,
)

# 문서 인덱싱 (RAGTool이 청킹 + 임베딩 처리)
rag.index_autosurvey_output(summary_dir=Path("./output/summary"))

# 검색 및 응답
answer = rag.answer("AI 윤리의 주요 쟁점은?")
```

---

## 🛠️ 확장 가이드

### 다른 벡터 DB 추가

```python
# storage/pinecone_store.py
class PineconeStore:
    def __init__(self, api_key: str, index_name: str): ...
    def add_documents(self, doc_ids, contents, embeddings, metadatas): ...
    def query(self, query_embedding, n_results, where): ...
    def get_document_count(self) -> int: ...
    def clear(self) -> None: ...
```

### 다중 컬렉션 관리

```python
# 용도별 컬렉션 분리
web_store = VectorStore(persist_dir=Path("./data"), collection_name="web_pages")
doc_store = VectorStore(persist_dir=Path("./data"), collection_name="documents")
```

---

## 📐 설계 원칙

1. **Upsert 전략**: 동일 ID 문서는 자동 갱신 (중복 방지)
2. **외부 임베딩**: `embedding_fn`은 외부에서 주입받아 LLM과의 결합도 낮춤
3. **Batch 처리**: 대량 문서 처리 시 `add_documents()` 사용 권장
4. **메타데이터 활용**: 필터링을 위해 문서에 적절한 메타데이터 부여

---

## 🔗 의존성

```
storage/
└── vector_store.py ──▶ chromadb

사용처:
├── tools/rag_tool.RAGTool ──▶ storage/VectorStore
└── main.py ──▶ storage/VectorStore (초기화)
```

**핵심 규칙**:
- 이 모듈은 다른 프로젝트 모듈에 의존하지 않음 (독립적)
- 외부 라이브러리(`chromadb`)만 의존
- 상위 모듈(`services/`, `main.py`)에서 이 모듈을 사용
