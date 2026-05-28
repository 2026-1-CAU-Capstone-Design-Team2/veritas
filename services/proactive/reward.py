"""Surface-specific feedback → canonical feedback → (engage, suggest) reward.

The canonical layer is the *single source of truth* the bandit consumes —
native TAB and external "복사" must collapse into the same ``accept`` (both
pay the same engage reward and the same suggestion reward) so the policy
doesn't see a phantom surface effect.

``like`` is preserved as an ``accept`` alias for legacy API payloads only;
new clients must emit native ``tab`` / external ``copy``.
"""
from __future__ import annotations

from typing import Any

CanonicalFeedbackStr = str  # alias for clarity at call sites


# Canonical reward table. Historical artifact from the bandit era — the
# rule-based system no longer uses ``engage_reward`` / ``suggestion_reward``
# numerically. UserAdaptationMemory has its own per-canonical update rules
# in ``adaptation.py``. The fields are kept here only so the legacy_bandit
# shadow telemetry can still read them.
CANONICAL_REWARD: dict[str, dict[str, float | None]] = {
    "accept": {"engage_reward": +1.0, "suggestion_reward": +1.0},
    "reject": {"engage_reward": -1.0, "suggestion_reward": -0.8},
    "retry": {"engage_reward": +0.3, "suggestion_reward": -0.2},
    "timeout": {"engage_reward": -0.3, "suggestion_reward": -0.3},
    "cancelled": {"engage_reward": -0.1, "suggestion_reward": None},
    # wrong_anchor: the suggestion landed at the wrong target. The
    # candidate extraction / context selection is at fault, not the user's
    # preference. UserAdaptationMemory treats this specially — it does NOT
    # update task_type EMA the way `reject` does (see adaptation.py §7.1).
    "wrong_anchor": {"engage_reward": -0.2, "suggestion_reward": None},
    # noop_* are legacy from the bandit's no-op outcome heuristic. The
    # rule-based system uses null_outcome_monitor.py instead.
    "noop_positive": {"engage_reward": +0.2, "suggestion_reward": None},
    "noop_negative": {"engage_reward": -0.2, "suggestion_reward": None},
}


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
    # anchor extraction, not the task_type. See §2.2 / §7.1.
    "wrong_anchor": "wrong_anchor",
    "off_target": "wrong_anchor",
    # legacy aliases — accept alias for `like/helpful`. See §5.3 of the spec.
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
    # "현재 위치와 관련 없음" button — see SuggestionCard frontend wiring.
    "wrong_anchor": "wrong_anchor",
    "off_target": "wrong_anchor",
    # legacy alias — see §5.3.
    "like": "accept",
    "helpful": "accept",
}


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
        mapped = NATIVE_FEEDBACK_TO_CANONICAL.get(action)
    elif surface == "external_screen":
        mapped = EXTERNAL_FEEDBACK_TO_CANONICAL.get(action)
    else:
        # Synthetic surface (no-op outcome monitor) — pass through canonical.
        mapped = action if action in CANONICAL_REWARD else None
    if mapped is None:
        # Last-resort: maybe the client already sent a canonical name.
        if action in CANONICAL_REWARD:
            return action
        return "timeout"
    return mapped


def reward_for(canonical: str) -> tuple[float | None, float | None]:
    """Return ``(engage_reward, suggestion_reward)`` for a canonical feedback.

    Unknown canonical → ``(None, None)`` (the orchestrator treats this as
    "skip update", same as cancelled when there is no engage signal).
    """
    entry = CANONICAL_REWARD.get(canonical)
    if entry is None:
        return None, None
    return entry.get("engage_reward"), entry.get("suggestion_reward")


def _coerce_metadata(metadata: Any) -> dict[str, Any]:
    return dict(metadata) if isinstance(metadata, dict) else {}


def describe_feedback(*, surface: str, raw_action: str, metadata: Any = None) -> dict[str, Any]:
    """Compact dict describing the (raw → canonical → reward) decomposition.

    Used by the API service layer when logging feedback so the JSONL has the
    full trace of how a button click became a reward, not just the final
    number. Avoids re-deriving the mapping at debug time.
    """
    canonical = canonicalize_feedback(surface=surface, raw_action=raw_action)
    engage_r, suggest_r = reward_for(canonical)
    return {
        "surface": surface,
        "raw_action": raw_action,
        "canonical": canonical,
        "engage_reward": engage_r,
        "suggestion_reward": suggest_r,
        "metadata": _coerce_metadata(metadata),
    }
