"""Domain models and tunable configuration for the verification layer.

Every inter-module interface in ``services/verification/`` is a dataclass, not
a bare ``dict`` (VERIFY_DESIGN.md §1.3) — this keeps the pipelines type-safe and
refactor-safe. The LLM-authored JSON artifacts (``plan.json``,
``grounding.json``) are *external input*, not our domain model, so they stay
plain dicts and are only ever touched by ``artifact_loader``.

All tunable thresholds live on :class:`VerificationConfig` (§1.5) — no magic
numbers in algorithm code. The threshold *defaults* here are provisional: the
structure is fixed now, the values get tuned once the pipelines run against
real workspaces.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationConfig:
    """Immutable bundle of every tunable knob in the verification layer.

    Flat scalar fields only (no nested dicts) so the config stays hashable and
    a stable :meth:`fingerprint` can be written into output JSON. A future
    per-workspace ``runs/<ws>/verification/config.json`` can override these.
    """

    # --- BM25 (indexing/bm25_index.py) ---
    bm25_k1: float = 1.5
    bm25_b: float = 0.75

    # --- Reciprocal Rank Fusion (indexing/rrf.py) ---
    rrf_k: int = 60

    # --- Task 1: section clustering ---
    section_query_edge_threshold: float = 0.55  # cosine edge in must_cover graph
    community_resolution: float = 1.0           # Louvain resolution (shared T1/T2)
    section_top_chunk: int = 10                 # chunk evidence kept per section
    section_candidate_multiplier: int = 5       # per-query candidate pool = top_chunk * this
    unmet_must_cover_threshold: float = 0.01    # RRF score below this => unmet must_cover
    doc_score_top_chunk: int = 5                # topK chunks averaged into a doc-level score
    label_top_n: int = 8                        # c-TF-IDF terms kept per auto label
    label_ngram_min: int = 1
    label_ngram_max: int = 3
    label_max_features: int = 5000

    # --- Task 2: intent coverage ---
    intent_query_edge_threshold: float = 0.55   # cosine edge in intent-query graph
    intent_weight_max: float = 0.4              # doc_intent_score = max/mean/breadth blend
    intent_weight_mean: float = 0.3
    intent_weight_breadth: float = 0.3
    intent_coverage_gap_threshold: float = 0.3  # facet whose best doc score < this => gap

    # --- Task 3: cross-source consensus ---
    concept_edge_threshold_rrf: float = 0.012   # min fused RRF weight kept as a graph edge
    min_cluster_size: int = 2                   # clusters smaller than this are dropped
    conflict_min_cluster_size: int = 4          # conflict detection needs at least this many KPs
    silhouette_split_threshold: float = 0.45    # sub-cluster silhouette above => semantic split
    cross_domain_disagreement_threshold: float = 0.15
    hits_max_iter: int = 200

    # --- shared ---
    drift_tolerance: float = 0.3
    random_seed: int = 0

    def fingerprint(self) -> str:
        """Stable short hash of the config — emitted as ``config_hash`` in output JSON."""
        payload = json.dumps(dataclasses.asdict(self), sort_keys=True)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Input domain models — loaded from runs/<ws>/ by artifact_loader
# ---------------------------------------------------------------------------


@dataclass
class ChunkRecord:
    """One embedded chunk from ``runs/<ws>/chromadb/`` (granite-embedding, 384-d).

    ``chunk_id`` is ChromaDB's id: ``"<parent_doc_id>:chunk_<NNN>"``, or just
    ``"<parent_doc_id>"`` for a single-chunk document. ``embedding`` is filled
    by the loader (L2-normalized) so cosine similarity is a plain dot product.
    """

    chunk_id: str
    parent_doc_id: str
    chunk_index: int
    chunk_count: int
    text: str
    domain: str = ""
    title: str = ""
    url: str = ""
    search_query: str = ""
    embedding: np.ndarray | None = None


@dataclass
class DocRecord:
    """A research document: ``index.json`` metadata + parsed ``doc_<id>.md`` summary.

    Duplicate documents (``index.json`` ``duplicate_of`` set) have no clean_md
    file and therefore no chunks, but are still loaded — consensus/diversity
    needs to see them — with ``is_duplicate=True`` and an empty ``clean_md_text``.
    Fetch-error stubs (``doc_<id>_error.md``) are skipped entirely by the loader.
    """

    doc_id: str
    title: str = ""
    url: str = ""
    final_url: str = ""
    domain: str = ""
    search_query: str = ""
    duplicate_of: str | None = None
    is_duplicate: bool = False
    summary: str = ""
    key_points: list[str] = field(default_factory=list)
    reliability_notes: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    clean_md_text: str = ""  # full Crawl4AI clean markdown; "" for duplicates


@dataclass
class KeyPointRecord:
    """A single claim unit pulled from a ``doc_<id>.md`` summary.

    ``kind`` records which section it came from: ``"key_point"`` or
    ``"reliability_note"``. ``kp_id`` is a corpus-wide index assigned by the loader.
    """

    kp_id: int
    text: str
    doc_id: str
    domain: str
    kind: str
    embedding: np.ndarray | None = None


@dataclass
class Query:
    """A retrieval query derived from artifacts (request / plan / grounding).

    ``origin`` records provenance (e.g. ``"plan.keyword[3]"``) so coverage gaps
    can be traced back to their source. ``type`` is a coarse label
    (``"full" | "topic" | "goal" | "keyword" | "term" | "must_cover"``).
    """

    origin: str
    text: str
    type: str
    embedding: np.ndarray | None = None


# ---------------------------------------------------------------------------
# Output domain models — produced by the three task pipelines
# ---------------------------------------------------------------------------


@dataclass
class ChunkEvidence:
    """One chunk supporting a section, with its fused retrieval score."""

    doc_id: str
    chunk_id: str
    rrf_score: float


@dataclass
class Section:
    """An auto-identified report section (Task 1)."""

    id: int
    origin_must_cover_indices: list[int]
    label_terms: list[str]
    chunk_evidence: list[ChunkEvidence] = field(default_factory=list)
    doc_scores: dict[str, float] = field(default_factory=dict)


@dataclass
class UnmetMustCover:
    """A ``must_cover`` item the corpus does not actually support (Task 1 §3.5)."""

    index: int
    text: str
    top_rrf: float


@dataclass
class SectionResult:
    sections: list[Section] = field(default_factory=list)
    unmet_must_cover: list[UnmetMustCover] = field(default_factory=list)


@dataclass
class Facet:
    """A user-intent facet: a community of related intent queries (Task 2)."""

    id: int
    label_terms: list[str]
    origin_queries: list[str] = field(default_factory=list)


@dataclass
class CoverageGap:
    """An intent facet no document covers well (Task 2)."""

    facet_id: int
    label_terms: list[str]
    top_doc_score: float


@dataclass
class IntentResult:
    facets: list[Facet] = field(default_factory=list)
    # (N_facet, N_doc); columns aligned to doc_order.
    doc_facet_matrix: np.ndarray | None = None
    doc_intent_score: dict[str, float] = field(default_factory=dict)
    coverage_gap: list[CoverageGap] = field(default_factory=list)
    doc_order: list[str] = field(default_factory=list)


@dataclass
class ConceptCluster:
    """A community of cross-source Key Points expressing the same concept (Task 3)."""

    id: int
    label_terms: list[str]
    kp_ids: list[int]
    domains: list[str]
    pagerank: float
    diversity: float
    authority_mean: float
    composite: float


@dataclass
class ConflictFlag:
    """A candidate disagreement inside a concept cluster (Task 3 §5.3.4).

    ``type`` is ``"semantic_split"`` or ``"cross_domain"``. ``partition`` maps
    kp_id -> sub-cluster label, only set for semantic splits.
    """

    cluster_id: int
    type: str
    score: float
    evidence_kp_ids: list[int] = field(default_factory=list)
    partition: dict[int, int] | None = None


@dataclass
class ConsensusResult:
    concept_clusters: list[ConceptCluster] = field(default_factory=list)
    domain_authority: dict[str, float] = field(default_factory=dict)
    conflicts: list[ConflictFlag] = field(default_factory=list)


@dataclass
class VerificationArtifacts:
    """Container for the three task outputs (VERIFY_DESIGN.md §1.3)."""

    sections: SectionResult | None = None
    intent: IntentResult | None = None
    consensus: ConsensusResult | None = None
    config_hash: str = ""


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------


@dataclass
class ProgressEvent:
    """A progress tick from ``VerificationService.run``.

    Shaped to match ``workflows/autosurvey_workflow.py``'s callback contract
    (§1.7) so the API ring buffer + frontend poller patterns are reused
    unchanged. ``stage`` is the task name; ``status`` is ``"start"`` or ``"done"``.
    """

    stage: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "VerificationConfig",
    "ChunkRecord",
    "DocRecord",
    "KeyPointRecord",
    "Query",
    "ChunkEvidence",
    "Section",
    "UnmetMustCover",
    "SectionResult",
    "Facet",
    "CoverageGap",
    "IntentResult",
    "ConceptCluster",
    "ConflictFlag",
    "ConsensusResult",
    "VerificationArtifacts",
    "ProgressEvent",
]
