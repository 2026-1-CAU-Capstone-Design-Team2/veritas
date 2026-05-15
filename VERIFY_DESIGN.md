# Veritas Verification Layer — 설계 명세서

> **목적**: `runs/<workspace>/` 산출물(LLM이 만든 artifacts)을 입력으로 받아,
> LLM 추가 호출 없이 **임베딩 모델 + IR/NLP 알고리즘**만으로 다음 3가지 검증 산출물을 생성한다.
>
> 1. **섹션 클러스터링** — 보고서 섹션별 evidence chunk/document 후보
> 2. **의도 적합도** — 문서별 사용자 의도 facet 커버리지 매트릭스
> 3. **교차 출처 일치** — claim concept cluster + 합의/불일치 점수
>
> **설계 철학**: 외부 가설(키워드 사전, prototype 텍스트, 도메인 cue 등)을 코드에
> 인코딩하지 않는다. 모든 신호는 (a) artifacts 안에 이미 존재하는 텍스트, 또는 (b) 그
> 텍스트들 사이의 IR/NLP 알고리즘 결과로부터 유도한다. 도메인이 바뀌어도 코드는 그대로.

---

## 0. 입력 / 출력 / 위치

### 0.1 입력 (모두 기존 `runs/<workspace>/` 산출물)

| 경로 | 내용 | 용도 |
|---|---|---|
| `chromadb/` | granite-embedding으로 임베딩된 chunk 벡터 + 메타 | 모든 dense 채널의 source |
| `clean_md/NNN.md` | 정제된 raw 본문 | BM25 인덱스 source |
| `summary/doc_NNN.md` | LLM 생성 doc 요약 (Summary / Key Points / Reliability Notes) | claim 단위 입력 |
| `summary/index.json` | 문서 메타 (domain, search_query, duplicate_of) | 출처 다양성 계산 |
| `summary/plan.json` | LLM이 도출한 `topic`, `goal`, `must_cover[]`, `keywords[]` | 섹션·의도 query source |
| `summary/grounding.json` | `grounded_terms[]`, `candidate_entities[]` | 의도 facet source |
| `summary/request.md` | 원본 사용자 요청 | 의도 query source |

### 0.2 출력 (`runs/<workspace>/verification/`)

| 파일 | 스키마 (요약) |
|---|---|
| `sections.json` | `{ sections: [{ id, label_terms[], doc_score[], chunk_evidence[(doc_id, chunk_id, score)] }], unmet_must_cover: [] }` |
| `intent_coverage.json` | `{ facets: [{ id, label_terms[], origin_queries[] }], doc_facet_matrix: float[N_doc][N_facet], doc_intent_score: float[N_doc], coverage_gap: [] }` |
| `consensus.json` | `{ concept_clusters: [{ id, label_terms[], kp_ids[], domains[], pagerank, diversity, authority, composite }], domain_authority: {domain: float}, conflicts: [{ cluster_id, type, evidence[] }] }` |
| `report.md` (선택) | 사람이 한 번 읽고 검수할 수 있도록 위 3개를 요약한 마크다운 |

### 0.3 코드 위치 (ARCHITECTURE.md의 layering 준수)

```
services/
  verification/                           ← 신규 패키지. 도메인 서비스 레이어.
    __init__.py
    service.py                            ← VerificationService (외부 facade)
    artifact_loader.py                    ← runs/ 디스크 → 인메모리 도메인 모델
    models.py                             ← dataclass / pydantic 모델
    tokenization.py                       ← Kiwi + whitespace 토크나이저
    indexing/
      bm25_index.py                       ← BM25 래퍼
      dense_index.py                      ← ChromaDB / embed 래퍼
      rrf.py                              ← Reciprocal Rank Fusion
    sections/
      section_pipeline.py                 ← 섹션 task 진입점
      query_grouping.py                   ← must_cover community detection
      labeling.py                         ← c-TF-IDF 자동 라벨
    intent/
      intent_pipeline.py                  ← 의도 task 진입점
      facet_extraction.py                 ← request/plan/grounding → multi-query
      scoring.py                          ← facet × doc 집계
    consensus/
      consensus_pipeline.py               ← 합의 task 진입점
      kp_parser.py                        ← doc_*.md → Key Point 추출
      concept_graph.py                    ← 양채널 KP 그래프
      authority.py                        ← HITS / PageRank
      conflict.py                         ← sub-split / cross-domain disagreement
    persistence.py                        ← runs/<ws>/verification/ JSON IO

api/
  api_routes/verify.py                    ← 기존 라우터. 엔드포인트만 추가.
  services/verify_service.py              ← 라우터 뒤 thin wrapper (기존 패턴)
```

> ARCHITECTURE.md의 "Service = 상태/비즈니스 로직 소유자" 규칙을 따라,
> `services/verification/`이 모든 계산 로직과 상태를 갖는다. `api/services/`는 얇은 어댑터.

---

## 1. 소프트웨어 설계 원칙 (Claude Code가 따를 것)

### 1.1 단일 책임 / 파일 크기

* **한 파일 한 책임**. 각 `*_pipeline.py`는 *조립*만, 알고리즘 본체는 형제 모듈에 분리.
* **파일 LOC 가이드**: 일반 모듈 200~300 LOC를 넘기지 않는다. 넘으면 분할 신호.
* `service.py` (facade)는 의존성 주입과 호출 순서만 담당. 알고리즘 없음.

### 1.2 의존성 방향 (절대 어기지 말 것)

```
api_routes  ─▶  api/services  ─▶  services/verification/service
                                          │
                                          ▼
                            sections / intent / consensus pipelines
                                          │
                                          ▼
                               indexing / tokenization / models
                                          │
                                          ▼
                              llm.LLMClient (/embed 만), storage.VectorStore
```

* **`services/verification/` 내부에서 `api/` 또는 `frontend/` import 금지**.
* **`llm/`은 `embed` 메서드만 호출**. LLM 텍스트 생성 호출 금지.
* `core/prompts.py`는 verify와 무관 (LLM 호출 없음).
* I/O (디스크 읽기/쓰기)는 `artifact_loader.py`와 `persistence.py`에만 존재. 다른 모듈은 순수 함수에 가깝게.

### 1.3 데이터 모델 우선 (Models, not Maps)

`models.py`에 dataclass(또는 pydantic)로 명시:

* `ChunkRecord`, `DocRecord`, `KeyPointRecord`, `Query`
* `SectionResult`, `IntentResult`, `ConsensusResult`
* `VerificationArtifacts` — 위 셋의 컨테이너

> 코드 전반에서 `dict[str, Any]` 떠다니는 걸 금지. 타입 안정성 + 리팩터링 안전성을 위해.

### 1.4 알고리즘은 순수 함수, 상태는 서비스가 보유

* `concept_graph.build_graph(kps, sim_dense, sim_sparse, tau)` → `nx.Graph` (순수 함수)
* `VerificationService`만 ChromaDB connection·BM25 index·캐시를 보유.
* 테스트 시 순수 함수에는 numpy 배열만 주입하면 동작 (mock 불필요).

### 1.5 설정과 상수의 외부화

`models.py` 또는 별도 `config.py`에 dataclass로:

```python
@dataclass(frozen=True)
class VerificationConfig:
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    rrf_k: int = 60
    section_top_chunk: int = 10
    concept_edge_threshold: float = 0.78
    community_resolution: float = 1.0
    drift_tolerance: float = 0.3
    # ...
```

매직 넘버를 코드 안에 박지 않는다. 모든 임계값은 config 객체를 통해 흐른다.
나중에 워크스페이스별 튜닝(예: `runs/<ws>/verification/config.json`)이 필요해도 자연스럽게 확장.

### 1.6 점진적 산출 가능성 (Composable Pipelines)

3개 task는 서로 독립이지만 공유 인덱스(`BM25Index`, `DenseIndex`)를 쓴다.
`VerificationService.run()`은 다음 시그니처를 지원:

```python
def run(self, tasks: list[Literal["sections","intent","consensus"]] = ALL) -> VerificationArtifacts: ...
```

* 한 task만 재계산 가능 → 개발 중 빠른 iteration.
* 인덱스는 `@cached_property`로 lazy build, 첫 task에서만 비용 발생.

### 1.7 진행률 콜백 (기존 패턴 재사용)

`workflows/autosurvey_workflow.py`의 `progress_callback` 시그니처를 그대로 따라
`VerificationService.run(progress_callback=...)`를 제공.
API 라우터는 ring buffer에 이벤트를 emit하고, frontend는 폴링(기존 패턴).

### 1.8 테스트

* 알고리즘 본체(순수 함수)는 numpy fixture로 단위 테스트.
* 통합 테스트는 첨부 `MCP/` 워크스페이스 같은 소규모 샘플 1개 fixture로 충분.
* `tests/services/verification/` 하위.

### 1.9 금기 사항 (사용자 명시)

* 키워드/단어 리스트 하드코딩 금지 (`["OAuth", "PKCE", ...]` 같은 것).
* "이 표현은 정의 섹션이다" 류의 cue 사전 금지.
* prototype 텍스트를 코드에 박는 것 금지.
* 위 신호가 필요하면 **artifacts에서 추출하거나 알고리즘으로 도출**한다.

---

## 2. 공통 기반: Tokenizer · BM25 · Dense · RRF

### 2.1 Tokenizer (`tokenization.py`)

* 한국어/영어 혼재 코퍼스 대응.
* Kiwi로 한국어 형태소 분석, 의미 형태소 (체언/용언/외래어) 만 보존, 영어는 lower-case whitespace.
* **하드코딩 키워드 아님**: 일반적인 언어 처리, 도메인 독립적.

```python
# tokenization.py — 참고 구현
from kiwipiepy import Kiwi
import re

class HybridTokenizer:
    """ko 형태소 + en whitespace. 도메인 가정 없음."""
    KEEP_TAGS = ("N", "V", "SL", "SH", "SN")  # 명사/동사/외래/한자/수
    _kiwi: Kiwi | None = None

    def __init__(self) -> None:
        self._kiwi = Kiwi()

    def __call__(self, text: str) -> list[str]:
        out: list[str] = []
        for tok in self._kiwi.tokenize(text):
            if any(tok.tag.startswith(t) for t in self.KEEP_TAGS):
                out.append(tok.form.lower())
        # Kiwi가 한 영어 토큰을 그대로 형태소로 내주지 않는 경우 보강
        for m in re.findall(r"[A-Za-z][A-Za-z0-9_\-\.]+", text):
            out.append(m.lower())
        return out
```

> 토크나이저는 한 번 생성 후 서비스 수명 동안 재사용 (Kiwi 초기화 비용 ~수백 ms).

### 2.2 BM25 Index (`indexing/bm25_index.py`)

* `bm25s` 또는 `rank_bm25` 래핑.
* `build(corpus_texts, doc_ids)` / `score(query_text) -> np.ndarray` / `top_k(query_text, k)`.
* 두 곳에서 인스턴스화: **chunk-corpus용**, **kp-corpus용**.

### 2.3 Dense Index (`indexing/dense_index.py`)

* 기존 `storage.VectorStore` (ChromaDB) 를 chunk 측에서 그대로 활용.
* KP 측은 `llm.LLMClient.embed`로 배치 임베딩 후 `np.ndarray`로 보관 (메모리에 충분히 작음).
* `embed(texts) -> np.ndarray`, `score_against(query_emb, target_matrix) -> np.ndarray`.

### 2.4 RRF (`indexing/rrf.py`)

```python
# 한 함수, 한 책임.
def reciprocal_rank_fusion(
    rankings: list[list[int]],
    k: int = 60,
    out_size: int | None = None,
) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    fused = sorted(scores.items(), key=lambda x: -x[1])
    return fused[:out_size] if out_size else fused
```

---

## 3. Task 1 — 섹션 클러스터링

### 3.1 목표

* `plan.must_cover[]`를 출발점으로, **자동으로 N개의 보고서 섹션을 식별**하고
* 각 섹션마다 chunk-level evidence와 doc-level 후보를 ranking.
* 섹션 이름은 c-TF-IDF로 코퍼스 어휘에서 자동 추출 (외부 라벨 사전 0).

### 3.2 데이터 흐름

```
plan.must_cover[]
        │  ── embed ──▶ cover_emb (M, d)
        │
        │  community detection (Louvain on cosine graph)
        ▼
section_groups: List[List[int]]                    # 각 그룹 = 한 섹션
        │
        │  for each group:
        │     queries = [must_cover[i] for i in group]
        │     for q in queries:
        │        BM25 score over chunks
        │        cosine score over chunks
        │        → RRF fuse → chunk ranking
        │     section_chunk_scores = aggregate over queries
        ▼
section_evidence: {section_id: [(doc_id, chunk_id, score), ...]}
        │
        │  c-TF-IDF on (must_cover texts ∪ top-K chunk texts) per section
        ▼
section_labels: {section_id: [term, ...]}
```

### 3.3 핵심 구현 포인트 (참고 예시)

#### 3.3.1 query 그룹핑 (`sections/query_grouping.py`)

```python
# 참고 예시 — 정확한 임계값은 config에서 주입받도록 작성
import networkx as nx
import networkx.algorithms.community as nxcom

def group_queries(
    query_emb: np.ndarray,             # (M, d), L2-normalized
    edge_threshold: float,
    resolution: float,
) -> list[list[int]]:
    sim = query_emb @ query_emb.T
    G = nx.Graph()
    G.add_nodes_from(range(len(query_emb)))
    iu = np.triu_indices_from(sim, k=1)
    for i, j in zip(*iu):
        if sim[i, j] >= edge_threshold:
            G.add_edge(int(i), int(j), weight=float(sim[i, j]))
    communities = nxcom.louvain_communities(G, weight="weight", resolution=resolution)
    return [sorted(c) for c in communities]
```

#### 3.3.2 섹션 retrieval + RRF (`sections/section_pipeline.py`)

```python
# 참고 예시
def retrieve_section(
    queries: list[str],
    bm25: BM25Index,
    dense: DenseIndex,
    chunk_emb: np.ndarray,
    chunk_meta: list[ChunkRecord],
    cfg: VerificationConfig,
) -> list[ChunkScore]:
    rankings: list[list[int]] = []
    for q in queries:
        bm25_top = bm25.top_k(q, k=cfg.section_top_chunk * 5)
        q_emb = dense.embed([q])[0]
        cos = chunk_emb @ q_emb
        dense_top = np.argsort(-cos)[: cfg.section_top_chunk * 5].tolist()
        rankings.append(bm25_top)
        rankings.append(dense_top)
    fused = reciprocal_rank_fusion(rankings, k=cfg.rrf_k, out_size=cfg.section_top_chunk)
    return [ChunkScore(chunk_id=cid, score=s, meta=chunk_meta[cid]) for cid, s in fused]
```

#### 3.3.3 자동 라벨링 (`sections/labeling.py`) — c-TF-IDF

```python
# 참고 예시. 라벨 단어는 코퍼스에서 도출, 외부 사전 없음.
from sklearn.feature_extraction.text import TfidfVectorizer

def label_sections(
    section_corpora: dict[int, str],            # section_id → joined text (queries + top chunks)
    tokenizer,
    top_n: int = 8,
    ngram_range: tuple[int, int] = (1, 3),
) -> dict[int, list[str]]:
    sids = list(section_corpora.keys())
    docs = [section_corpora[s] for s in sids]
    vec = TfidfVectorizer(tokenizer=tokenizer, ngram_range=ngram_range, max_features=5000)
    M = vec.fit_transform(docs).toarray()
    terms = vec.get_feature_names_out()
    out: dict[int, list[str]] = {}
    for i, sid in enumerate(sids):
        top_idx = M[i].argsort()[-top_n:][::-1]
        out[sid] = [terms[t] for t in top_idx]
    return out
```

### 3.4 doc-level 집계

* `doc_section_score[d][s] = topK_mean(chunk_score[c][s] for c in chunks_of(d))`.
* `doc_*.md`의 `Summary` 임베딩도 보조 신호로 활용 가능 (선택). 본 task 핵심은 chunk-level RRF.

### 3.5 unmet must_cover 진단

각 `must_cover[i]`에 대해 RRF top score가 임계값 미만이면 → `unmet_must_cover`에 기록.
*plan이 식별한 gap을 verify가 데이터로 재검증* 하는 self-consistency check.

### 3.6 출력 (`sections.json`)

```json
{
  "config_hash": "...",
  "sections": [
    {
      "id": 0,
      "origin_must_cover_indices": [3, 7, 12],
      "label_terms": ["oauth", "pkce", "ssrf", "token", "authentication"],
      "chunk_evidence": [
        {"doc_id": "014", "chunk_id": 17, "rrf_score": 0.041},
        ...
      ],
      "doc_scores": {"014": 0.82, "017": 0.61, ...}
    }
  ],
  "unmet_must_cover": [
    {"index": 5, "text": "자동 테스트 생성 에이전트 구체적 설계", "top_rrf": 0.008}
  ]
}
```

---

## 4. Task 2 — 의도 적합도

### 4.1 목표

* 사용자 의도를 **multi-query**로 분해 (외부 query 작성 없이 artifacts에서 추출).
* facet × doc 매트릭스 + 문서별 의도 점수 + facet별 coverage gap.

### 4.2 데이터 흐름

```
request.md, plan.{topic, goal, keywords[]}, grounding.{grounded_terms[]}
        │
        ▼
queries: list[Query]                            # (origin, text, type)
        │
        │  embed → community detection (Task 1과 동일 알고리즘 재사용)
        ▼
intent_facets: list[list[query_idx]]
        │
        │  for each query: BM25 + dense → fused chunk score → topK_mean per doc
        ▼
query_doc_score: np.ndarray (M_query, N_doc)
        │
        │  group by facet (mean over queries in facet)
        ▼
facet_doc_matrix: np.ndarray (N_facet, N_doc)
        │
        ▼
doc_intent_score[d] = aggregate(facet_doc_matrix[:, d])
coverage_gap = [facet for facet if facet_doc_matrix[facet, :].max() < tau]
```

### 4.3 핵심 구현 포인트 (참고 예시)

#### 4.3.1 facet query 추출 (`intent/facet_extraction.py`)

```python
# 참고 예시 — origin 다양화는 robustness를 위한 것, 어떤 origin도 하드코딩 키워드 아님.
def extract_intent_queries(
    request_text: str,
    plan: dict,
    grounding: dict,
) -> list[Query]:
    qs: list[Query] = []
    qs.append(Query(origin="request", text=request_text, type="full"))
    if plan.get("topic"):
        qs.append(Query(origin="plan.topic", text=plan["topic"], type="topic"))
    if plan.get("goal"):
        qs.append(Query(origin="plan.goal", text=plan["goal"], type="goal"))
    for i, kw in enumerate(plan.get("keywords") or []):
        qs.append(Query(origin=f"plan.keyword[{i}]", text=kw, type="keyword"))
    for i, t in enumerate(grounding.get("grounded_terms") or []):
        qs.append(Query(origin=f"grounding.term[{i}]", text=t, type="term"))
    # de-duplicate by text
    seen, dedup = set(), []
    for q in qs:
        k = q.text.strip().lower()
        if k and k not in seen:
            seen.add(k); dedup.append(q)
    return dedup
```

#### 4.3.2 doc score 집계 (`intent/scoring.py`)

```python
# 참고 예시 — 가중치는 config로 외부화
def compute_doc_intent_score(
    facet_doc: np.ndarray,                # (N_facet, N_doc)
    weights: dict[str, float],            # {"max":0.4,"mean":0.3,"breadth":0.3}
) -> np.ndarray:
    abs_max = facet_doc.max(axis=0)
    mean = facet_doc.mean(axis=0)
    # breadth: facet 차원 entropy. 한 doc이 여러 facet에 고르게 강한가?
    p = softmax(facet_doc * 5.0, axis=0)
    breadth = -(p * np.log(p + 1e-9)).sum(axis=0)
    breadth_norm = breadth / np.log(facet_doc.shape[0])
    return (
        weights["max"] * abs_max
        + weights["mean"] * mean
        + weights["breadth"] * breadth_norm
    )
```

### 4.4 출력 (`intent_coverage.json`)

```json
{
  "facets": [
    {"id": 0, "label_terms": ["정의", "프로토콜", "표준"], "origin_queries": ["grounding.term[0]", "plan.keyword[3]"]},
    ...
  ],
  "doc_facet_matrix": [[0.81, 0.62, 0.31, 0.22], ...],
  "doc_intent_score": {"000": 0.71, "007": 0.68, ...},
  "coverage_gap": [
    {"facet_id": 2, "label_terms": ["테스트", "자동 생성"], "top_doc_score": 0.18}
  ]
}
```

### 4.5 Task 1과의 중복 줄이기

Task 1과 Task 2는 query만 다르고 retrieval 절차가 같음.
**공통 함수 `retrieve_for_queries(queries, bm25, dense, ...) -> np.ndarray (M_q, N_chunk)`**
를 `sections/` 외부 (또는 `indexing/`)에 만들어 양쪽에서 호출. DRY 준수.

---

## 5. Task 3 — 교차 출처 일치

### 5.1 목표

* `doc_*.md`의 Key Points를 코퍼스 내부 claim 단위로 보고,
* **양채널(BM25+dense) 유사도 그래프 + community detection**으로 concept cluster 식별.
* **HITS / PageRank**로 도메인 신뢰도를 코퍼스 내부에서 자동 도출 (외부 화이트리스트 없음).
* **sub-cluster split + cross-domain disagreement**로 conflict 후보 검출.

### 5.2 데이터 흐름

```
doc_*.md
   │  (parser: ## Summary / ## Key Points / ## Reliability Notes)
   ▼
key_points: list[KeyPointRecord]              # (text, doc_id, domain, kind)
   │
   │  embed + BM25 (KP corpus 내부)
   ▼
sim_dense, sim_sparse: (N_kp, N_kp)
   │
   │  RRF로 edge weight → 그래프
   ▼
concept_clusters: list[set[int]]
   │
   │  per cluster:
   │     domain_diversity (Shannon entropy)
   │     pagerank centrality
   │     cross-domain split score
   │     sub-cluster silhouette
   │
   │  도메인 노드 ↔ KP 노드 이분 그래프 → HITS
   ▼
domain_authority: dict[domain → float]
   ▼
consensus_score, conflict_flags
```

### 5.3 핵심 구현 포인트 (참고 예시)

#### 5.3.1 KP 파서 (`consensus/kp_parser.py`)

```python
# 참고 예시 — doc_*.md 형식은 LLM이 만든 정형 포맷, 정규식 충분.
import re
from pathlib import Path

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)

def parse_doc_summary(md_path: Path) -> ParsedDoc:
    text = md_path.read_text(encoding="utf-8")
    sections = _split_sections(text)
    summary = sections.get("Summary", "").strip()
    key_points = _extract_bullets(sections.get("Key Points", ""))
    reliability = _extract_bullets(sections.get("Reliability Notes", ""))
    return ParsedDoc(
        doc_id=md_path.stem.replace("doc_", ""),
        summary=summary,
        key_points=key_points,
        reliability_notes=reliability,
    )

def _extract_bullets(block: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^\s*-\s+(.+)$", block, re.M)]
```

> 형식이 깨진 doc (`doc_003_error.md`)는 loader 단에서 스킵하고 warning 로그.

#### 5.3.2 concept graph (`consensus/concept_graph.py`)

```python
# 참고 예시
def build_concept_graph(
    kp_emb: np.ndarray,
    bm25_kp: BM25Index,
    kps: list[KeyPointRecord],
    cfg: VerificationConfig,
) -> nx.Graph:
    n = len(kps)
    sim_d = kp_emb @ kp_emb.T

    # BM25 self-similarity
    sim_s = np.zeros((n, n))
    for i in range(n):
        sim_s[i] = bm25_kp.score(kps[i].text)
    sim_s = (sim_s + sim_s.T) * 0.5

    # 각각 rank를 매겨 RRF로 edge weight
    G = nx.Graph()
    G.add_nodes_from(range(n))
    rank_d = np.argsort(-sim_d, axis=1)
    rank_s = np.argsort(-sim_s, axis=1)
    rrf_full = np.zeros((n, n))
    for i in range(n):
        for r, j in enumerate(rank_d[i]):
            rrf_full[i, j] += 1.0 / (cfg.rrf_k + r)
        for r, j in enumerate(rank_s[i]):
            rrf_full[i, j] += 1.0 / (cfg.rrf_k + r)
    rrf_full = (rrf_full + rrf_full.T) * 0.5

    iu = np.triu_indices(n, k=1)
    for i, j in zip(*iu):
        w = float(rrf_full[i, j])
        if w >= cfg.concept_edge_threshold_rrf:
            G.add_edge(int(i), int(j), weight=w)
    return G
```

#### 5.3.3 도메인 authority (`consensus/authority.py`)

```python
# 참고 예시 — HITS on bipartite (domain ↔ KP).
def compute_domain_authority(
    kps: list[KeyPointRecord],
    concept_clusters: list[set[int]],
) -> dict[str, float]:
    B = nx.Graph()
    domains = sorted({k.domain for k in kps})
    for d in domains: B.add_node(("dom", d))
    for i, k in enumerate(kps): B.add_node(("kp", i))

    # cluster 내부 합의도가 edge weight로 흐름
    for cluster in concept_clusters:
        if len(cluster) < 2: continue
        # 한 cluster 안에서 각 (domain, kp) 연결을 가중
        for i in cluster:
            B.add_edge(("dom", kps[i].domain), ("kp", i),
                       weight=1.0 + 0.1 * len(cluster))

    hubs, auth = nx.hits(B, max_iter=200, normalized=True)
    return {d: float(auth[("dom", d)]) for d in domains}
```

#### 5.3.4 conflict 검출 (`consensus/conflict.py`)

두 신호를 합산하되 둘 다 *외부 단어 사전 없이* 기하학적/통계적 신호:

* **Sub-cluster silhouette**: cluster 내부 KMeans(k=2)의 silhouette가 임계값 초과 → 의미 분기.
* **Cross-domain disagreement**: within-domain mean similarity − between-domain mean similarity.

```python
# 참고 예시
def detect_conflicts(
    cluster: set[int],
    kps: list[KeyPointRecord],
    kp_emb: np.ndarray,
    cfg: VerificationConfig,
) -> list[ConflictFlag]:
    flags: list[ConflictFlag] = []
    idx = sorted(cluster)
    if len(idx) < 4: return flags
    sub_emb = kp_emb[idx]

    # (a) split
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    km = KMeans(n_clusters=2, n_init=5, random_state=0).fit(sub_emb)
    sil = silhouette_score(sub_emb, km.labels_)
    if sil > cfg.silhouette_split_threshold:
        flags.append(ConflictFlag(type="semantic_split", score=float(sil),
                                  partition={int(i): int(l) for i, l in zip(idx, km.labels_)}))

    # (b) cross-domain
    by_dom: dict[str, list[int]] = {}
    for i in idx: by_dom.setdefault(kps[i].domain, []).append(i)
    if len(by_dom) >= 2:
        within, between = [], []
        sim = sub_emb @ sub_emb.T
        local = {g_i: l_i for l_i, g_i in enumerate(idx)}
        from itertools import combinations
        for dom, ids in by_dom.items():
            for i, j in combinations(ids, 2):
                within.append(sim[local[i], local[j]])
        for (d1, ids1), (d2, ids2) in combinations(by_dom.items(), 2):
            for i in ids1:
                for j in ids2:
                    between.append(sim[local[i], local[j]])
        if within and between:
            diff = float(np.mean(within) - np.mean(between))
            if diff > cfg.cross_domain_disagreement_threshold:
                flags.append(ConflictFlag(type="cross_domain", score=diff))
    return flags
```

### 5.4 출력 (`consensus.json`)

```json
{
  "domain_authority": {"modelcontextprotocol.io": 0.18, "xenoss.io": 0.04, ...},
  "concept_clusters": [
    {
      "id": 0,
      "label_terms": ["mcp", "정의", "표준", "프로토콜"],
      "kp_ids": [3, 17, 41, 78],
      "domains": ["modelcontextprotocol.io", "modelcontextprotocol.info", "dev.to", "realpython.com"],
      "pagerank": 0.034,
      "diversity": 1.21,
      "authority_mean": 0.14,
      "composite": 0.0058
    }
  ],
  "conflicts": [
    {"cluster_id": 7, "type": "cross_domain", "score": 0.31, "evidence_kp_ids": [...]}
  ]
}
```

---

## 6. Facade (`service.py`) — 외부 단일 진입점

```python
# 참고 예시 — 알고리즘 본체 없음. 조립과 캐싱만.
class VerificationService:
    def __init__(
        self,
        workspace: str,
        artifact_loader: ArtifactLoader,
        bm25_factory,                 # callable[[corpus], BM25Index]
        dense: DenseIndex,
        config: VerificationConfig,
        persistence: VerificationPersistence,
    ):
        self.ws = workspace
        self._load = artifact_loader
        self._bm25f = bm25_factory
        self._dense = dense
        self._cfg = config
        self._save = persistence

    @cached_property
    def docs(self): return self._load.load_docs(self.ws)

    @cached_property
    def chunks(self): return self._load.load_chunks(self.ws)

    @cached_property
    def chunk_bm25(self): return self._bm25f([c.text for c in self.chunks])

    @cached_property
    def kps(self): return self._load.load_key_points(self.ws)

    @cached_property
    def kp_bm25(self): return self._bm25f([k.text for k in self.kps])

    def run(
        self,
        tasks: Sequence[str] = ("sections", "intent", "consensus"),
        progress_callback: Callable[[ProgressEvent], None] | None = None,
    ) -> VerificationArtifacts:
        cb = progress_callback or (lambda _e: None)
        out = VerificationArtifacts()

        if "sections" in tasks:
            cb(ProgressEvent("sections", "start"))
            out.sections = run_section_pipeline(
                docs=self.docs, chunks=self.chunks,
                bm25=self.chunk_bm25, dense=self._dense,
                plan=self._load.load_plan(self.ws),
                cfg=self._cfg,
            )
            cb(ProgressEvent("sections", "done"))

        if "intent" in tasks:
            cb(ProgressEvent("intent", "start"))
            out.intent = run_intent_pipeline(
                docs=self.docs, chunks=self.chunks,
                bm25=self.chunk_bm25, dense=self._dense,
                request=self._load.load_request(self.ws),
                plan=self._load.load_plan(self.ws),
                grounding=self._load.load_grounding(self.ws),
                cfg=self._cfg,
            )
            cb(ProgressEvent("intent", "done"))

        if "consensus" in tasks:
            cb(ProgressEvent("consensus", "start"))
            out.consensus = run_consensus_pipeline(
                kps=self.kps, kp_bm25=self.kp_bm25, dense=self._dense,
                cfg=self._cfg,
            )
            cb(ProgressEvent("consensus", "done"))

        self._save.persist(self.ws, out)
        return out
```

---

## 7. API 통합 (기존 패턴 준수)

```python
# api/api_routes/verify.py — 라우터 (얇게)
@router.post("/verify/run")
def run_verification(body: RunRequest, svc: VerifyApiService = Depends(...)):
    return svc.run(workspace=body.workspace, tasks=body.tasks)

@router.get("/verify/sections")
def get_sections(workspace: str, svc: VerifyApiService = Depends(...)):
    return svc.get_sections(workspace)

# ... intent, consensus, progress
```

`api/services/verify_service.py`는 `services.verification.VerificationService`를 호출하고
`AgentRuntime`의 진행률 ring buffer에 콜백을 연결.
**라우터에서 `services/verification/` 내부 모듈을 직접 import 하지 않는다.**

---

## 8. 구현 순서 (Claude Code 계획용)

1. **`models.py` + `VerificationConfig`** — 데이터 모델 먼저, 타입으로 인터페이스 고정.
2. **`artifact_loader.py`** — `runs/` 디스크에서 plan/grounding/request/doc_*.md/index.json을 로드. 첨부 `MCP/` 샘플로 동작 확인.
3. **`tokenization.py` + `indexing/`** — BM25/Dense/RRF 단위 동작 확인.
4. **Task 3 (consensus)** — KP가 가장 정형화되어 있어 작은 입력으로 빠른 iteration. 여기서 알고리즘 골조와 graph/HITS 검증.
5. **Task 1 (sections)** — Task 3의 그래프/community 코드를 부분 재사용.
6. **Task 2 (intent)** — Task 1의 retrieval 코드 재사용. Pipeline은 얇음.
7. **`service.py` 조립** — 캐시·진행률·persistence.
8. **API 라우터/서비스 thin wrapper**.
9. **테스트**: 첨부 MCP 워크스페이스로 end-to-end snapshot.
10. **(선택) `report.md` 렌더러** — 사람이 한 번 읽을 수 있는 요약 마크다운.

---

## 9. 성능 예산 (참고치, MCP 21 docs / ~100 KP / ~600 chunk)

| 단계 | 예상 시간 |
|---|---|
| artifact_loader (모든 산출물 파싱) | < 1 s |
| Kiwi 토크나이즈 (전체) | 2–4 s |
| chunk BM25 인덱스 | < 0.5 s |
| KP 임베딩 (~100건, granite-97M, CPU) | 5–10 s |
| query 임베딩 (~50건) | 3–5 s |
| 모든 RRF / numpy 행렬곱 | < 1 s |
| Louvain / HITS / silhouette | < 1 s |
| persistence (JSON write) | < 0.2 s |
| **합계 (cold)** | **15–25 s** |
| 단일 task 재실행 (cache hit) | **2–5 s** |

100 docs로 키워도 1분 이내. 캐시·점진 재계산 덕에 개발 중에는 훨씬 빠름.

---

## 10. 비-목표 (이번 verify 레이어 범위 밖)

* 보고서 초안 *생성* — 별도 단계 (draft_chat 등)에서 위 산출물을 evidence pack으로 받아 처리.
* LLM 기반 의미 contradiction 판정 (NLI 추론) — 본 레이어는 *후보 검출*까지. 사람/LLM이 마지막 판정.
* 한국어 외 형태소 분석 — 영어는 whitespace로 충분. 다국어 확장 시 `tokenization.py`만 교체.

---

## 11. 마지막 원칙 재확인

* **외부 가설 0**: 키워드 리스트, prototype 텍스트, 도메인 cue 사전 어떤 것도 코드에 박지 않는다.
* **artifacts 우선**: plan/grounding/doc_*.md가 신호의 원천. 알고리즘은 그것을 추출·집계·검증.
* **공유 코어, 분리된 파이프라인**: 토크나이저/BM25/Dense/RRF는 공유, task별 pipeline은 독립.
* **레이어 준수**: api → api.services → services.verification 단방향. service 내부에서 api/frontend import 금지.
* **모델 우선, dict 금지**: 인터페이스는 dataclass로 고정.
* **순수 함수 + 상태 분리**: 알고리즘은 순수 함수, 인덱스/캐시는 facade에만.
* **점진 실행 가능**: tasks 인자로 부분 재실행. 캐시는 cached_property로.

위 원칙이 지켜지면 도메인이 바뀌어도, 임베딩 모델이 바뀌어도, 새 task가 추가되어도
코드 구조가 그대로 유지된다.
