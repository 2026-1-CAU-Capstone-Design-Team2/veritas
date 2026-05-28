"""Primitive telemetry normalization helpers.

After the rule-based pivot most of this module's bandit-era contents are
gone — feature vectors aren't projected into a learned policy anymore. What
remains is the *primitive dict* the CandidateFactory and the RuleEvaluator
both consume: a small set of numeric / boolean signals derived directly from
the rolling write telemetry.

Strict rule (locked in by the user's directive 2026-05-28): **no hard-coded
lexical-keyword features.** Anything like "if 근거 or 출처 appears in the
sentence" is a culture-/topic-specific heuristic that doesn't generalize and
will be a source of model bias for future users. This file only carries
purely numeric / structural primitives. If a future feature needs lexical
content, it must come from a model-driven signal (e.g. RAG retrieval
relevance), never from a hard-coded vocabulary.
"""
from __future__ import annotations

import math
from typing import Any


# ----------------------------------------------------------- numeric helpers


def clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def log_norm(x: float, cap: float) -> float:
    """log1p(x) / log1p(cap), clipped to [0, 1]. ``x < 0`` clamps to 0."""
    x = max(0.0, float(x))
    cap_val = max(float(cap), 1.0)
    return clip01(math.log1p(x) / math.log1p(cap_val))


def signed_log_norm(x: float, cap: float) -> float:
    """Like ``log_norm`` but keeps sign — for net_growth (positive = adding,
    negative = deleting)."""
    cap_val = max(float(cap), 1.0)
    sign = 1.0 if x >= 0 else -1.0
    return sign * clip01(math.log1p(abs(float(x))) / math.log1p(cap_val))


# ---------------------------------------------------- primitive extraction


def extract_primitive_features(
    *,
    observation: Any,
    idle_sec: float,
    stable_capture_count: int,
    added_chars_window: float,
    deleted_chars_window: float,
    recent_negative_rate: float,
    time_since_last_intervention: float,
    relevant_sources_available: bool,
) -> dict[str, float | int | str | bool]:
    """Build the primitive dict the CandidateFactory + RuleEvaluator share.

    All time/edit telemetry is owned by the orchestrator (it sees the
    observation stream); this function only does the *normalization*. No I/O,
    no RAG, no LLM — those are too expensive for an observe tick.

    ``observation`` is duck-typed to avoid a circular import on
    ``ProactiveObservation``.
    """
    surface = str(getattr(observation, "surface", ""))
    text = str(getattr(observation, "text", "") or "")
    cursor_index = getattr(observation, "cursor_index", None)
    paragraph = str(getattr(observation, "current_paragraph", "") or "")

    edit_volume = max(0.0, float(added_chars_window)) + max(0.0, float(deleted_chars_window))
    net_growth = float(added_chars_window) - float(deleted_chars_window)

    # churn_score: lots of edits but little net change — the user is rewriting
    # the same region rather than making forward progress. Structural, not
    # lexical: derived from character-count deltas only.
    churn_score = clip01(edit_volume / 300.0) * (
        1.0 - clip01(abs(net_growth) / (edit_volume + 1e-6))
    )

    document_len = float(len(text))
    paragraph_len = float(len(paragraph))
    if cursor_index is None or document_len <= 0:
        cursor_pos = 1.0
    else:
        cursor_pos = clip01(float(cursor_index) / max(document_len, 1.0))

    return {
        "idle_sec": float(idle_sec),
        "stable_capture_count": int(stable_capture_count),
        "added_chars_window": float(added_chars_window),
        "deleted_chars_window": float(deleted_chars_window),
        "edit_volume": edit_volume,
        "net_growth": net_growth,
        "churn_score": float(churn_score),
        "paragraph_len": paragraph_len,
        "document_len": document_len,
        "cursor_pos": float(cursor_pos),
        "relevant_sources_available": bool(relevant_sources_available),
        "recent_negative_rate": clip01(recent_negative_rate),
        "time_since_last_intervention": float(time_since_last_intervention),
        "surface_is_native": 1.0 if surface == "native_editor" else 0.0,
    }
