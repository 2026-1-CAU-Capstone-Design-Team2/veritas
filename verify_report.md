# Veritas Verification Layer — 절차 보고서

> **목적**: `services/verification/` 가 `runs/<workspace>/` 산출물을 입력으로 받아
> 세 가지 검증 산출물(섹션 클러스터링 · 의도 적합도 · 교차 출처 일치)을 만들기까지
> *어떤 순서로, 어떤 모듈을 거쳐, 어떤 데이터가 어떻게 변환되는지* 를 절차적으로 정리한다.
>
> 설계 명세(`VERIFY_DESIGN.md`)가 *무엇을·왜* 를 규정한다면, 본 문서는 *어떻게·언제* 를
> 실행 순서 그대로 추적한다. 구현된 모듈에 대한 직접 링크를 함께 제공한다.

---

## 0. 한눈에 보기

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Phase 0  artifact_loader                  runs/<ws>/ → 인메모리 도메인 모델 │
├─────────────────────────────────────────────────────────────────────────┤
│ Phase 1  공통 인덱스/리소스 구축          BM25(chunk·KP) · chunk 임베딩 stack │
├─────────────────────────────────────────────────────────────────────────┤
│ Phase 2  Task 1 — sections                must_cover → 섹션 · evidence · 라벨 │
├─────────────────────────────────────────────────────────────────────────┤
│ Phase 3  Task 2 — intent                  multi-query → facet · 의도점수 · gap │
├─────────────────────────────────────────────────────────────────────────┤
│ Phase 4  Task 3 — consensus               KP 그래프 → 클러스터 · 권위 · 충돌 │
├─────────────────────────────────────────────────────────────────────────┤
│ Phase 5  결과 직렬화                      VerificationArtifacts → JSON      │
└─────────────────────────────────────────────────────────────────────────┘
```

* Phase 0~4는 본 구현 범위(`VERIFY_DESIGN.md §8.1 ~ §8.6`)에서 완결.
* Phase 5(`service.py` + `persistence.py` + API thin wrapper)는 §8.7~§8.8 범위.

---

## 1. 입력 · 출력 인터페이스

### 1.1 입력 (모두 `runs/<workspace>/` 안에 이미 존재)

| 경로 | 데이터 | 사용처 |
|---|---|---|
| `summary/index.json` | 문서 메타 (`doc_id`, `domain`, `duplicate_of` …) | 모든 task |
| `summary/doc_<id>.md` | LLM 생성 doc 요약 (Summary / Key Points / Reliability Notes) | Task 3 |
| `summary/plan.json` | `topic`, `goal`, `must_cover[]`, `keywords[]` | Task 1·2 |
| `summary/grounding.json` | `grounded_terms[]`, `candidate_entities[]` | Task 2 |
| `summary/request.md` | 원본 사용자 요청 | Task 2 |
| `clean_md/<id>.md` | Crawl4AI 가 정제한 원문 | (도구가 직접 읽지는 않음 — chunk 텍스트가 ChromaDB 안에 이미 존재) |
| `chromadb/` | granite-embedding 으로 임베딩된 chunk 벡터 + 메타 | Task 1·2 dense 채널 |

### 1.2 출력 (in-memory `VerificationArtifacts` — §8.7 직렬화 이전)

```python
@dataclass
class VerificationArtifacts:
    sections: SectionResult | None    # Task 1
    intent: IntentResult | None       # Task 2
    consensus: ConsensusResult | None # Task 3
    config_hash: str                  # VerificationConfig.fingerprint()
```

각 task 결과의 정확한 dataclass 정의는 [services/verification/models.py](services/verification/models.py) 참고.

---

## 2. Phase 0 — Artifact 로딩

**진입점**: [ArtifactLoader](services/verification/artifact_loader.py)

| 호출 | 산출물 | 메모 |
|---|---|---|
| `load_plan(ws)` | `dict` (raw plan.json) | LLM 산출 dict 는 도메인 모델로 변환하지 않고 그대로 보존 |
| `load_grounding(ws)` | `dict` | 〃 |
| `load_request(ws)` | `str` | request.md 본문 |
| `load_docs(ws)` | `list[DocRecord]` | index.json 의 doc 목록 기준 + doc_*.md 파싱 |
| `load_key_points(ws)` | `list[KeyPointRecord]` | `load_docs` 결과를 flatten — KP·Reliability Note 모두 단일 claim 단위 |
| `load_chunks(ws)` | `list[ChunkRecord]` | ChromaDB `research_docs` 컬렉션에서 직접 read, L2-normalize |

### 절차

1. `index.json` 의 `records` 를 순회하며 `DocRecord` 한 개씩 생성.
2. 각 `doc_<id>.md` 를 `_split_sections` + `_extract_bullets` 로 파싱하여
   `summary` / `key_points` / `reliability_notes` / `keywords` 채움.
   * `# Fetch Error` 로 시작하는 stub 은 로그 경고 후 메타만 남기고 본문은 비움.
3. `clean_md/<id>.md` 가 존재하면 그대로 `clean_md_text` 에 보존(duplicate 문서는 skip).
4. `load_chunks` 는 ChromaDB 의 `get(include=["embeddings","documents","metadatas"])` 로
   전체 코퍼스를 한 번에 읽고 `(parent_doc_id, chunk_index)` 순으로 정렬 — ChromaDB 자체
   `get()` 순서는 명세상 비결정적이라 결정성 확보를 위해 명시 정렬.
5. `key_points_from_docs(docs)` 가 모든 KP 에 코퍼스-전역 `kp_id` 를 0부터 할당.

### 디스크 I/O 격리

`artifact_loader.py` + (예정) `persistence.py` 외 어떤 모듈도 디스크에 접근하지
않는다 (`VERIFY_DESIGN.md §1.2`). 다른 모듈은 dataclass 와 numpy 만 받는 순수 함수에 가깝다.

---

## 3. Phase 1 — 공통 인덱스/리소스 구축

세 task 가 공유하는 *연산 리소스* 를 한 번만 만들어 둔다. (§8.7 facade 가 도입되면
이 단계는 `@cached_property` 로 lazy build 된다.)

| 리소스 | 모듈 | 비고 |
|---|---|---|
| `HybridTokenizer` | [tokenization.py](services/verification/tokenization.py) | Kiwi(`NNG/NNP/VV/VA/SH/SN`) + Latin regex. Kiwi 초기화 비용 수백 ms — 서비스 수명 동안 재사용 |
| `BM25Index`(chunk) | [indexing/bm25_index.py](services/verification/indexing/bm25_index.py) | `build([c.text for c in chunks])` — Task 1·2 공유 |
| `BM25Index`(KP) | 〃 | `build([k.text for k in kps])` — Task 3 전용 |
| chunk 임베딩 행렬 `(N_chunk, d)` | [retrieval.py](services/verification/retrieval.py)::`stack_chunk_embeddings` | ArtifactLoader 가 이미 L2-normalize 해 둔 벡터를 vstack. Task 1·2 공유 |
| `DenseIndex` | [indexing/dense_index.py](services/verification/indexing/dense_index.py) | `llm.LLMClient` 래퍼 — `embed` / `embed_batch` 만 사용 |

---

## 4. Phase 2 — Task 1: 섹션 클러스터링

**진입점**: [`run_section_pipeline`](services/verification/sections/section_pipeline.py)

### 4.1 절차

```
plan.must_cover[]
   │ (1) _clean_must_cover: blank/non-string 제거
   ▼
must_cover: list[str]                                   ──┐
   │ (2) dense.embed(must_cover)                          │
   ▼                                                       │
cover_embeddings: np.ndarray (M, d)                       │
   │ (3) build_cosine_graph(threshold=cfg.section_query_edge_threshold)
   │ (4) detect_communities(resolution=cfg.community_resolution, seed=cfg.random_seed)
   ▼                                                       │
communities: list[list[int]]   # 섹션 단위 그룹              │
   │                                                       │
   │ for each section:                                     │
   │   (5) fused_chunk_scores_for_queries(                 │
   │           query_texts=[must_cover[i] for i in group], │  ← retrieval.py
   │           query_embeddings=cover_emb[group],          │
   │           chunk_bm25, chunk_embeddings, cfg,          │
   │           out_size=cfg.section_top_chunk)             │
   │     → list[(chunk_position, rrf_score)]               │
   │                                                       │
   │   (6) aggregate_chunk_scores_to_docs(                 │
   │           fused, chunks, top_k=cfg.doc_score_top_chunk)│
   │     → doc_scores: dict[doc_id, float]                 │
   │                                                       │
   │   (7) chunk_evidence 직렬화                            │
   │                                                       │
   │   (8) section_corpora[sid] = "\n".join(queries + top_chunk_texts)
   ▼                                                       │
sections: list[Section]                                   │
   │                                                       │
   │ (9) label_groups(section_corpora, tokenizer, cfg)     │  ← labeling.py
   ▼                                                       │
section.label_terms 채움                                  │
   │                                                       │
   │ (10) _detect_unmet_must_cover:                        │
   │      각 must_cover[i] 단독 단일-query RRF top1 < threshold
   ▼                                                       │
unmet: list[UnmetMustCover]                              ──┘
```

### 4.2 단계 상세

1. **must_cover 정제** — `_clean_must_cover` 가 빈 문자열·비-문자열 제거.
2. **임베딩** — `dense.embed` 1회 호출로 `(M, d)` 벡터 획득.
3. **유사도 그래프** — `build_cosine_graph` 가 cosine ≥ threshold 인 쌍만 edge.
   임베딩은 이미 L2-normalize 되어 있어 cosine 은 단순 dot product.
4. **Louvain community 분할** — `detect_communities` 가 결정적 시드로 가장 큰
   community 부터 정렬하여 반환. 고립 노드는 singleton.
5. **섹션 retrieval** — 그룹 안 모든 query 의 BM25 rank + dense rank 를 모아
   `reciprocal_rank_fusion` 으로 한 번에 fuse. RRF 는 incommensurable score
   문제를 회피하면서 채널 간 합의가 강한 chunk 를 상위로 끌어올린다.
6. **doc-level 집계** — 각 doc 의 top `doc_score_top_chunk` 개 chunk 점수의
   평균. 한 개의 강한 chunk 가 긴 doc 을 부풀리는 것을 막는다.
7. **chunk evidence** — fused 결과를 그대로 `ChunkEvidence` 로 직렬화.
8. **라벨 코퍼스** — 섹션의 query 텍스트와 top chunk 텍스트를 합쳐 한 개의
   pseudo-doc 으로 만든다. query 가 라벨을 anchor 하고, chunk 는 어휘를 넓힌다.
9. **c-TF-IDF 라벨링** — `label_groups` 가 섹션 간 어휘를 구분짓는 top n-gram
   추출. 외부 사전 0 (`§1.9` 금기).
10. **unmet must_cover 자기검증** — 각 must_cover 를 단일 query 로 다시 RRF 한
    top1 점수가 임계값 미만이면 코퍼스가 그 요청을 *실제로 못 받친다* 는 신호.

### 4.3 출력

```python
SectionResult(
    sections=[Section(id, origin_must_cover_indices, label_terms,
                      chunk_evidence: list[ChunkEvidence],
                      doc_scores: dict[str, float])],
    unmet_must_cover=[UnmetMustCover(index, text, top_rrf)],
)
```

---

## 5. Phase 3 — Task 2: 의도 적합도

**진입점**: [`run_intent_pipeline`](services/verification/intent/intent_pipeline.py)

### 5.1 절차

```
request.md, plan.{topic, goal, keywords[]}, grounding.{grounded_terms[]}
   │ (1) extract_intent_queries
   ▼
queries: list[Query]                                     ← facet_extraction.py
   │ (2) dense.embed(query_texts)
   ▼
query_embeddings: (M_query, d)
   │ (3) build_cosine_graph(threshold=cfg.intent_query_edge_threshold)
   │ (4) detect_communities  ← Task 1과 동일 알고리즘 재사용 (§4.5 DRY)
   ▼
facet_groups: list[list[int]]                            # intent facet
   │
   │ (5) build_query_doc_matrix
   │     for each query:
   │       fused_chunk_scores_for_queries([q_text], q_emb, ...)
   │       → aggregate_chunk_scores_to_docs → doc 점수
   ▼
query_doc_matrix: np.ndarray (M_query, N_doc)
   │ (6) aggregate_to_facet_matrix  (mean over each facet's rows)
   ▼
facet_doc_matrix: np.ndarray (N_facet, N_doc)
   │
   │ (7) label_groups(facet의 query 텍스트만, tokenizer, cfg)
   ▼
Facet.label_terms 채움
   │
   │ (8) compute_doc_intent_score(facet_doc_matrix, cfg)
   ▼
doc_intent_score: dict[doc_id, float]
   │
   │ (9) detect_coverage_gaps(facets, facet_doc_matrix, cfg)
   ▼
coverage_gap: list[CoverageGap]
```

### 5.2 단계 상세

1. **multi-query 추출** — `extract_intent_queries` 가 `request → plan.topic →
   plan.goal → plan.keywords → grounding.grounded_terms` 순서로 후보 query 를
   만들고, 텍스트 기준 case-insensitive dedupe.
2. **임베딩** — 한 번에 `embed_batch` 로 모두 임베딩.
3~4. **facet community 분할** — Task 1 의 `build_cosine_graph + detect_communities`
   를 그대로 호출. 알고리즘은 같고 query 만 다르다는 §4.5 DRY 원칙 그대로.
5. **query × doc 매트릭스** — query 별로 별도 RRF fuse 와 doc 집계를 한 뒤
   `(M_query, N_doc)` 매트릭스에 채움. `out_size=None` 으로 *모든* fused
   chunk 를 받아 doc 매핑이 누락 없이 일어나도록 한다.
6. **facet 단위 평균** — 각 facet 의 row 들을 평균하여 `(N_facet, N_doc)`.
7. **facet 라벨링** — facet 자신의 query 텍스트만으로 c-TF-IDF. chunk 텍스트를
   섞으면 라벨이 *어느 doc 이 우세한지* 신호로 흐릿해진다.
8. **doc 의도 점수** — `max / mean / breadth` 세 신호의 가중합. breadth 는
   facet 축 entropy(softmax × 5 후) 를 `ln(N_facet)` 로 정규화한 값으로,
   여러 facet 에 고르게 강한 doc 에 가중을 더 준다.
9. **coverage gap** — facet 별 최고 doc 점수가 `intent_coverage_gap_threshold`
   미만이면 gap — *intent 측 자기검증* (Task 1 의 unmet must_cover 와 대칭).

### 5.3 출력

```python
IntentResult(
    facets=[Facet(id, label_terms, origin_queries)],
    doc_facet_matrix: np.ndarray (N_facet, N_doc),
    doc_intent_score: dict[doc_id, float],
    coverage_gap: list[CoverageGap],
    doc_order: list[doc_id],   # 매트릭스 컬럼 alignment 의 기준
)
```

---

## 6. Phase 4 — Task 3: 교차 출처 일치

**진입점**: [`run_consensus_pipeline`](services/verification/consensus/consensus_pipeline.py)

### 6.1 절차

```
kps: list[KeyPointRecord]   (이미 ArtifactLoader 가 준비)
   │ (1) dense.embed([kp.text for kp in kps])
   ▼
kp_embeddings: (N_kp, d)        + 각 kp.embedding 캐시
   │ (2) build_concept_graph (dense + sparse 채널 → RRF fuse → edge weight)
   ▼
graph: nx.Graph
   │ (3) detect_communities → min_cluster_size 이상만 유지
   ▼
clusters: list[list[kp_position]]
   │
   │ (4) _safe_pagerank(graph)
   │ (5) compute_domain_authority(kps, clusters, cfg)     ← HITS on bipartite
   │ (6) label_groups(cluster_texts, tokenizer, cfg)
   │
   │ for each cluster:
   │   (7) Shannon diversity over domains
   │   (8) pagerank mean over cluster nodes
   │   (9) authority mean over cluster domains
   │   (10) composite = pagerank × diversity × authority_mean
   │   (11) detect_conflicts → semantic_split + cross_domain
   ▼
ConsensusResult(concept_clusters, domain_authority, conflicts)
```

### 6.2 단계 상세

1. **KP 임베딩** — KP 텍스트를 한 번에 임베딩, `kp.embedding` 에 캐시(외부에서
   재임베딩 없이 후속 분석 가능).
2. **concept 그래프** — `build_concept_graph` 가 dense cosine 과 BM25 두 채널의
   per-row ranking 을 `1/(k+rank)` 로 합산하여 edge weight 산출.
   `concept_edge_threshold_rrf` 이상만 유지. 두 채널이 *동시에* 가까워야
   엮이므로, 단순 lexical/topical 유사도와 구분되는 "concept" 응집이 된다.
3. **Louvain → 유효 클러스터** — `min_cluster_size` 미만 community 는 noise.
4. **PageRank** — concept 그래프 자체의 노드 중요도. 비수렴 시 uniform fallback.
5. **도메인 권위 (HITS)** — `(domain, kp)` 이분 그래프에서 cluster 내부에 한해
   edge 를 생성, edge weight 은 cluster 크기에 비례. 큰 다중-도메인 클러스터에
   많이 등장하는 도메인일수록 authority 상승. 외부 화이트리스트 0.
6. **c-TF-IDF 라벨** — 각 클러스터의 KP 텍스트 묶음을 pseudo-doc 으로 라벨링.
7. **Shannon diversity** — 도메인 분포 entropy(nats). 0 ↔ 단일 출처, `ln(D)`
   ↔ 균등. *교차 출처 합의* 신호.
8. **pagerank centrality** — 클러스터 노드들 PageRank 의 산술 평균. 크기에
   상관없이 "이 클러스터가 얼마나 중심적인 concept 인가" 를 측정.
9. **authority mean** — 클러스터에 참여하는 도메인들 HITS authority 의 평균.
10. **composite** = `pagerank × diversity × authority_mean` — concept 의 *질*
    (중심성) × *교차합의* × *출처 권위*. 세 신호 모두 0 이상이라 단조 가중.
11. **conflict 후보 검출** — 두 *기하학적·통계적* 신호로 한정:
    * `semantic_split` — KMeans(k=2) 의 silhouette > threshold ↔ 클러스터가
      실제로 두 sub-concept 으로 깔끔히 갈라짐.
    * `cross_domain` — within-domain 평균 cos − between-domain 평균 cos >
      threshold ↔ 같은 출처는 모이고 다른 출처는 멀어짐.
    NLI/sentiment 같은 *언어적* 판정은 본 layer 범위 밖 (`§10`).

### 6.3 출력

```python
ConsensusResult(
    concept_clusters=[ConceptCluster(id, label_terms, kp_ids, domains,
                                     pagerank, diversity, authority_mean, composite)],
    domain_authority: dict[domain, float],
    conflicts=[ConflictFlag(cluster_id, type, score, evidence_kp_ids, partition?)],
)
```

---

## 7. 공통 알고리즘 모듈 (Task 간 공유)

| 모듈 | 함수 | 사용 task |
|---|---|---|
| [graph.py](services/verification/graph.py) | `build_cosine_graph`, `detect_communities` | T1 (must_cover) · T2 (intent query) · T3 (KP, RRF-fused 그래프에서 community 만 호출) |
| [labeling.py](services/verification/labeling.py) | `label_groups` (c-TF-IDF) | T1 · T2 · T3 |
| [retrieval.py](services/verification/retrieval.py) | `stack_chunk_embeddings`, `chunk_rankings_for_query`, `fused_chunk_scores_for_queries`, `aggregate_chunk_scores_to_docs` | T1 · T2 |
| [indexing/bm25_index.py](services/verification/indexing/bm25_index.py) | `BM25Index` | T1·T2 chunk corpus + T3 KP corpus |
| [indexing/dense_index.py](services/verification/indexing/dense_index.py) | `DenseIndex` | 전 task — embed 호출은 LLM 클라이언트의 embed 메서드만 (`§1.2`) |
| [indexing/rrf.py](services/verification/indexing/rrf.py) | `reciprocal_rank_fusion` | retrieval.py, concept_graph.py |

`VERIFY_DESIGN.md §4.5` 의 *공유 코어, 분리된 파이프라인* 원칙이 그대로 코드 구조에
반영되어 있다. 어떤 task pipeline 도 자체적인 retrieval/라벨/그래프 코드를 들고 있지
않다.

---

## 8. 절차상의 결정성 보증

verify 의 출력이 동일 입력 + 동일 config 에서 항상 같아야 디버깅·튜닝이 가능하다.
실제로 결정성을 깨뜨릴 수 있는 모든 지점에 결정성 시드/정렬을 박아두었다.

| 잠재적 비결정 | 처리 |
|---|---|
| `BM25Okapi.get_scores` 동점 | `np.argsort(kind="stable")` 사용 ([bm25_index.py:62](services/verification/indexing/bm25_index.py)) |
| `np.argsort` dense top-K 동점 | 〃 ([retrieval.py](services/verification/retrieval.py)) |
| RRF 점수 동점 | `(−score, item_id)` 2차 키 정렬 ([rrf.py:28](services/verification/indexing/rrf.py)) |
| Louvain 무작위 초기화 | `cfg.random_seed` 강제 주입 ([graph.py:50](services/verification/graph.py)) |
| KMeans 무작위 초기화 | `random_state=cfg.random_seed` ([conflict.py:46](services/verification/consensus/conflict.py)) |
| ChromaDB `get()` 순서 | `(parent_doc_id, chunk_index)` 명시 정렬 ([artifact_loader.py:286](services/verification/artifact_loader.py)) |
| HITS 수렴 실패 | `try/except PowerIterationFailedConvergence` → 0 fallback ([authority.py:52](services/verification/consensus/authority.py)) |
| PageRank 수렴 실패 | uniform fallback ([consensus_pipeline.py:48](services/verification/consensus/consensus_pipeline.py)) |

### config 지문(fingerprint)

[`VerificationConfig.fingerprint()`](services/verification/models.py)
가 모든 임계값·시드의 안정 해시(12 hex)를 반환한다. 결과 JSON 에 `config_hash`
로 기록되어, *어떤 config 로 만든 결과인지* 를 사후 추적할 수 있다.

---

## 9. 실측 절차 (MCP 워크스페이스, 473 chunks / 21 docs / ~ 300 KP)

[_verify_smoke_pipelines.py](_verify_smoke_pipelines.py) + [_verify_smoke3.py](_verify_smoke3.py)
가 LIVE granite-embedding (:8081) 모드에서 다음을 검증한다. 두 smoke 모두 100 % PASS.

| 절차 | 검증 항목 |
|---|---|
| Phase 0 | chunk 473 개 / doc 21 개 / KP ~ 300 개 정상 로딩 |
| Phase 1 | `stack_chunk_embeddings` shape · 누락 처리, BM25 양쪽 build |
| Phase 2 | section 수 ≥ 1, 각 section 에 chunk_evidence + doc_scores · label_terms, must_cover 인덱스가 빠짐없이 섹션에 배치 |
| Phase 3 | facet 수 ≥ 1, `doc_facet_matrix.shape == (N_facet, N_doc)`, `doc_intent_score` 가 모든 doc 포함 + finite, coverage_gap list 보장 |
| Phase 4 | concept cluster ≥ 1, `composite == pagerank × diversity × authority_mean`, 모든 KP 의 embedding 캐시 됨, conflict list 보장 |

실제 LIVE 결과 발췌(MCP 워크스페이스):

```
Task 1: 1 section, 10 chunk evidence, 5 docs
        labels: mcp, https, docs, and, to, ai, modelcontextprotocol.info
        top: doc=000 chunk=000:chunk_007 rrf=0.3391

Task 2: 1 facet, 21 docs
        labels: 활용, 실무, 코딩, 효율, 자동, 효율 코딩
        top doc by intent: doc=001 (0.0133), doc=000 (0.0108), doc=003 (0.0106)

Task 3: 12+ concept clusters, every domain in authority dict,
        composite formula reproduced exactly
```

(MCP 워크스페이스 자체가 한 가지 주제로 좁아 facet/section 이 1개로 수렴 — 코드의
*절차* 가 옳다는 검증이지, 알고리즘의 도메인 적용성을 판단할 자리는 아니다.)

---

## 10. 예외/경계 절차

| 상황 | 대응 |
|---|---|
| `plan.must_cover` 비어 있음 | `SectionResult()` 즉시 반환, 로그 경고 |
| `chunks` 비어 있음 | 〃 (모든 task 가 입력 빈 case 를 명시적으로 빠져나감) |
| `extract_intent_queries` 결과 0개 | `IntentResult(doc_order=…)` 만 채워 반환 |
| chunk 임베딩 일부 누락 | `stack_chunk_embeddings` 가 zero-row 로 메우고 cosine 채널에서 0 점수 → 자연 제외 |
| `doc_<id>_error.md` (fetch stub) | summary 파싱 skip, doc 메타만 보존 — duplicate/error 가 카운트는 유지되도록 |
| ChromaDB SQLite 핸들이 워크스페이스 락 | `store.close()` 를 `finally` 에 두어 Windows 락 누수 차단 ([artifact_loader.py:251](services/verification/artifact_loader.py)) |

---

## 11. 다음 절차 (본 구현 범위 밖)

`VERIFY_DESIGN.md §8.7~§8.10` 후속 단계:

* **§8.7 `service.py` facade** — `VerificationService.run(tasks, progress_callback)`
  로 위 Phase 0~4 를 캐시(`@cached_property`)·점진 실행(`tasks=…`) 가능하게 묶음.
* **§8.8 API 라우터** — `api/api_routes/verify.py` + `api/services/verify_service.py`
  thin wrapper. `AgentRuntime` 의 진행률 ring buffer 에 `ProgressEvent` 연결.
* **§8.9 통합 테스트** — `tests/services/verification/` 에 MCP fixture 스냅샷.
* **§8.10 `report.md` 렌더러** — Section/Intent/Consensus 를 한 마크다운으로
  요약 (사람 검수용).

본 보고서가 다루는 절차(Phase 0~4)는 위 후속 단계에서 *그대로 호출* 된다 —
facade 는 조립과 캐싱만 더하고 알고리즘은 추가하지 않는다 (`§1.1`).
