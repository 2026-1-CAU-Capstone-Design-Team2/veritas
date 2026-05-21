"""Task 1 entry point — sentence-level report-flow outline.

Pipeline (redesigned — see VERIFY_DESIGN §3 extended for sentence-flow):

1. ``sentence_splitter.split_docs_to_sentences`` — every kept doc's clean_md
   is decomposed into stable, addressable sentence units.
2. ``flow_planner.plan_report_flow`` — *one* LLM call decides the report's
   narrative outline (ordered, role-tagged sections with title+description).
3. ``sentence_retrieval.assign_sentences_to_sections`` — for each flow
   section, BM25 + dense ranking over sentences is RRF-fused, and every
   sentence is exclusively assigned to its best-fitting section.

The LLM is the only thing in this pipeline that isn't deterministic numpy
(§11 amended): retrieval and assignment remain pure functions, so for a
fixed outline + corpus the per-section sentence list is reproducible.
"""

from __future__ import annotations

import logging

from kiwipiepy import Kiwi

from tools.verify_flow_planner_tool import VerifyFlowPlannerTool

from ..indexing.bm25_index import BM25Index
from ..indexing.dense_index import DenseIndex
from core.models import ParsedDocRecord

from ..models import (
    SectionResult,
    SentenceUnit,
    VerificationConfig,
)
from ..tokenization import HybridTokenizer, create_kiwi
from .flow_planner import plan_report_flow
from .sentence_retrieval import assign_sentences_to_sections
from .sentence_splitter import split_docs_to_sentences

logger = logging.getLogger(__name__)


def run_section_pipeline(
    docs: list[ParsedDocRecord],
    dense: DenseIndex,
    flow_planner_tool: VerifyFlowPlannerTool,
    request_text: str,
    plan: dict,
    grounding: dict,
    cfg: VerificationConfig,
    *,
    tokenizer: HybridTokenizer | None = None,
    kiwi: Kiwi | None = None,
) -> SectionResult:
    """Build the ordered sentence-flow outline for this workspace.

    ``flow_planner_tool`` is the project's :class:`VerifyFlowPlannerTool` —
    the sole LLM consumer in verification (§11 amended). On any failure the
    pipeline degrades to a must_cover-based outline and flips ``flow_source``
    to ``"fallback"`` — the UI then warns the user. The retrieval half
    (sentence embedding + BM25 + RRF) is reached either way; a degraded
    outline still yields useful sentence assignments.
    """
    if not docs:
        logger.warning("verification: no documents to build a flow outline from")
        return SectionResult(flow_source="empty")

    tokenizer = tokenizer or HybridTokenizer()
    kiwi = kiwi or create_kiwi()

    # 1. Sentence decomposition.
    sentences: list[SentenceUnit] = split_docs_to_sentences(docs, cfg, kiwi=kiwi)
    if not sentences:
        logger.warning("verification: no sentences extracted from docs")
        return SectionResult(flow_source="empty", document_count=len(docs))

    # 2. LLM report-flow outline (or fallback). Delegated to the tool so the
    #    capability is also discoverable from the chat tool registry.
    sections, flow_source = plan_report_flow(
        flow_planner_tool=flow_planner_tool,
        request_text=request_text,
        plan=plan,
        grounding=grounding,
        docs=docs,
        cfg=cfg,
    )
    if not sections:
        return SectionResult(
            flow_source="empty",
            sentence_count=len(sentences),
            document_count=len({s.doc_id for s in sentences}),
        )

    # 3. BM25 index over sentences (built once, reused per section).
    sentence_bm25 = BM25Index(tokenizer, k1=cfg.bm25_k1, b=cfg.bm25_b).build(
        [s.text for s in sentences]
    )

    # 4. Exclusive sentence assignment (dense + BM25 → RRF).
    sections = assign_sentences_to_sections(
        sentences=sentences,
        sentence_bm25=sentence_bm25,
        sections=sections,
        dense=dense,
        cfg=cfg,
    )

    return SectionResult(
        sections=sections,
        flow_source=flow_source,
        sentence_count=len(sentences),
        document_count=len({s.doc_id for s in sentences}),
    )


__all__ = ["run_section_pipeline"]
