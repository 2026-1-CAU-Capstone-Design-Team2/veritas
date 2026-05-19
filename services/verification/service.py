"""VerificationService — single external entry point (VERIFY_DESIGN.md §6 / §8.7).

The facade owns the *connections, indices and caches* the task pipelines
share; the pipelines themselves stay pure-ish functions of the inputs. Every
heavy resource (Kiwi tokenizer, BM25 indices) is built once via
:class:`functools.cached_property` so a multi-task run pays the cost only on
the first task and a single-task rerun (``run(tasks=("sections",))``) during
tuning is cheap.

Layering (§1.2): this module sits *between* ``api/services/`` and the
algorithm layer; it imports from ``indexing/`` / ``sections/`` / ``consensus/``
/ ``reliability/`` but never from ``api/`` or ``frontend/``.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Callable, Sequence

from tools.loader import load_schema
from tools.verify_flow_planner_tool import VerifyFlowPlannerTool

from .artifact_loader import ArtifactLoader, key_points_from_docs
from .consensus.consensus_pipeline import run_consensus_pipeline
from .indexing.bm25_index import BM25Index
from .indexing.dense_index import DenseIndex
from core.models import ParsedDocRecord

from .models import (
    ChunkRecord,
    KeyPointRecord,
    VerificationArtifacts,
    VerificationConfig,
)
from .persistence import VerificationPersistence
from .reliability import run_reliability_pipeline
from .sections.section_pipeline import run_section_pipeline
from .tokenization import HybridTokenizer

# The flow-planner tool's schema lives next to its implementation. Resolved
# once at import time so per-run service construction stays cheap.
_FLOW_PLANNER_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "verify_flow_planner_tool"
    / "tool_schema.json"
)

# Task names the service dispatches when ``tasks`` is left at the default.
# The order here is the order pipelines execute. To run a subset during
# tuning, pass an explicit tuple to :meth:`VerificationService.run`.
ALL_TASKS: tuple[str, ...] = ("sections", "reliability", "consensus")
DEFAULT_TASKS: tuple[str, ...] = ALL_TASKS

# Progress callback: same shape as ``workflows.autosurvey_workflow``'s callback
# (§1.7), so the API ring buffer + frontend poller from research are reused
# unchanged. ``stage`` = phase name, ``message`` = user-facing line.
ProgressCallback = Callable[..., None]

Bm25Factory = Callable[[Sequence[str]], BM25Index]


def _default_bm25_factory(tokenizer: HybridTokenizer, cfg: VerificationConfig) -> Bm25Factory:
    """Factory that builds a fresh :class:`BM25Index` over the given texts.

    Closed over the shared tokenizer so every corpus (Key Points, sentences)
    goes through the same tokenization rules.
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
        llm=None,
        flow_planner_tool: VerifyFlowPlannerTool | None = None,
        bm25_factory: Bm25Factory | None = None,
        tokenizer: HybridTokenizer | None = None,
    ) -> None:
        self._workspace = workspace
        self._loader = artifact_loader
        self._dense = dense
        # ``flow_planner_tool`` is required for the sections task. Callers may
        # pass a pre-built instance — e.g. one already registered in the chat
        # ToolRegistry — or omit it and pass ``llm`` to let the service spin up
        # its own tool against the schema next to the implementation. The
        # reliability and consensus tasks don't need it; ``run`` raises a clear
        # error if sections is requested with neither supplied.
        self._llm = llm
        if flow_planner_tool is not None:
            self._flow_planner_tool: VerifyFlowPlannerTool | None = flow_planner_tool
        elif llm is not None:
            self._flow_planner_tool = VerifyFlowPlannerTool(
                schema=load_schema(_FLOW_PLANNER_SCHEMA_PATH),
                llm=llm,
            )
        else:
            self._flow_planner_tool = None
        self._cfg = config
        self._persistence = persistence
        self._tokenizer = tokenizer or HybridTokenizer()
        self._bm25_factory = bm25_factory or _default_bm25_factory(self._tokenizer, config)

    # -- input resources (cached once per service instance) -------------------

    @cached_property
    def docs(self) -> list[ParsedDocRecord]:
        return self._loader.load_docs(self._workspace)

    @cached_property
    def chunks(self) -> list[ChunkRecord]:
        return self._loader.load_chunks(self._workspace)

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
            if self._flow_planner_tool is None:
                raise RuntimeError(
                    "sections task needs the verify_flow_planner tool; "
                    "construct VerificationService with llm=... (auto-builds the tool) "
                    "or pass flow_planner_tool=... directly, or omit 'sections' from tasks."
                )
            cb("sections", "보고서 흐름 구성 중 (LLM)...")
            artifacts.sections = run_section_pipeline(
                docs=self.docs,
                dense=self._dense,
                flow_planner_tool=self._flow_planner_tool,
                request_text=self.request_text,
                plan=self.plan,
                grounding=self.grounding,
                cfg=self._cfg,
                tokenizer=self._tokenizer,
            )
            section_count = len(artifacts.sections.sections)
            assigned_sentences = sum(
                len(section.sentence_assignments)
                for section in artifacts.sections.sections
            )
            cb(
                "sections",
                f"흐름 구성 완료 · 섹션 {section_count}개 · 배치된 문장 {assigned_sentences}개"
                f" ({artifacts.sections.flow_source})",
                detail={
                    "sections": section_count,
                    "sentenceAssignments": assigned_sentences,
                    "flowSource": artifacts.sections.flow_source,
                },
            )
            completed.append("sections")

        if "reliability" in requested:
            if self._llm is None:
                raise RuntimeError(
                    "reliability task needs an LLM client; "
                    "construct VerificationService with llm=... "
                    "or omit 'reliability' from tasks."
                )
            cb("reliability", "출처 신뢰도 분석 중 (LLM)...")
            artifacts.reliability = run_reliability_pipeline(
                docs=self.docs,
                llm=self._llm,
                summary_dir=self._loader.summary_dir_for(self._workspace),
                cfg=self._cfg,
                request_text=self.request_text,
            )
            distribution = dict(artifacts.reliability.distribution)
            cb(
                "reliability",
                f"신뢰도 분석 완료 · 높음 {distribution.get('high', 0)}"
                f" · 중간 {distribution.get('medium', 0)}"
                f" · 낮음 {distribution.get('low', 0)}",
                detail={
                    "high": int(distribution.get("high", 0)),
                    "medium": int(distribution.get("medium", 0)),
                    "low": int(distribution.get("low", 0)),
                },
            )
            completed.append("reliability")

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


__all__ = ["VerificationService", "ProgressCallback", "ALL_TASKS", "DEFAULT_TASKS"]
