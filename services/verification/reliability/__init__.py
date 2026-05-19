"""Source reliability assessment (verify Task — replaces Task 2 intent_coverage).

The previous "의도 일치율" pipeline turned BM25 + Dense + RRF scores into a
single percentage, but the resulting number was both tiny in absolute value
(≲ 0.02) and impossible to explain to a user: it blended a max / mean /
breadth signal across LLM-derived facets. Users could see the percentage move
but could not see *why*.

This pipeline replaces that with an LLM-authored verdict on each document:

  level     ∈ {"high", "medium", "low"}  — overall trust band
  rationale  short Korean sentence(s) — why the LLM picked that level
  signals   {authority, verifiability, self_consistency} — sub-judgments
  batch_mentions  list of (batch_id, kind, snippet) — every place this doc
                  was cited in a batch_*.md "New Findings" / "Reliability
                  Notes" bullet, so the card can show concrete contributions

Two sub-modules:

* ``batch_index`` — parses ``summary/batch_*.md`` for ``[doc_<id>]`` markers
* ``llm_judge`` — runs the reliability prompt (5-doc batched) and returns the
  structured verdicts
"""

from .batch_index import BatchMention, build_batch_index
from .reliability_pipeline import (
    ReliabilityItem,
    ReliabilityMentionDTO,
    ReliabilityResult,
    run_reliability_pipeline,
)

__all__ = [
    "BatchMention",
    "build_batch_index",
    "ReliabilityItem",
    "ReliabilityMentionDTO",
    "ReliabilityResult",
    "run_reliability_pipeline",
]
