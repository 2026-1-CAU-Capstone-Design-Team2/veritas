"""RuleEvaluator — deterministic gate + score for ProactiveTask candidates.

Two layers, in this order:

1. **Hard gates** (``check_hard_gates``) — early-reject candidates that are
   structurally invalid (no anchor, wrong target, cooldown active,
   suppression active, etc). Returns a ``GateResult`` with structured
   reason codes so the operator can read /explain and see *exactly* why a
   candidate was vetoed.

2. **Rubric score** (``score_candidate``) — for candidates that pass the
   gates, compute a 0..1 score using the spec §6.2 linear combination of
   anchor_confidence / need_signal / context_sufficiency / task_fit /
   source_support − interruption_risk − recent_negative_rate.

Choosing a candidate is then:

    if score >= adjusted_threshold(candidate.task_type, anchor.id):
        emit candidate
    else:
        emit NullPrediction

Note: this module deliberately reads from ``UserAdaptationState`` but does
*not* mutate it. The orchestrator owns the adaptation lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .anchors import ActiveAnchor, MIN_CONFIDENCE_FOR_ACTIVE_SUGGESTION
from .candidates import PrimitiveSignals
from .proposal_models import ProactiveTask, SurfaceCapabilities


BASE_SHOW_THRESHOLD: float = 0.62
THRESHOLD_FLOOR: float = 0.35
THRESHOLD_CEIL: float = 0.90


@dataclass
class GateResult:
    """Outcome of the hard-gate phase.

    ``reasons`` is a list of short codes the operator can read in /explain.
    When ``allowed`` is False, the candidate is dropped — no score computed.
    """

    allowed: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class ScoreBreakdown:
    """Per-coefficient contribution to the final score — kept so /explain
    can show "anchor_confidence contributed +0.18, recent_negative_rate
    subtracted -0.05" and the operator can sanity-check the formula."""

    anchor_confidence: float = 0.0
    need_signal: float = 0.0
    context_sufficiency: float = 0.0
    task_fit: float = 0.0
    source_support: float = 0.0
    interruption_risk: float = 0.0
    recent_negative_rate: float = 0.0

    @property
    def total(self) -> float:
        raw = (
            0.30 * self.anchor_confidence
            + 0.20 * self.need_signal
            + 0.20 * self.context_sufficiency
            + 0.15 * self.task_fit
            + 0.10 * self.source_support
            - 0.20 * self.interruption_risk
            - 0.15 * self.recent_negative_rate
        )
        return max(0.0, min(1.0, raw))


# ----------------------------------------------------------- hard gates


def check_hard_gates(
    *,
    candidate: ProactiveTask,
    anchor: ActiveAnchor,
    signals: PrimitiveSignals,
    surface: SurfaceCapabilities,
    user_adaptation: Any = None,
) -> GateResult:
    """Run the spec §6.1 hard-gate checks.

    Each gate's reason code matches the spec exactly so /explain output
    stays readable across versions.
    """
    reasons: list[str] = []

    if not anchor.anchor_id:
        reasons.append("anchor_missing")

    if anchor.confidence < MIN_CONFIDENCE_FOR_ACTIVE_SUGGESTION:
        reasons.append("anchor_confidence_too_low")

    if candidate.target_anchor_id != anchor.anchor_id:
        reasons.append("off_anchor_target")

    if not surface.supports(candidate.render_mode):
        reasons.append("surface_render_unsupported")

    if not _context_is_present(anchor, candidate):
        reasons.append("context_insufficient")

    if _is_active_typing_unstable(signals, candidate):
        reasons.append("active_typing_not_stable")

    if user_adaptation is not None:
        if _is_anchor_task_on_cooldown(user_adaptation, anchor.anchor_id, candidate.task_type):
            reasons.append("cooldown_same_anchor_task")
        if _is_task_type_suppressed(user_adaptation, candidate.task_type):
            reasons.append("same_task_recently_rejected")
        if _is_in_recent_negative_streak(user_adaptation, signals):
            reasons.append("recent_negative_streak")

    if (
        candidate.task_type == "evidence_or_citation_prompt"
        and not signals.relevant_sources_available
        and not bool(candidate.metadata.get("allow_placeholder_only", True))
    ):
        reasons.append("no_relevant_source_for_strong_evidence_task")

    return GateResult(allowed=(not reasons), reasons=reasons)


def _context_is_present(anchor: ActiveAnchor, candidate: ProactiveTask) -> bool:
    """Sanity check: the scope the candidate names actually has data in the
    anchor. e.g. ``current_and_previous_paragraph`` without ``prev_paragraph``
    would force the generator to fall back to whole-document context, which
    the spec forbids."""
    scope = candidate.context_scope
    if scope in ("cursor_previous_sentences", "current_sentence"):
        return bool(anchor.sentence_text or anchor.paragraph_text)
    if scope == "current_paragraph":
        return bool(anchor.paragraph_text)
    if scope == "current_and_previous_paragraph":
        return bool(anchor.paragraph_text and anchor.prev_paragraph)
    if scope == "current_prev_next_paragraphs":
        return bool(anchor.paragraph_text and (anchor.prev_paragraph or anchor.next_paragraph))
    if scope == "claim_window":
        return bool(anchor.sentence_text or anchor.paragraph_text)
    if scope == "anchor_diff_region":
        # The candidate factory already required recent_diff_overlaps_anchor;
        # at evaluation time we trust that signal — but we still require some
        # text to anchor against.
        return bool(anchor.paragraph_text or anchor.sentence_text)
    if scope == "section_local_excerpt":
        return bool(anchor.section_heading)
    return bool(anchor.paragraph_text)


def _is_active_typing_unstable(signals: PrimitiveSignals, candidate: ProactiveTask) -> bool:
    """Some task types need a stable buffer before we run them — they're
    review/flow tasks. next_sentence is the exception: the user is *expected*
    to be at a brief pause."""
    if candidate.task_type in ("logic_flow_review", "long_paragraph_split"):
        return signals.idle_sec < 2.0 or signals.stable_capture_count < 1
    return False


def _is_anchor_task_on_cooldown(state: Any, anchor_id: str, task_type: str) -> bool:
    cooldown = getattr(state, "anchor_cooldowns", {}) or {}
    key = f"{anchor_id}|{task_type}"
    entry = cooldown.get(key) if isinstance(cooldown, dict) else None
    if not entry:
        return False
    from datetime import datetime, timezone

    until = _parse_iso(getattr(entry, "cooldown_until", None) or (entry.get("cooldown_until") if isinstance(entry, dict) else None))
    if until is None:
        return False
    return datetime.now(timezone.utc) < until


def _is_task_type_suppressed(state: Any, task_type: str) -> bool:
    stats_map = getattr(state, "task_type_stats", {}) or {}
    entry = stats_map.get(task_type) if isinstance(stats_map, dict) else None
    if not entry:
        return False
    suppressed_until = (
        getattr(entry, "suppressed_until", None)
        if not isinstance(entry, dict)
        else entry.get("suppressed_until")
    )
    from datetime import datetime, timezone

    until = _parse_iso(suppressed_until)
    if until is None:
        return False
    return datetime.now(timezone.utc) < until


def _is_in_recent_negative_streak(state: Any, signals: PrimitiveSignals) -> bool:
    """Two-part heuristic: very high recent_negative_rate AND user is still
    actively engaged (low idle). When idle is high, the user isn't going to
    see the next suggestion either way — let it through to keep the feedback
    loop alive."""
    stats = getattr(state, "global_stats", None)
    if stats is None:
        return False
    rate = float(getattr(stats, "recent_negative_rate", 0.0) or 0.0)
    return rate >= 0.85 and signals.idle_sec < 10.0


def _parse_iso(value: Any) -> Any:
    if not value:
        return None
    try:
        from datetime import datetime

        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


# ----------------------------------------------------------- rubric score


def score_candidate(
    *,
    candidate: ProactiveTask,
    anchor: ActiveAnchor,
    signals: PrimitiveSignals,
    user_adaptation: Any = None,
) -> ScoreBreakdown:
    """Compute the 0..1 rubric score per spec §6.2. Returns the breakdown
    so the score can be reproduced from /explain."""

    need = _need_signal(candidate.task_type, signals)
    ctx_suff = _context_sufficiency(candidate, anchor)
    fit = _task_fit(candidate.task_type, signals, anchor)
    source = _source_support(candidate.task_type, signals)
    interruption = _interruption_risk(signals, candidate)
    recent_neg = (
        float(getattr(getattr(user_adaptation, "global_stats", None), "recent_negative_rate", 0.0) or 0.0)
        if user_adaptation is not None
        else 0.0
    )

    return ScoreBreakdown(
        anchor_confidence=max(0.0, min(1.0, anchor.confidence)),
        need_signal=need,
        context_sufficiency=ctx_suff,
        task_fit=fit,
        source_support=source,
        interruption_risk=interruption,
        recent_negative_rate=max(0.0, min(1.0, recent_neg)),
    )


def _need_signal(task_type: str, s: PrimitiveSignals) -> float:
    """How urgently does the user need *this kind* of help right now?"""
    if task_type == "next_sentence":
        # caret pausing at end of sentence in a non-empty paragraph
        return _scale(s.idle_sec, lo=1.0, hi=8.0)
    if task_type == "paragraph_rewrite":
        return max(_scale(s.churn_score, lo=0.20, hi=0.70), 0.4 if s.recent_undo else 0.0)
    if task_type == "local_copyedit":
        return 0.6  # presence-detected; constant once the factory chose it
    if task_type == "logic_flow_review":
        return _scale(s.paragraph_len, lo=120, hi=500)
    if task_type == "evidence_or_citation_prompt":
        return max(0.4, _scale(s.evidence_need_score, lo=0.20, hi=0.80))
    if task_type == "recovery_or_integration_note":
        return 0.7 if s.recent_diff_overlaps_anchor else 0.0
    if task_type == "long_paragraph_split":
        return _scale(s.paragraph_len, lo=500, hi=1200)
    return 0.0


def _context_sufficiency(candidate: ProactiveTask, anchor: ActiveAnchor) -> float:
    """Quality proxy for whether the materialized context will be enough."""
    have = 0
    want = 0
    scope = candidate.context_scope
    if scope in ("cursor_previous_sentences", "current_sentence"):
        want, have = 1, int(bool(anchor.sentence_text))
    elif scope == "current_paragraph":
        want, have = 1, int(bool(anchor.paragraph_text))
    elif scope == "current_and_previous_paragraph":
        want = 2
        have = int(bool(anchor.paragraph_text)) + int(bool(anchor.prev_paragraph))
    elif scope == "current_prev_next_paragraphs":
        want = 3
        have = (
            int(bool(anchor.paragraph_text))
            + int(bool(anchor.prev_paragraph))
            + int(bool(anchor.next_paragraph))
        )
    elif scope == "claim_window":
        want, have = 1, int(bool(anchor.sentence_text or anchor.paragraph_text))
    elif scope == "anchor_diff_region":
        want, have = 1, int(bool(anchor.paragraph_text or anchor.sentence_text))
    elif scope == "section_local_excerpt":
        want = 2
        have = int(bool(anchor.section_heading)) + int(bool(anchor.paragraph_text))
    else:
        want, have = 1, int(bool(anchor.paragraph_text))
    return have / max(1, want)


def _task_fit(task_type: str, s: PrimitiveSignals, anchor: ActiveAnchor) -> float:
    """How well does the task type match the *kind* of moment this is?"""
    # next_sentence + at end of paragraph + paragraph not empty
    if task_type == "next_sentence":
        if anchor.paragraph_text and len(anchor.paragraph_text) >= 40:
            return 0.9
        return 0.5
    if task_type == "paragraph_rewrite":
        if s.paragraph_len >= 80 and s.churn_score >= 0.30:
            return 0.85
        return 0.5
    if task_type == "logic_flow_review":
        # better fit when at least both neighbors exist
        return 0.9 if (anchor.prev_paragraph and anchor.next_paragraph) else 0.6
    if task_type == "local_copyedit":
        return 0.7
    if task_type == "evidence_or_citation_prompt":
        return 0.8 if s.evidence_need_score >= 0.35 else 0.6
    if task_type == "recovery_or_integration_note":
        return 0.8 if s.recent_diff_overlaps_anchor else 0.3
    if task_type == "long_paragraph_split":
        return 0.85 if s.paragraph_len >= 700 else 0.55
    return 0.5


def _source_support(task_type: str, s: PrimitiveSignals) -> float:
    """Only evidence_or_citation actually depends on the source index. For
    every other task type, source_support is 1 (the coefficient already
    weights it down)."""
    if task_type == "evidence_or_citation_prompt":
        return 1.0 if s.relevant_sources_available else 0.2
    return 1.0


def _interruption_risk(s: PrimitiveSignals, candidate: ProactiveTask) -> float:
    """Heuristic 0..1: how disruptive would this be right now?"""
    # Active typing → high interruption risk for everything but next_sentence
    if s.idle_sec < 1.0 and candidate.task_type != "next_sentence":
        return 0.9
    # Long, focused writing burst → don't break flow with review tasks
    if s.edit_volume_window > 400 and candidate.task_type in ("logic_flow_review", "paragraph_rewrite"):
        return 0.6
    return 0.2


def _scale(value: float, *, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


# ----------------------------------------------------------- threshold


def adjusted_threshold(
    *,
    task_type: str,
    anchor_id: str,
    user_adaptation: Any = None,
    base: float = BASE_SHOW_THRESHOLD,
) -> float:
    """Spec §7.2: dynamic threshold per task / anchor.

    Returns ``+inf`` when the task/anchor is on a hard cooldown or the
    task_type is currently suppressed — those candidates can never beat
    the threshold, so the orchestrator gets a clean NullPrediction.
    """
    if user_adaptation is not None:
        if _is_anchor_task_on_cooldown(user_adaptation, anchor_id, task_type):
            return float("inf")
        if _is_task_type_suppressed(user_adaptation, task_type):
            return float("inf")

    offset = (
        float(getattr(user_adaptation, "threshold_offset", 0.0) or 0.0)
        if user_adaptation is not None
        else 0.0
    )
    recent_neg = (
        float(getattr(getattr(user_adaptation, "global_stats", None), "recent_negative_rate", 0.0) or 0.0)
        if user_adaptation is not None
        else 0.0
    )
    threshold = base + offset + 0.15 * recent_neg
    return max(THRESHOLD_FLOOR, min(THRESHOLD_CEIL, threshold))
