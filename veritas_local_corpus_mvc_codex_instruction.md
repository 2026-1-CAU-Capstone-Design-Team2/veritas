# VERITAS Local Corpus Integration — Codex Implementation Instruction

## 0. 목적

현재 VERITAS는 AutoSurvey로 수집한 외부 웹 조사 결과를 중심으로 다음 흐름을 가진다.

```text
AutoSurvey → clean_md / summary / final.md → ChromaDB → Verification / RAG Chat / Draft
```

이 작업의 목표는 **사용자의 로컬 파일을 VERITAS workspace의 지식 코퍼스로 통합**하는 것이다.

단, 로컬 파일을 단순히 AutoSurvey 문서처럼 끼워 넣지 말 것. 로컬 파일은 다음 속성을 가진 별도 corpus로 다뤄야 한다.

- 외부 검색에 사용되면 안 되는 private knowledge
- 파일/폴더/시트/page/range 등 구조적 출처를 가진 knowledge
- PDF/docx/md/txt 같은 비정형 문서와 xlsx/csv 같은 구조화 데이터를 모두 포함
- RAG, Verification, Draft Generation에서 외부 조사 결과와 함께 사용되어야 함
- privacy mode에 따라 local-only LLM 사용을 강제할 수 있어야 함

이 구현은 **기능 추가보다 아키텍처 정리와 유지보수성**이 우선이다. MVC 및 계층형 책임 분리를 반드시 지켜라.

---

## 1. 현재 코드 구조 요약

현재 주요 구조는 다음과 같다.

```text
core/models.py
- IndexedDocRecord: AutoSurvey author-side index.json record
- ParsedDocRecord: verification reader-side document model

services/rag_service.py
- RAGService
- index_autosurvey_output(clean_md_dir, index_path)
- index_all_markdown(base_dir)
- query / answer generation

storage/vector_store.py
- VectorStore: ChromaDB wrapper
- add_documents / query / clear / delete_documents

services/verification/artifact_loader.py
- ArtifactLoader.load_docs(workspace)
- ArtifactLoader.load_chunks(workspace)

services/verification/service.py
- ALL_TASKS = ("sections", "reliability", "consensus")
- VerificationService.run(...)

api/services/draft_service.py
- _gather_knowledge(...)
- currently reads final.md and summary/batch_*.md or selected summary/doc_<id>.md

api/api_routes/*
- API controller layer

frontend/ui/*
- View layer

frontend/controllers/*
- Desktop UI controller layer
```

현재 `IndexedDocRecord`와 `ParsedDocRecord`는 웹 조사 결과에 강하게 결합되어 있다. `url`, `domain`, `search_query`, `html_path` 같은 필드는 로컬 파일에 자연스럽지 않다. 따라서 기존 dataclass에 억지로 optional field를 계속 추가하지 말고, **공통 knowledge model을 새로 정의**해야 한다.

---

## 2. 절대 준수할 아키텍처 원칙

### 2.1 MVC + Service + Repository 계층을 강제한다

이 프로젝트에서 MVC를 다음처럼 해석한다.

```text
Model
- core/models/*
- domain dataclass / enum / value object
- business entity only
- UI/API/Chroma/LLM에 의존 금지

View
- frontend/ui/*
- 화면 렌더링만 담당
- 파일 parsing, vector indexing, LLM 호출 금지

Controller
- api/api_routes/*
- frontend/controllers/*
- request/response 변환, 이벤트 연결, service 호출만 담당
- business logic 금지

Service / Use Case
- services/*
- 실제 workflow orchestration 담당
- controller에서 호출되는 application-level API 제공
- 단, parsing/indexing/retrieval/persistence를 직접 다 하지 말고 하위 component에 위임

Repository / Persistence
- db/*
- storage/*
- services/.../manifest_repository.py 등
- JSON, DB, ChromaDB, DuckDB, filesystem 접근 담당
```

### 2.2 의존성 방향

반드시 아래 방향으로만 의존해야 한다.

```text
frontend/ui → frontend/controllers → api client
api/api_routes → api/services → services/domain/use_cases → repositories/storage
core/models → no project-internal dependencies except stdlib typing/dataclasses/enums
```

금지:

```text
core/models → services
services → api/api_routes
services → frontend
storage → api
storage → frontend
repository → controller
view → service 직접 호출
```

### 2.3 Controller에 business logic을 넣지 말 것

API route는 다음만 한다.

- request model validation
- service 호출
- response model 반환
- HTTP exception 변환

금지 예시:

```python
# 금지: api route에서 직접 파일 파싱, Chroma indexing, Excel profile 생성
@router.post("/local-corpus/index")
def index_local_files(req):
    for path in req.paths:
        text = parse_docx(path)
        chroma.add_documents(...)
```

허용 예시:

```python
@router.post("/local-corpus/index")
def index_local_files(req: LocalCorpusIndexRequest):
    return local_corpus_service.index_workspace_sources(
        workspace_id=req.workspace_id,
        roots=req.roots,
        options=req.options,
    )
```

### 2.4 God Service 금지

`RAGService`, `VerificationService`, `DraftService`, `AgentRuntime`에 모든 기능을 추가하지 말 것.

특히 다음은 금지한다.

- `RAGService`가 로컬 파일 탐색, PDF/docx/xlsx parsing, manifest 관리까지 담당
- `VerificationService`가 로컬 파일 parsing을 직접 수행
- `draft_service._gather_knowledge()`에 local/external retrieval, rerank, crosscheck merge를 전부 추가
- `AgentRuntime`에 local corpus indexing workflow를 직접 작성

기능은 작은 service/component로 분리한다.

---

## 3. 목표 구조

다음 구조를 새로 도입한다.

```text
core/models/
  __init__.py
  knowledge.py
  local_corpus.py
  verification_crosscheck.py
  draft_knowledge.py

services/knowledge/
  __init__.py
  source_registry.py
  chunker.py
  knowledge_indexer.py
  retrieval_service.py
  knowledge_pack_builder.py

services/local_corpus/
  __init__.py
  file_scanner.py
  parsers.py
  table_profiler.py
  local_corpus_service.py
  manifest_repository.py

services/verification/crosscheck/
  __init__.py
  claim_extractor.py
  claim_matcher.py
  relation_judge.py
  pipeline.py

storage/
  vector_store.py              # extend only cleanly
  table_store.py               # optional: DuckDB/SQLite wrapper for csv/xlsx

api/api_routes/
  local_corpus.py

api/services/
  local_corpus_app_service.py  # thin application service if needed
```

기존 `core/models.py`는 즉시 대규모로 깨지 않되, 신규 모델은 `core/models/knowledge.py` 계열로 이동시키는 것을 우선한다. 만약 현재 `core/models.py` 파일과 `core/models/` 패키지가 충돌한다면, 다음 중 하나를 선택한다.

1. 안전한 MVP: `core/knowledge_models.py`, `core/local_corpus_models.py`로 생성
2. 구조 리팩토링 가능 시: `core/models.py`를 `core/models/__init__.py` 패키지로 승격하고 기존 import 호환 처리

기존 import가 많다면 1번을 우선하라. 유지보수성이 중요하지만, 대규모 import breakage는 피한다.

---

## 4. 신규 Domain Model 정의

### 4.1 KnowledgeSourceRecord

파일 위치 예시: `core/knowledge_models.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class SourceScope(str, Enum):
    EXTERNAL = "external"
    LOCAL = "local"


class SourceKind(str, Enum):
    WEB_PAGE = "web_page"
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    MARKDOWN = "markdown"
    XLSX = "xlsx"
    CSV = "csv"
    TABLE_SUMMARY = "table_summary"
    UNKNOWN = "unknown"


class PrivacyLabel(str, Enum):
    PUBLIC_WEB = "public_web"
    LOCAL_PRIVATE = "local_private"
    LOCAL_APPROVED_EXTERNAL = "local_approved_external"


@dataclass(frozen=True)
class KnowledgeSourceRecord:
    source_id: str
    workspace_id: str
    source_scope: SourceScope
    source_kind: SourceKind
    title: str
    canonical_uri: str
    display_path: str
    privacy_label: PrivacyLabel
    content_hash: str
    created_at: str = ""
    modified_at: str = ""
    parser_version: str = ""
    status: str = "indexed"
    metadata: dict[str, Any] = field(default_factory=dict)
```

### 4.2 KnowledgeChunkRecord

```python
@dataclass(frozen=True)
class KnowledgeChunkRecord:
    chunk_id: str
    source_id: str
    workspace_id: str
    source_scope: SourceScope
    source_kind: SourceKind
    text: str
    chunk_index: int
    chunk_count: int
    page_start: int | None = None
    page_end: int | None = None
    sheet_name: str | None = None
    row_start: int | None = None
    row_end: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

### 4.3 LocalFileManifest

```python
@dataclass(frozen=True)
class LocalFileManifestEntry:
    source_id: str
    root_id: str
    absolute_path: str
    relative_path: str
    file_name: str
    extension: str
    size_bytes: int
    modified_at: str
    content_hash: str
    parser_status: str
    parser_error: str = ""
    extracted_text_path: str = ""
    table_profile_path: str = ""
```

### 4.4 TableProfile

Excel/CSV는 일반 텍스트 문서로만 취급하면 안 된다.

```python
@dataclass(frozen=True)
class TableColumnProfile:
    name: str
    inferred_type: str
    null_count: int
    non_null_count: int
    sample_values: list[str]
    min_value: float | None = None
    max_value: float | None = None
    mean_value: float | None = None


@dataclass(frozen=True)
class TableProfile:
    source_id: str
    sheet_name: str | None
    row_count: int
    column_count: int
    columns: list[TableColumnProfile]
    sample_rows_markdown: str
    summary_markdown: str
```

---

## 5. Workspace 저장 구조

기존 workspace 구조를 망가뜨리지 말고, local corpus용 하위 디렉터리를 추가한다.

```text
runs/<workspace>/
  clean_md/                 # existing external autosurvey clean md
  summary/                  # existing external summaries
  final.md                  # existing final autosurvey output
  chromadb/                 # existing vector store root

  local/
    manifest.json
    extracted_md/
      <source_id>.md
    tables/
      table_profiles.json
      <source_id>.duckdb     # optional, MVP에서는 생략 가능
    summaries/
      <source_id>.md         # optional local file summary

  knowledge/
    sources.json             # unified KnowledgeSourceRecord list
    chunks_manifest.json     # optional; vector metadata가 충분하면 생략 가능

  verification/
    sections.json
    reliability.json
    consensus.json
    crosscheck.json

  drafts/
    <draft_id>.md
    <draft_id>.source_map.json
```

`workspace_paths.py`에 local/knowledge/verification/drafts 경로 accessor를 추가하되, 경로 생성 책임을 분명히 하라.

금지:

- 여러 service에서 직접 `Path("runs") / workspace / "local"` 생성 반복
- API route에서 workspace path 직접 조립

---

## 6. Local Corpus Ingestion 구현 지시

### 6.1 FileScanner

파일: `services/local_corpus/file_scanner.py`

책임:

- 허용된 root folder 아래 파일 탐색
- 확장자 필터
- 숨김 파일/임시 파일/캐시 디렉터리 제외
- 파일 크기 제한 적용
- content hash 계산
- manifest entry 후보 생성

금지:

- 파일 내용 parsing 금지
- ChromaDB 접근 금지
- LLM 호출 금지

권장 제한값:

```python
DEFAULT_ALLOWED_EXTENSIONS = {".md", ".txt", ".pdf", ".docx", ".xlsx", ".csv"}
DEFAULT_MAX_FILE_SIZE_MB = 50
DEFAULT_MAX_FILES = 300
```

### 6.2 ParserRegistry / Parsers

파일: `services/local_corpus/parsers.py`

책임:

- 파일 타입별 텍스트 추출
- 실패 시 parser_status와 parser_error 반환
- 원문 파일을 수정하지 않음

MVP parser:

- `.txt`: utf-8, cp949 fallback
- `.md`: utf-8, cp949 fallback
- `.docx`: python-docx 사용 가능 시 추출
- `.pdf`: 텍스트 기반 PDF만 우선 지원. OCR은 MVP 범위 밖. OCR 필요 시 TODO로 명시
- `.csv`: pandas 또는 csv module로 schema/sample/profile 생성
- `.xlsx`: openpyxl로 sheet별 schema/sample/profile 생성

Parser 출력은 반드시 구조화할 것.

```python
@dataclass(frozen=True)
class ParsedLocalDocument:
    source_id: str
    source_kind: SourceKind
    markdown_text: str
    table_profiles: list[TableProfile]
    metadata: dict[str, Any]
```

금지:

- parser가 vector store에 직접 넣는 것
- parser가 verification artifact를 생성하는 것
- parser가 draft용 요약을 직접 생성하는 것

### 6.3 TableProfiler

파일: `services/local_corpus/table_profiler.py`

책임:

- CSV/XLSX의 sheet/table profile 생성
- 컬럼 타입 추정
- numeric column 통계
- sample rows markdown 생성
- RAG용 `summary_markdown` 생성

주의:

- 전체 row를 임베딩하지 말 것
- 기본적으로 table summary와 sample rows만 vector index에 넣을 것
- 실제 수치 계산 질의는 추후 `TableStore`에서 처리할 수 있도록 확장 포인트를 남길 것

### 6.4 LocalCorpusService

파일: `services/local_corpus/local_corpus_service.py`

책임:

- FileScanner → ParserRegistry → ManifestRepository → KnowledgeIndexer orchestration
- progress callback emission
- 변경 감지: content_hash/mtime 기반으로 unchanged 파일 skip

금지:

- parser 세부 로직 직접 구현 금지
- ChromaDB low-level call 직접 호출 금지. 반드시 `KnowledgeIndexer`를 통해 호출
- API request/response model import 금지

권장 public method:

```python
class LocalCorpusService:
    def index_workspace_sources(
        self,
        workspace_id: str,
        roots: list[str],
        *,
        clear_local_first: bool = False,
        progress_callback: Callable[..., None] | None = None,
    ) -> LocalCorpusIndexResult:
        ...

    def list_sources(self, workspace_id: str) -> list[KnowledgeSourceRecord]:
        ...

    def remove_sources(self, workspace_id: str, source_ids: list[str]) -> LocalCorpusMutationResult:
        ...
```

---

## 7. Knowledge Indexing / Retrieval 구현 지시

### 7.1 KnowledgeIndexer

파일: `services/knowledge/knowledge_indexer.py`

책임:

- KnowledgeSourceRecord + markdown_text/table_profile을 chunking
- VectorStore에 upsert
- metadata 표준화
- external/local source 모두 처리 가능해야 함

기존 `RAGService.index_autosurvey_output()`의 chunking 로직은 재사용 가능하지만, 장기적으로는 `RAGService` 밖으로 이동시켜라.

권장 구조:

```python
class KnowledgeIndexer:
    def index_sources(
        self,
        sources: list[KnowledgeSourceRecord],
        documents: dict[str, str],
        *,
        clear_where: dict | None = None,
    ) -> int:
        ...
```

Vector metadata는 반드시 다음 필드를 포함해야 한다.

```python
{
    "workspace_id": workspace_id,
    "source_id": source_id,
    "source_scope": "local" | "external",
    "source_kind": "pdf" | "docx" | "web_page" | ...,
    "privacy_label": "local_private" | "public_web" | ...,
    "title": title,
    "display_path": display_path,
    "parent_doc_id": source_id,
    "chunk_index": chunk_index,
    "chunk_count": chunk_count,
}
```

### 7.2 VectorStore 확장

파일: `storage/vector_store.py`

필요하면 다음 method를 추가하라.

```python
def delete_where(self, where: dict[str, Any]) -> None: ...
def get_all(self, where: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...
```

주의:

- 기존 public API를 깨지 말 것
- collection name 기본값 `research_docs`는 유지
- local/external 분리는 metadata filter로 우선 구현
- collection 분리는 후속 확장으로 가능하게 설계

### 7.3 RetrievalService

파일: `services/knowledge/retrieval_service.py`

책임:

- RAG query에 대해 local/external/all filter 적용
- ChromaDB query 결과를 domain object 또는 normalized dict로 변환
- RAGService가 직접 VectorStore metadata filter를 조립하지 않도록 분리

권장 method:

```python
class RetrievalService:
    def retrieve(
        self,
        workspace_id: str,
        query: str,
        *,
        n_results: int = 8,
        source_scopes: set[SourceScope] | None = None,
        source_kinds: set[SourceKind] | None = None,
        include_private: bool = True,
    ) -> list[RetrievedChunk]:
        ...
```

`RAGService`는 장기적으로 이 service를 사용하도록 리팩토링한다.

---

## 8. Verification Cross-check 구현 지시

### 8.1 Task 추가

`services/verification/service.py`의 task 목록에 `crosscheck`를 추가한다.

```python
ALL_TASKS: tuple[str, ...] = ("sections", "reliability", "consensus", "crosscheck")
```

단, 기존 sections/reliability/consensus 동작을 깨지 말 것.

### 8.2 ArtifactLoader 확장

`ArtifactLoader.load_docs()`는 현재 외부 AutoSurvey summary/index 중심이다. 다음 중 하나로 확장하라.

권장:

```python
load_external_docs(workspace) -> list[ParsedDocRecord]
load_local_docs(workspace) -> list[ParsedKnowledgeRecord]
load_knowledge_sources(workspace) -> list[KnowledgeSourceRecord]
```

기존 `load_docs()`는 backward compatibility를 위해 유지하라.

금지:

- 기존 `ParsedDocRecord`에 local-only 의미의 fake url/domain/search_query를 넣지 말 것
- local file을 external record로 위장하지 말 것

### 8.3 Crosscheck Pipeline

파일: `services/verification/crosscheck/pipeline.py`

책임:

- external/local source에서 claim candidate 추출
- claim type 분류: numeric/date/entity/policy/general
- source 간 relation 판정
- artifact 저장

초기 MVP는 LLM 없이 heuristic 중심이어도 된다.

우선 구현할 relation:

```text
supports
contradicts
partially_supports
insufficient
stale_or_outdated
numeric_mismatch
```

출력 모델 예시:

```python
@dataclass(frozen=True)
class CrossCheckClaim:
    claim_id: str
    source_id: str
    source_scope: SourceScope
    text: str
    claim_type: str
    evidence_span: str
    metadata: dict[str, Any]

@dataclass(frozen=True)
class CrossCheckRelation:
    claim_a: str
    claim_b: str
    relation: str
    severity: str
    reason: str

@dataclass(frozen=True)
class CrossCheckArtifact:
    claims: list[CrossCheckClaim]
    relations: list[CrossCheckRelation]
    flags: list[dict[str, Any]]
```

### 8.4 Persistence

`services/verification/persistence.py`에 `crosscheck.json` 저장/로드를 추가한다.

기존 artifact 파일을 덮어쓰지 않도록 `completed` task 단위 persist 방식을 유지하라.

---

## 9. Draft Generation 통합 지시

현재 `api/services/draft_service.py::_gather_knowledge()`는 final.md와 summary batch를 직접 읽는다. 이 구조는 local corpus가 들어오면 유지보수성이 낮다.

### 9.1 KnowledgePackBuilder 도입

파일: `services/knowledge/knowledge_pack_builder.py`

책임:

- draft outline/section별로 필요한 evidence retrieval
- external/local evidence 분리
- table summary 삽입
- crosscheck conflict note 반영
- prompt에 넣을 markdown context 생성
- source_map 생성

권장 모델:

```python
@dataclass(frozen=True)
class SectionKnowledgePack:
    section_title: str
    external_evidence: list[RetrievedChunk]
    local_evidence: list[RetrievedChunk]
    table_summaries: list[str]
    conflict_notes: list[str]

@dataclass(frozen=True)
class DraftKnowledgePack:
    global_context: str
    section_packs: list[SectionKnowledgePack]
    source_map: dict[str, Any]
```

### 9.2 draft_service 변경 방식

`_gather_knowledge()`를 즉시 삭제하지 말고 다음처럼 점진적으로 바꿔라.

1. 기존 `_gather_knowledge()` 유지
2. 새 `_gather_knowledge_pack()` 추가
3. draft generation path에서 feature flag 또는 options로 새 builder 사용
4. 안정화 후 기존 gather path 축소

금지:

- draft_service 안에 ChromaDB query 직접 추가
- draft_service 안에 local file parser 호출
- selected_doc_ids만으로 local/external filter를 어설프게 처리

초안 생성 시 반드시 지킬 원칙:

- 로컬 파일 원문 전체를 prompt에 넣지 말 것
- section별 필요한 chunk만 넣을 것
- Excel/CSV는 계산된 summary/profile만 넣을 것
- crosscheck conflict가 있으면 “주의/불일치/기준연도 차이”를 prompt에 넣을 것
- source_map을 저장할 것

---

## 10. RAG Chat 통합 지시

RAG Chat은 사용자가 다음 중 하나를 선택할 수 있어야 한다.

```text
- 외부 조사 결과만 사용
- 로컬 파일만 사용
- 외부 + 로컬 모두 사용
```

API/request model에 다음 개념을 추가하라.

```python
source_scope_filter: Literal["external", "local", "all"] = "all"
include_private_local: bool = True
```

Privacy rule:

- `include_private_local=True`이고 local source가 retrieval context에 포함될 경우, 기본적으로 local LLM provider를 사용해야 한다.
- 외부 API provider를 사용할 수 있는 경우는 사용자가 명시적으로 local file external processing을 허용한 경우뿐이다.
- 이 rule은 controller가 아니라 service layer에서 검사해야 한다.

---

## 11. API Controller 구현 지시

파일: `api/api_routes/local_corpus.py`

필수 endpoint 예시:

```python
POST /local-corpus/index
GET  /local-corpus/sources/{workspace_id}
DELETE /local-corpus/sources/{workspace_id}
POST /local-corpus/reindex/{workspace_id}
```

API route는 다음만 담당한다.

- Pydantic request/response model 사용
- service 호출
- 예외를 HTTPException으로 변환

Pydantic model은 `api/api_models.py` 또는 별도 `api/models/local_corpus.py`에 둔다.

금지:

- API route에서 filesystem scan 금지
- API route에서 parser 호출 금지
- API route에서 vector store 직접 접근 금지

---

## 12. Frontend / View 구현 지시

View는 최소한 다음을 제공한다.

- workspace별 local folder 추가 UI
- indexing progress 표시
- indexed local sources 목록 표시
- source scope filter 선택: external/local/all

단, frontend는 backend service 책임을 침범하면 안 된다.

`frontend/ui/*`:

- 화면 component만
- business logic 금지

`frontend/controllers/*`:

- UI event → API call 연결
- 응답을 view model로 변환
- parser/indexing 로직 금지

---

## 13. Privacy / Security 강제사항

반드시 지킬 것:

1. 로컬 파일 내용은 AutoSurvey 검색 query 생성에 절대 사용하지 말 것.
2. 로컬 파일 내용은 외부 검색 API, 외부 LLM API로 전달하지 말 것. 단, 사용자가 명시적으로 허용한 경우만 예외.
3. Vector metadata에 `privacy_label`을 반드시 넣을 것.
4. source_scope가 `local`인 chunk가 RAG context에 포함되면 provider policy check를 수행할 것.
5. 로그에 로컬 파일 원문 내용을 출력하지 말 것.
6. 로그에는 파일명/상대경로 정도만 출력하고, 필요하면 masking 옵션을 둘 것.
7. absolute path는 내부 manifest에는 저장 가능하지만, UI와 prompt에는 기본적으로 relative/display path만 사용하라.

---

## 14. 테스트 요구사항

테스트는 반드시 추가한다. 실제 LLM/API 호출 없이 fake/stub로 검증한다.

### 14.1 Unit Tests

추가할 테스트 예시:

```text
tests/test_local_file_scanner.py
- allowed extension만 수집
- max file size 초과 skip
- hidden/temp files skip
- content hash stable

tests/test_local_parsers.py
- txt/md parsing
- csv profile generation
- xlsx sheet profile generation if openpyxl available
- unsupported extension returns clean error

tests/test_knowledge_indexer.py
- local source metadata includes source_scope/source_kind/privacy_label
- chunk ids deterministic
- table summary indexed, full table rows not blindly indexed

tests/test_retrieval_filters.py
- source_scope_filter=local returns only local
- source_scope_filter=external returns only external
- source_scope_filter=all returns both

tests/test_crosscheck_pipeline.py
- numeric mismatch detected
- local/external source ids preserved

tests/test_draft_knowledge_pack_builder.py
- section packs separate local/external evidence
- conflict notes are included
- source_map generated
```

### 14.2 Integration Tests

```text
tests/test_local_corpus_integration.py
- create temp workspace
- create sample md/txt/csv files
- run LocalCorpusService.index_workspace_sources
- verify manifest.json
- verify sources.json
- verify Chroma metadata or fake vector store calls
- retrieve with local filter
```

### 14.3 Regression Tests

기존 기능이 깨지면 안 된다.

- AutoSurvey external indexing still works
- `RAGService.index_autosurvey_output()` still indexes existing clean_md
- Verification `sections/reliability/consensus` still runs when no local corpus exists
- Draft generation still works when no local corpus exists

---

## 15. 완료 기준

이 작업은 다음 조건을 만족해야 완료다.

### Functional

- workspace에 local folder/files를 등록할 수 있다.
- md/txt/docx/pdf/csv/xlsx 중 MVP 지원 파일을 parsing할 수 있다.
- local extracted markdown과 manifest가 workspace/local 아래 저장된다.
- local source가 vector store에 `source_scope=local` metadata와 함께 index된다.
- RAG retrieval에서 external/local/all filter가 동작한다.
- Verification에서 local source를 읽을 수 있다.
- Crosscheck artifact를 생성하고 `verification/crosscheck.json`에 저장한다.
- Draft generation에서 local evidence를 section별 knowledge pack으로 사용할 수 있다.

### Architectural

- API route에 business logic이 없다.
- UI view에 business logic이 없다.
- parser/indexer/retriever/crosscheck/draft pack builder가 분리되어 있다.
- 기존 `IndexedDocRecord`를 local file용 fake model로 사용하지 않는다.
- local file을 external web document로 위장하지 않는다.
- `RAGService`가 god service로 비대해지지 않는다.
- `AgentRuntime`에 local corpus workflow 세부 구현을 넣지 않는다.

### Privacy

- local file content가 AutoSurvey query에 사용되지 않는다.
- local file content가 외부 LLM/API로 자동 전송되지 않는다.
- source metadata에 privacy label이 포함된다.
- local retrieval context 사용 시 provider policy check가 있다.

### Test

- unit/integration/regression test가 추가된다.
- fake LLM/fake vector store 기반으로 CI에서 실행 가능하다.
- 기존 테스트가 통과한다.

---

## 16. 구현 순서

반드시 다음 순서로 진행하라.

### Phase 0 — 구조 점검

- 현재 import graph 확인
- `core/models.py`를 패키지로 바꿀지, 신규 `core/knowledge_models.py`로 갈지 결정
- backward compatibility 우선

### Phase 1 — Domain model 추가

- `KnowledgeSourceRecord`
- `KnowledgeChunkRecord`
- `LocalFileManifestEntry`
- `TableProfile`
- `CrossCheckArtifact`
- `DraftKnowledgePack`

### Phase 2 — Workspace path 확장

- `workspace_paths.py`에 local/knowledge/verification/drafts accessor 추가
- path 조립 중복 제거

### Phase 3 — Local corpus ingestion

- FileScanner
- ParserRegistry
- TableProfiler
- ManifestRepository
- LocalCorpusService

### Phase 4 — Knowledge indexing/retrieval

- KnowledgeIndexer
- RetrievalService
- VectorStore metadata filter 확장
- 기존 RAGService와 최소 연결

### Phase 5 — API controller

- request/response models
- local_corpus route
- controller는 thin하게 유지

### Phase 6 — Verification 통합

- ArtifactLoader local loading 확장
- crosscheck task 추가
- crosscheck persistence 추가

### Phase 7 — Draft 통합

- KnowledgePackBuilder
- draft_service에서 새 builder 사용
- source_map 저장

### Phase 8 — Frontend 통합

- local folder selection UI
- indexing progress
- local sources list
- RAG source filter

### Phase 9 — Tests and cleanup

- 테스트 추가
- 기존 tests 통과 확인
- README 또는 services/README.md에 architecture update 작성

---

## 17. 주의할 구현 함정

1. `IndexedDocRecord`를 재사용하고 local file의 `url`에 `file://...`을 넣는 방식은 피하라. 웹 문서와 로컬 파일의 lifecycle이 다르다.
2. Excel 전체를 markdown으로 변환해 embedding하지 말라. row 폭발과 retrieval 품질 저하가 발생한다.
3. local file indexing이 AutoSurvey completion path에 강하게 결합되면 안 된다. AutoSurvey 없이도 local corpus만 index 가능해야 한다.
4. ChromaDB collection을 즉시 여러 개로 쪼개기보다 metadata filter 기반 통합을 먼저 구현하라. 단, collection 분리 가능성은 열어둔다.
5. Draft prompt에 local file 원문 전체를 넣지 말라. 반드시 retrieval/section pack을 거친다.
6. Verification crosscheck를 LLM-only로 만들지 말라. numeric/date/entity heuristic을 먼저 둔다.
7. path, source_id, chunk_id는 deterministic해야 한다. 재색인 때 source_map이 흔들리면 안 된다.
8. Windows file lock 문제를 고려하라. 기존 `VectorStore.close()`와 `release_chromadb_handles_for()` 패턴을 유지한다.
9. progress callback은 AutoSurvey와 유사한 shape를 유지하라.
10. 실패한 파일 하나 때문에 전체 indexing이 실패하면 안 된다. per-file error로 manifest에 기록하고 계속 진행한다.

---

## 18. Codex 작업 방식

작업 시 다음 원칙을 따른다.

1. 먼저 repository 구조를 읽고 현재 import/call graph를 파악한다.
2. 한 번에 모든 기능을 완성하려고 하지 말고 phase별 commit 가능한 단위로 구현한다.
3. 각 phase마다 테스트를 추가한다.
4. 기존 public method signature를 깨지 않는다.
5. 기존 UI/API가 동작하지 않게 만드는 rename은 피한다.
6. 새 기능은 backward-compatible default를 가진다.
7. local corpus가 없는 workspace에서도 기존 AutoSurvey/RAG/Verification/Draft가 동일하게 동작해야 한다.
8. 대형 리팩토링이 필요한 경우, compatibility shim을 먼저 추가하고 실제 migration은 후속 단계로 미룬다.

---

## 19. 최종 산출물

Codex는 작업 완료 후 다음을 보고하라.

```text
1. 추가/수정한 파일 목록
2. 새 아키텍처 계층 설명
3. Local corpus indexing 흐름
4. RAG retrieval filter 동작 방식
5. Verification crosscheck 동작 방식
6. Draft KnowledgePack 동작 방식
7. Privacy guardrail 구현 위치
8. 실행한 테스트와 결과
9. 아직 남은 TODO / 후속 개선 사항
```

