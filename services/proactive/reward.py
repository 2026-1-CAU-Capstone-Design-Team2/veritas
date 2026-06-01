"""Surface-specific raw feedback string → canonical feedback name.

The bandit era also had reward shaping here (``CANONICAL_REWARD`` table,
``reward_for()`` helper, ``describe_feedback()`` summary). The rule-based
system doesn't need them — ``adaptation.UserAdaptationMemory`` has its own
per-canonical update rules and never reads a numeric reward. This module
now does one job: collapse raw UI strings to the canonical name.

Canonical layer is the single source of truth the rule-based system
consumes — native TAB and external "복사" both flow into ``accept`` so the
adaptation policy doesn't see a phantom surface effect.
"""
from __future__ import annotations


NATIVE_FEEDBACK_TO_CANONICAL: dict[str, str] = {
    "tab": "accept",
    "accept": "accept",
    "esc": "reject",
    "reject": "reject",
    "retry": "retry",
    "rewrite": "retry",
    "timeout": "timeout",
    "ignored": "timeout",
    "cancelled": "cancelled",
    # User explicitly marked the suggestion as targeting the wrong location.
    # Handled distinctly from `reject` so the adaptation memory blames the
    # anchor extraction, not the task_type.
    "wrong_anchor": "wrong_anchor",
    "off_target": "wrong_anchor",
    # legacy aliases for pre-pivot clients still emitting "like"/"helpful"
    "like": "accept",
    "helpful": "accept",
    "dislike": "reject",
}


EXTERNAL_FEEDBACK_TO_CANONICAL: dict[str, str] = {
    "copy": "accept",
    "accept": "accept",
    "red_reject": "reject",
    "reject": "reject",
    "dislike": "reject",
    "retry": "retry",
    "regenerate": "retry",
    "timeout": "timeout",
    "ignored": "timeout",
    "cancelled": "cancelled",
    # "현재 위치와 관련 없음" SuggestionCard button.
    "wrong_anchor": "wrong_anchor",
    "off_target": "wrong_anchor",
    # legacy aliases
    "like": "accept",
    "helpful": "accept",
}


# Canonical names the adaptation layer knows how to handle.
_KNOWN_CANONICAL: frozenset[str] = frozenset({
    "accept",
    "reject",
    "retry",
    "timeout",
    "cancelled",
    "wrong_anchor",
    # synthetic — emitted by null_outcome_monitor, not by users
    "noop_positive",
    "noop_negative",
})


def canonicalize_feedback(*, surface: str, raw_action: str) -> str:
    """Map a surface-specific raw action string onto a canonical feedback.

    Falls back to ``timeout`` for unknown action strings on either surface —
    the alternative (raise) would lose the engage signal for misconfigured
    clients, which is worse than logging a slightly stale negative reward.
    """
    action = (raw_action or "").strip().lower()
    if not action:
        return "timeout"
    if surface == "native_editor":
        table = NATIVE_FEEDBACK_TO_CANONICAL
    elif surface == "external_screen":
        table = EXTERNAL_FEEDBACK_TO_CANONICAL
    else:
        # Synthetic surface (no-op outcome monitor) — pass canonical through.
        return action if action in _KNOWN_CANONICAL else "timeout"
    mapped = table.get(action)
    if mapped is not None:
        return mapped
    # Last-resort: maybe the client already sent a canonical name.
    return action if action in _KNOWN_CANONICAL else "timeout"
