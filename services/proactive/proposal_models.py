"""ProactiveTask / NullPrediction — the rule-based system's *plan*, not its
generated answer.

A ``ProactiveTask`` is what the CandidateFactory builds and the RuleEvaluator
scores: it carries the *intent* (task_type), the *target* (anchor_id), the
*context window* (context_scope), and the *render channel* (render_mode).
The Generator turns this plan into one LLM call when (and only when) the
evaluator decides the task is worth showing.

``NullPrediction`` is the explicit "we considered helping and chose not to"
return value. It's logged alongside its gate_reasons so an operator can
look at decisions.jsonl and see why the system stayed silent.

These are pure data shapes — no behavior, no I/O. Match the Bandit-era
``models.py`` style so the rest of the package can swap in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


TaskType = Literal[
    "next_sentence",
    "paragraph_rewrite",
    "local_copyedit",
    "logic_flow_review",
    "evidence_or_citation_prompt",
    "recovery_or_integration_note",
    "long_paragraph_split",
]


# Context scopes are all anchor-relative — note the absence of any
# "full_document" / "previous_section" scope. The rule-based pivot
# *forbids* selecting context from unrelated parts of the document.
ContextScope = Literal[
    "cursor_previous_sentences",
    "current_sentence",
    "current_paragraph",
    "current_and_previous_paragraph",
    "current_prev_next_paragraphs",
    "claim_window",
    "anchor_diff_region",
    "section_local_excerpt",
]


RenderMode = Literal[
    "native_ghost",
    "native_inline_diff",
    "native_inline_marker",
    "external_card_blue",
    "external_card_orange",
    "external_card_red",
    "external_card_green",
    "external_card_gray",
]


# Surface capabilities — drives the CandidateFactory's render gating. If the
# frontend doesn't implement ``native_inline_diff`` yet, ``paragraph_rewrite``
# is silently dropped from the native menu rather than producing a task that
# can't be rendered.
@dataclass
class SurfaceCapabilities:
    surface: Literal["native_editor", "external_app"]
    native_ghost: bool = True
    native_inline_diff: bool = False
    native_inline_marker: bool = False
    external_card: bool = True

    @classmethod
    def for_native(cls, *, inline_diff: bool = False, inline_marker: bool = False) -> "SurfaceCapabilities":
        return cls(
            surface="native_editor",
            native_ghost=True,
            native_inline_diff=inline_diff,
            native_inline_marker=inline_marker,
            external_card=False,
        )

    @classmethod
    def for_external(cls) -> "SurfaceCapabilities":
        return cls(
            surface="external_app",
            native_ghost=False,
            native_inline_diff=False,
            native_inline_marker=False,
            external_card=True,
        )

    def supports(self, render_mode: RenderMode) -> bool:
        if render_mode == "native_ghost":
            return self.surface == "native_editor" and self.native_ghost
        if render_mode == "native_inline_diff":
            return self.surface == "native_editor" and self.native_inline_diff
        if render_mode == "native_inline_marker":
            return self.surface == "native_editor" and self.native_inline_marker
        if render_mode.startswith("external_card"):
            return self.surface == "external_app" and self.external_card
        return False


@dataclass
class ProactiveTask:
    """The unit of "what to show, where, and from what context."

    ``confidence`` and ``evaluator_score`` are different things:
    - ``confidence`` is the CandidateFactory's a-priori "is this task even
      meaningful at this anchor?" 0..1 score.
    - ``evaluator_score`` is the RuleEvaluator's post-gate rubric score
      against the dynamic threshold. Only the latter decides whether the
      task is actually shown.
    """

    task_type: TaskType
    target_anchor_id: str
    context_scope: ContextScope
    render_mode: RenderMode
    reason: str = ""
    confidence: float = 0.0
    evaluator_score: float = 0.0
    gate_reasons: list[str] = field(default_factory=list)
    # Optional metadata the candidate factory wants to forward to the
    # generator (e.g. detected claim type for evidence_or_citation_prompt).
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class NullPrediction:
    """The "we chose to stay silent" result. Mirrors ProactiveTask so the
    decision log row schema is identical except for the discriminator.

    ``reason`` is the operator-facing short label (e.g. "score_below_threshold").
    ``gate_reasons`` is the structured list from the RuleEvaluator's hard
    gates, useful for the /explain endpoint."""

    reason: str
    gate_reasons: list[str] = field(default_factory=list)
    evaluator_score: float = 0.0
    candidate_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# Type alias for the top-level "what did observe decide?" return.
Prediction = ProactiveTask | NullPrediction


def is_task(prediction: Prediction) -> bool:
    return isinstance(prediction, ProactiveTask)


def is_null(prediction: Prediction) -> bool:
    return isinstance(prediction, NullPrediction)
