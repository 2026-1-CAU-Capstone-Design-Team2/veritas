"""Pure data shapes for the proactive bandit pipeline.

Kept as plain dataclasses (no Pydantic) so the policy / orchestrator core has
no FastAPI coupling — the API layer in ``api/api_models.py`` defines the HTTP
schemas separately and maps onto these.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Surface = Literal["native_editor", "external_screen"]

EngageAction = Literal["no_op", "intervene"]

SuggestionType = Literal[
    "next_sentence",
    "paragraph_rewrite",
    "local_copyedit",
    "logic_flow_review",
    "evidence_citation_prompt",
    "recovery_integration_note",
]

RenderMode = Literal[
    "none",
    "native_ghost",
    "native_inline_diff",
    "external_card",
]

ContextScope = Literal[
    "none",
    "previous_sentences",
    "current_paragraph",
    "current_sentence_and_paragraph",
    "previous_and_current_paragraph",
    "claim_window",
    "diff_region",
]

CanonicalFeedback = Literal[
    "accept",
    "reject",
    "retry",
    "timeout",
    "cancelled",
    "noop_positive",
    "noop_negative",
]


@dataclass
class ProactiveObservation:
    """One observation tick from a surface — native editor cursor moves and
    external screen captures both flow in as this."""

    surface: Surface
    workspace_id: str
    document_key: str
    document_id: str = ""
    source_app: str = ""
    window_title: str = ""

    # Full text may be present in memory only — the orchestrator passes it to
    # the generator via an in-memory decision cache; policy_state.json must
    # never persist it. See `policy_store.PolicyStore` invariants.
    text: str = ""
    cursor_index: int | None = None
    prefix: str = ""
    suffix: str = ""
    current_sentence: str = ""
    current_paragraph: str = ""
    previous_paragraph: str = ""
    changed_text: str = ""
    confidence: float = 0.0
    captured_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FeatureSnapshot:
    """Feature vectors + primitive dict captured at decision time so updates
    can replay them without re-computing — and so debug logs can carry exactly
    what the policy saw, not a re-derivation."""

    engage_features: list[float]
    engage_feature_names: list[str]
    suggest_features: list[float]
    suggest_feature_names: list[str]
    primitive: dict[str, float | int | str | bool]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProactiveDecision:
    """The output of ``ProactiveOrchestrator.observe``.

    ``candidate_suggestion_type`` is the LinUCB pick *before* the engage
    randomization; ``suggestion_type`` is set only when ``engage_action ==
    "intervene"``. Action-Centered Engage updates need both, because the
    counterfactual is "what would we have shown if we hadn't no-opped?"
    """

    decision_id: str
    surface: Surface
    workspace_id: str
    document_key: str

    candidate_suggestion_type: SuggestionType | None
    available_suggestion_actions: list[SuggestionType]

    engage_action: EngageAction
    should_intervene: bool
    intervention_probability: float

    suggestion_type: SuggestionType | None = None
    context_scope: ContextScope = "none"
    render_mode: RenderMode = "none"
    selected_context: dict[str, Any] = field(default_factory=dict)

    feature_snapshot: FeatureSnapshot | None = None
    policy_info: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FeedbackRecord:
    """The canonicalized feedback the orchestrator applied to the policies.

    ``engage_reward`` is always set (every canonical feedback maps to one).
    ``suggestion_reward`` is ``None`` for the no-op outcomes and ``cancelled``
    because those carry no signal about *which* suggestion type was good — only
    about whether intervening was good at all.
    """

    decision_id: str
    surface: Surface
    feedback_action: CanonicalFeedback
    engage_reward: float | None
    suggestion_reward: float | None
    recorded_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
