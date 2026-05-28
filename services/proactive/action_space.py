"""Suggestion-type action mask (primitive → list of allowed suggestion types).

This is the only place that gates *which* suggestion types are even candidates
for a given observation. Bandit features stay primitive-only; capability gates
(e.g. "need a real paragraph before paragraph_rewrite") live here so the policy
never has to learn a rule we already know.

``no_op`` is **not** a suggestion action. The engage policy in
``policies/action_centered_engage.py`` is the only thing that picks no-op vs.
intervene.
"""
from __future__ import annotations

from typing import Any

ENGAGE_ACTIONS: list[str] = ["no_op", "intervene"]

SUGGESTION_ACTIONS: list[str] = [
    "next_sentence",
    "paragraph_rewrite",
    "local_copyedit",
    "logic_flow_review",
    "evidence_citation_prompt",
    "recovery_integration_note",
]


def build_suggestion_action_mask(
    primitive: dict[str, Any],
    *,
    surface_is_native: bool | None = None,
) -> list[str]:
    """Return the ordered list of suggestion types eligible for this primitive.

    ``surface_is_native`` is consulted ahead of every other gate because the
    native ghost UI can only *insert* text at the cursor — anything other than
    a pure continuation (``next_sentence``) would dump commentary like
    "이 단락의 흐름이 어색해 보입니다..." into the ghost overlay, where
    a TAB accept would then insert that commentary into the document. Until
    the ``native_inline_diff`` renderer ships we narrow the native action
    space to ``next_sentence`` only — this also cleans up the reward signal
    because the user is no longer rejecting *misrouted* suggestions instead
    of *bad-quality* suggestions.

    When ``surface_is_native`` is ``None`` we infer it from
    ``primitive["surface_is_native"]`` (set by the feature extractor), so
    older test sites that pass only the primitive dict keep working.

    Order matches ``SUGGESTION_ACTIONS`` so any downstream consumer that picks
    by index sees a stable mapping. The fallback ``next_sentence`` floor keeps
    the suggestion policy non-empty when the user has barely written anything
    (the bandit can still no-op via the engage path).
    """
    if surface_is_native is None:
        surface_is_native = bool(float(primitive.get("surface_is_native", 0.0)))

    paragraph_len = float(primitive.get("paragraph_len", 0))
    document_len = float(primitive.get("document_len", 0))
    idle_sec = float(primitive.get("idle_sec", 0))
    churn = float(primitive.get("churn_score", 0))
    net_growth = float(primitive.get("net_growth", 0))
    evidence = float(primitive.get("evidence_need_score", 0))
    sources = bool(primitive.get("relevant_sources_available", False))

    actions: list[str] = []

    # ----- Native editor: paste-ready continuation only ----------------------
    if surface_is_native:
        # Even with a tiny doc we still allow next_sentence — the ghost UI is
        # the user's *only* native surface for the bandit right now, so a
        # narrower gate would silence the policy completely on a new file.
        actions.append("next_sentence")
        return actions

    # ----- External screen: full suggestion menu -----------------------------
    if document_len >= 20 and idle_sec >= 1.0:
        actions.append("next_sentence")

    if paragraph_len >= 60 and churn >= 0.20:
        actions.append("paragraph_rewrite")

    if paragraph_len >= 80:
        actions.append("local_copyedit")

    if document_len >= 600 or paragraph_len >= 350:
        actions.append("logic_flow_review")

    if evidence >= 0.25 and sources:
        actions.append("evidence_citation_prompt")

    if abs(net_growth) >= 250 or churn >= 0.55:
        actions.append("recovery_integration_note")

    if not actions:
        actions.append("next_sentence")

    return actions
