"""VerificationService — single external entry point (VERIFY_DESIGN.md §6 / §8.7).

The facade owns the *connections, indices and caches* the three task pipelines
share; the pipelines themselves stay pure-ish functions of the inputs. Every
heavy resource (Kiwi tokenizer, BM25 indices, chunk embedding matrix) is built
once via :class:`functools.cached_property` so a multi-task run pays the cost
only on the first task and a single-task rerun (``run(tasks=("sections",))``)
during tuning is cheap.

Layering (§1.2): this module sits *between* ``api/services/`` and the algorithm
layer; it imports from ``indexing/`` / ``sections/`` / ``intent/`` / ``consensus/``
but never from ``api/`` or ``frontend/``.
"""

from __future__ import annotations

from functools import cached_property
from typing import Callable, Sequence

import numpy as np

from .artifact_loader import ArtifactLoader, key_points_from_docs
from .consensus.consensus_pipeline import run_consensus_pipeline
from .indexing.bm25_index import BM25Index
from .indexing.dense_index import DenseIndex
from .intent.intent_pipeline import run_intent_pipeline
from .models import (
    ChunkRecord,
    DocRecord,
    KeyPointRecord,
    VerificationArtifacts,
    VerificationConfig,
)
from .persistence import VerificationPersistence
from .retrieval import stack_chunk_embeddings
from .sections.section_pipeline import run_section_pipeline
from .tokenization import HybridTokenizer

# Task names accepted by :meth:`VerificationService.run`. The order here is the
# order pipelines execute when ``tasks`` is left at the default.
ALL_TASKS: tuple[str, ...] = ("sections", "intent", "consensus")

# Progress callback: same shape as ``workflows.autosurvey_workflow``'s callback
# (§1.7), so the API ring buffer + frontend poller from research are reused
# unchanged. ``stage`` = phase name, ``message`` = user-facing line.
ProgressCallback = Callable[..., None]

Bm25Factory = Callable[[Sequence[str]], BM25Index]


def _default_bm25_factory(tokenizer: HybridTokenizer, cfg: VerificationConfig) -> Bm25Factory:
    """Factory that builds a fresh :class:`BM25Index` over the given texts.

    Closed over the shared tokenizer so every corpus (chunks, Key Points) goes
    through the same tokenization rules.
    """

    def _build(texts: Sequence[str]) -> BM25Index:
        return BM25Index(tokenizer, k1=cfg.bm25_k1, b=cfg.bm25_b).build(texts)

    return _build


class VerificationService:
    """Verification facade: assembles inputs, runs selected tasks, persists output.

    Constructed once per workspace per run; cached properties are scoped to the
    instance and so a stale workspace's chunks/embeddings can never leak into a
    fresh run (the caller drops the service and instantiates a new one).
    """

    def __init__(
        self,
        workspace: str,
        artifact_loader: ArtifactLoader,
        dense: DenseIndex,
        config: VerificationConfig,
        persistence: VerificationPersistence,
        *,
        bm25_factory: Bm25Factory | None = None,
        tokenizer: HybridTokenizer | None = None,
    ) -> None:
        self._workspace = workspace
        self._loader = artifact_loader
        self._dense = dense
        self._cfg = config
        self._persistence = persistence
        self._tokenizer = tokenizer or HybridTokenizer()
        self._bm25_factory = bm25_factory or _default_bm25_factory(self._tokenizer, config)

    # -- input resources (cached once per service instance) -------------------

    @cached_property
    def docs(self) -> list[DocRecord]:
        return self._loader.load_docs(self._workspace)

    @cached_property
    def chunks(self) -> list[ChunkRecord]:
        return self._loader.load_chunks(self._workspace)

    @cached_property
    def chunk_embeddings(self) -> np.ndarray:
        return stack_chunk_embeddings(self.chunks)

    @cached_property
    def chunk_bm25(self) -> BM25Index:
        return self._bm25_factory([chunk.text for chunk in self.chunks])

    @cached_property
    def key_points(self) -> list[KeyPointRecord]:
        return key_points_from_docs(self.docs)

    @cached_property
    def kp_bm25(self) -> BM25Index:
        return self._bm25_factory([kp.text for kp in self.key_points])

    @cached_property
    def plan(self) -> dict:
        return self._loader.load_plan(self._workspace)

    @cached_property
    def grounding(self) -> dict:
        return self._loader.load_grounding(self._workspace)

    @cached_property
    def request_text(self) -> str:
        return self._loader.load_request(self._workspace)

    # -- orchestration --------------------------------------------------------

    def run(
        self,
        tasks: Sequence[str] = ALL_TASKS,
        progress_callback: ProgressCallback | None = None,
    ) -> VerificationArtifacts:
        """Execute the requested tasks in order and persist the combined result.

        Tasks may be subsetted to ``("consensus",)`` etc. during development —
        cached_property indices ensure unused inputs are never loaded.
        Persistence writes only the files for ``completed`` so a partial rerun
        does not clobber other tasks' previously saved JSON on disk.
        """
        cb = progress_callback or (lambda *_a, **_k: None)
        requested = [task for task in tasks if task in ALL_TASKS]
        if not requested:
            cb("completed", "검증할 작업이 없습니다.", final=True)
            return VerificationArtifacts(config_hash=self._cfg.fingerprint())

        artifacts = VerificationArtifacts(config_hash=self._cfg.fingerprint())
        completed: list[str] = []

        cb(
            "start",
            f"검증 준비 중 · 문서 {len(self.docs)}개 · chunk {len(self.chunks)}개",
            detail={"documentCount": len(self.docs), "chunkCount": len(self.chunks)},
        )

        if "sections" in requested:
            cb("sections", "섹션 클러스터링 분석 중...")
            artifacts.sections = run_section_pipeline(
                chunks=self.chunks,
                chunk_bm25=self.chunk_bm25,
                dense=self._dense,
                plan=self.plan,
                cfg=self._cfg,
                chunk_embeddings=self.chunk_embeddings,
                tokenizer=self._tokenizer,
            )
            section_count = len(artifacts.sections.sections)
            unmet_count = len(artifacts.sections.unmet_must_cover)
            cb(
                "sections",
                f"섹션 분석 완료 · 섹션 {section_count}개 · 미충족 {unmet_count}개",
                detail={"sections": section_count, "unmet": unmet_count},
            )
            completed.append("sections")

        if "intent" in requested:
            cb("intent", "사용자 의도 적합도 분석 중...")
            artifacts.intent = run_intent_pipeline(
                docs=self.docs,
                chunks=self.chunks,
                chunk_bm25=self.chunk_bm25,
                dense=self._dense,
                request_text=self.request_text,
                plan=self.plan,
                grounding=self.grounding,
                cfg=self._cfg,
                chunk_embeddings=self.chunk_embeddings,
                tokenizer=self._tokenizer,
            )
            facet_count = len(artifacts.intent.facets)
            gap_count = len(artifacts.intent.coverage_gap)
            cb(
                "intent",
                f"의도 분석 완료 · 의도 그룹 {facet_count}개 · 부족한 의도 {gap_count}개",
                detail={"facets": facet_count, "gaps": gap_count},
            )
            completed.append("intent")

        if "consensus" in requested:
            cb("consensus", "교차 출처 합의 분석 중...")
            artifacts.consensus = run_consensus_pipeline(
                kps=self.key_points,
                kp_bm25=self.kp_bm25,
                dense=self._dense,
                cfg=self._cfg,
                tokenizer=self._tokenizer,
            )
            cluster_count = len(artifacts.consensus.concept_clusters)
            conflict_count = len(artifacts.consensus.conflicts)
            cb(
                "consensus",
                f"교차 출처 분석 완료 · 핵심 개념 {cluster_count}개 · 충돌 후보 {conflict_count}개",
                detail={"clusters": cluster_count, "conflicts": conflict_count},
            )
            completed.append("consensus")

        cb("persisting", "검증 결과 저장 중...")
        self._persistence.persist(
            self._workspace,
            artifacts,
            cfg=self._cfg,
            completed_tasks=completed,
            doc_count=len(self.docs),
        )
        cb(
            "completed",
            "검증 완료",
            detail={"completedTasks": list(completed)},
            final=True,
        )
        return artifacts


__all__ = ["VerificationService", "ProgressCallback", "ALL_TASKS"]
