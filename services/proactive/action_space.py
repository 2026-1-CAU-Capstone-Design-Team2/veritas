"""Legacy action-mask module — superseded by the rule-based CandidateFactory.

The bandit era treated "which suggestion type to consider" as a *masking*
problem: a deterministic function would produce a list of arms, and the
suggestion policy (LinUCB) would pick among them.

In the rule-based pipeline the equivalent is :func:`candidates.build_candidates`,
which directly emits ``ProactiveTask`` objects with anchor binding + render
mode already chosen. There is no separate "mask" step.

This file is kept only so legacy test imports don't snap. New code should
import :func:`candidates.build_candidates` instead.
"""
from __future__ import annotations

from typing import Any

# Preserved for any caller that still reads the old constant names.
ENGAGE_ACTIONS: list[str] = ["no_op", "intervene"]

SUGGESTION_ACTIONS: list[str] = [
    "next_sentence",
    "paragraph_rewrite",
    "local_copyedit",
    "logic_flow_review",
    "evidence_or_citation_prompt",
    "recovery_or_integration_note",
    "long_paragraph_split",
]


def build_suggestion_action_mask(
    primitive: dict[str, Any],
    *,
    surface_is_native: bool | None = None,
) -> list[str]:
    """**Deprecated.** Use :func:`candidates.build_candidates` instead.

    Returns a coarse fallback mask for any callers still on the old API. The
    rule-based CandidateFactory does much more (anchor binding, render mode,
    confidence gating) and is the only function the orchestrator calls.
    """
    if surface_is_native is None:
        surface_is_native = bool(float(primitive.get("surface_is_native", 0.0)))
    if surface_is_native:
        return ["next_sentence"]
    return ["next_sentence", "paragraph_rewrite", "local_copyedit"]
