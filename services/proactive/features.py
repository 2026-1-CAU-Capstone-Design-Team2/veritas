"""Primitive telemetry → engage / suggest feature vectors.

The bandit *never* sees raw text and *never* sees the 23-scenario situation
labels: it only ever sees the normalized primitive features defined here. That
keeps the policy's input dimensionality fixed (so saved state can be reloaded
across app versions) and keeps the action mask — not the bandit — responsible
for capability gating.

Order matters: ``ENGAGE_FEATURE_NAMES`` / ``SUGGEST_FEATURE_NAMES`` are the
authoritative column ordering for everything that stores a feature vector
(``policy_state.json``, ``decisions.jsonl``, etc.). Adding or reordering a
column is a policy-state version bump.
"""
from __future__ import annotations

import math
import re
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


# ------------------------------------------------------- evidence heuristics

# Light-weight Korean + arabic evidence-need scoring. We avoid lexical
# dictionaries large enough to be culture-specific; the goal is "this sentence
# is making a claim that *probably* wants a number/source", not perfect
# classification.
_EVIDENCE_KEYWORDS = (
    "근거", "출처", "자료", "통계", "논문", "연구", "사례",
    "보고", "조사", "데이터", "실험", "결과",
)
_NUMBER_RE = re.compile(r"\d")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_PERCENT_RE = re.compile(r"\d+\s*%")


def compute_evidence_need(*, sentence: str, paragraph: str) -> float:
    """Heuristic 0..1 score for "does this passage want a citation?"

    A sentence with a number and a question mark is the strongest signal;
    keyword hits are weaker but additive. We cap at 1.0 so a paragraph that
    matches everything is treated the same as one strong signal — bandit
    features should not have unbounded magnitude.
    """
    text = f"{sentence}\n{paragraph}".strip()
    if not text:
        return 0.0
    score = 0.0
    if _NUMBER_RE.search(text):
        score += 0.25
    if _YEAR_RE.search(text):
        score += 0.20
    if _PERCENT_RE.search(text):
        score += 0.20
    if "?" in sentence:
        score += 0.15
    hits = sum(1 for kw in _EVIDENCE_KEYWORDS if kw in text)
    score += min(0.40, hits * 0.10)
    return clip01(score)


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
    """Build the primitive dict the feature vectors are projected from.

    All time/edit telemetry is owned by the orchestrator (it sees the
    observation stream); this function only does the *normalization* and the
    cheap evidence-need heuristic. No I/O, no RAG, no LLM — those are too
    expensive for an observe tick.

    ``observation`` is duck-typed to avoid a circular import on
    ``ProactiveObservation``.
    """
    surface = str(getattr(observation, "surface", ""))
    text = str(getattr(observation, "text", "") or "")
    cursor_index = getattr(observation, "cursor_index", None)
    paragraph = str(getattr(observation, "current_paragraph", "") or "")
    sentence = str(getattr(observation, "current_sentence", "") or "")

    edit_volume = max(0.0, float(added_chars_window)) + max(0.0, float(deleted_chars_window))
    net_growth = float(added_chars_window) - float(deleted_chars_window)

    # churn_score: lots of edits but little net change — the user is rewriting
    # the same region rather than making forward progress.
    churn_score = clip01(edit_volume / 300.0) * (
        1.0 - clip01(abs(net_growth) / (edit_volume + 1e-6))
    )

    document_len = float(len(text))
    paragraph_len = float(len(paragraph))
    if cursor_index is None or document_len <= 0:
        cursor_pos = 1.0
    else:
        cursor_pos = clip01(float(cursor_index) / max(document_len, 1.0))

    evidence_need = compute_evidence_need(sentence=sentence, paragraph=paragraph)

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
        "evidence_need_score": float(evidence_need),
        "relevant_sources_available": bool(relevant_sources_available),
        "recent_negative_rate": clip01(recent_negative_rate),
        "time_since_last_intervention": float(time_since_last_intervention),
        "surface_is_native": 1.0 if surface == "native_editor" else 0.0,
    }


# ----------------------------------------------------------- feature vectors

ENGAGE_FEATURE_NAMES: list[str] = [
    "bias",
    "idle_sec_norm",
    "stable_capture_count_norm",
    "edit_volume_norm",
    "churn_score",
    "paragraph_len_norm",
    "recent_negative_rate",
    "time_since_last_intervention_norm",
    "surface_is_native",
]


def build_engage_features(primitive: dict[str, Any]) -> list[float]:
    """Project primitives onto the engage policy's input vector.

    Engage policy decides "show candidate vs. no-op" only — it deliberately
    excludes net_growth / cursor_pos / evidence_need (those belong to *what*
    to suggest, not *whether* to suggest).
    """
    return [
        1.0,
        log_norm(float(primitive.get("idle_sec", 0.0)), 120.0),
        clip01(float(primitive.get("stable_capture_count", 0)) / 5.0),
        log_norm(float(primitive.get("edit_volume", 0.0)), 1500.0),
        clip01(float(primitive.get("churn_score", 0.0))),
        log_norm(float(primitive.get("paragraph_len", 0.0)), 2000.0),
        clip01(float(primitive.get("recent_negative_rate", 0.0))),
        log_norm(float(primitive.get("time_since_last_intervention", 0.0)), 1800.0),
        float(primitive.get("surface_is_native", 0.0)),
    ]


SUGGEST_FEATURE_NAMES: list[str] = [
    "bias",
    "net_growth_signed_norm",
    "churn_score",
    "paragraph_len_norm",
    "document_len_norm",
    "cursor_pos_norm",
    "evidence_need_score",
    "relevant_sources_available",
    "recent_negative_rate",
    "surface_is_native",
]


def build_suggest_features(primitive: dict[str, Any]) -> list[float]:
    """Project primitives onto the suggestion policy's input vector.

    Suggestion policy decides *which* type — so it sees the content-shape
    features (net_growth, cursor_pos, evidence_need) the engage vector skipped.
    """
    return [
        1.0,
        signed_log_norm(float(primitive.get("net_growth", 0.0)), 1500.0),
        clip01(float(primitive.get("churn_score", 0.0))),
        log_norm(float(primitive.get("paragraph_len", 0.0)), 2000.0),
        log_norm(float(primitive.get("document_len", 0.0)), 20000.0),
        clip01(float(primitive.get("cursor_pos", 0.0))),
        clip01(float(primitive.get("evidence_need_score", 0.0))),
        1.0 if bool(primitive.get("relevant_sources_available", False)) else 0.0,
        clip01(float(primitive.get("recent_negative_rate", 0.0))),
        float(primitive.get("surface_is_native", 0.0)),
    ]
