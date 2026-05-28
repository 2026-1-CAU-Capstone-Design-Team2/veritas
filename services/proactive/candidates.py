"""CandidateFactory — deterministic, anchor-local task generation.

Produces up to N (default 3) ``ProactiveTask`` candidates from a single
``ActiveAnchor`` + primitive telemetry + surface capabilities + the current
``UserAdaptationState``. **Never calls the LLM** — it's the cheap front gate
that runs every observe tick.

Design constraints (spec §5):
- Every candidate's ``target_anchor_id == anchor.anchor_id``. The factory
  cannot create a task that targets a different paragraph than the cursor
  is currently in.
- ``render_mode`` is filtered by ``SurfaceCapabilities`` before emit. Native
  rewrite candidates simply do not appear unless the inline-diff renderer
  reports itself as available.
- Each task type has explicit anchor-confidence + signal preconditions.
  When none are met, no candidate of that type is emitted.

The factory is intentionally rule-heavy and named-constant-rich so the
operator can read the source and predict its behavior without running it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from .anchors import (
    MIN_CONFIDENCE_FOR_ACTIVE_SUGGESTION,
    MIN_CONFIDENCE_FOR_FLOW_REVIEW,
    MIN_CONFIDENCE_FOR_REWRITE,
    ActiveAnchor,
)
from .proposal_models import (
    ContextScope,
    ProactiveTask,
    RenderMode,
    SurfaceCapabilities,
    TaskType,
)


MAX_CANDIDATES = 3


# ----------------------------------------------------------- input shape


@dataclass
class PrimitiveSignals:
    """Cheap rolling-window telemetry the factory consumes.

    Pure numeric/structural primitives — no lexical-keyword fields. See
    ``features.py``'s docstring for the rule about why we don't carry
    word-list-derived signals here.
    """

    idle_sec: float = 0.0
    stable_capture_count: int = 0
    edit_volume_window: float = 0.0
    net_growth_window: float = 0.0
    churn_score: float = 0.0
    paragraph_len: int = 0
    document_len: int = 0
    cursor_pos: float = 1.0  # 0..1 over document length
    relevant_sources_available: bool = False
    recent_diff_overlaps_anchor: bool = False
    recent_large_delete: bool = False
    recent_paste: bool = False
    recent_undo: bool = False

    @classmethod
    def from_primitive(cls, primitive: dict[str, Any]) -> "PrimitiveSignals":
        return cls(
            idle_sec=float(primitive.get("idle_sec", 0.0)),
            stable_capture_count=int(primitive.get("stable_capture_count", 0)),
            edit_volume_window=float(primitive.get("edit_volume", 0.0)),
            net_growth_window=float(primitive.get("net_growth", 0.0)),
            churn_score=float(primitive.get("churn_score", 0.0)),
            paragraph_len=int(float(primitive.get("paragraph_len", 0))),
            document_len=int(float(primitive.get("document_len", 0))),
            cursor_pos=float(primitive.get("cursor_pos", 1.0)),
            relevant_sources_available=bool(
                primitive.get("relevant_sources_available", False)
            ),
            recent_diff_overlaps_anchor=bool(
                primitive.get("recent_diff_overlaps_anchor", False)
            ),
            recent_large_delete=bool(primitive.get("recent_large_delete", False)),
            recent_paste=bool(primitive.get("recent_paste", False)),
            recent_undo=bool(primitive.get("recent_undo", False)),
        )


# ----------------------------------------------------------- helpers
#
# The detectors below are all *syntactic* / structural — sentence terminators
# are universal punctuation, the repeated-word regex matches any word-boundary
# pair regardless of meaning. We deliberately do NOT carry any lexical word
# list (the kind that would say "this paragraph mentions 근거 → needs source").
# Anything domain- or topic-specific must come from a learned/model signal,
# never from a hard-coded vocabulary.


_INCOMPLETE_SENTENCE_END = re.compile(r"[A-Za-z가-힣0-9,\s]$")  # not a terminator
_SENTENCE_TERMINATORS = ".!?。！？"

# Repeated whole word — purely structural ("the the", "이렇게 이렇게").
# Works for space-separated tokens; doesn't claim to catch every redundancy.
_AWKWARD_REPEAT = re.compile(r"(\b\w+\b)(?:\s+\1\b){1,}")


def _has_sentence_fragment(text: Optional[str]) -> bool:
    if not text:
        return False
    s = text.rstrip()
    if not s:
        return False
    return _INCOMPLETE_SENTENCE_END.search(s[-1:]) is not None and s[-1] not in _SENTENCE_TERMINATORS


def _at_end_of_paragraph(anchor: ActiveAnchor) -> bool:
    if anchor.cursor_index is None or anchor.paragraph_text is None:
        return False
    # We don't know where the paragraph starts in the document with full
    # precision here, but "the cursor is at or past the visible paragraph's
    # last char" is approximated by sentence_text being a suffix of
    # paragraph_text.
    sent = (anchor.sentence_text or "").rstrip()
    para = (anchor.paragraph_text or "").rstrip()
    if not sent or not para:
        return False
    return para.endswith(sent)


def _has_local_polish_signal(text: Optional[str]) -> bool:
    """Cheap proxy for 'this sentence has a local quality issue we could
    propose a copyedit on'. Doesn't try to actually find the bug — just
    detects classes of bug that exist."""
    if not text:
        return False
    s = text.strip()
    if not s:
        return False
    if _AWKWARD_REPEAT.search(s):
        return True
    # very long sentence — likely a runon
    if len(s) > 200:
        return True
    return False


def _actively_typing(signals: PrimitiveSignals) -> bool:
    """True iff the user is mid-keystroke — we don't want to interrupt with
    review/flow tasks while text is still settling."""
    return signals.idle_sec < 1.0 or signals.stable_capture_count < 1


# ----------------------------------------------------------- factory entry


def build_candidates(
    *,
    anchor: ActiveAnchor,
    signals: PrimitiveSignals,
    surface: SurfaceCapabilities,
    user_adaptation: Any = None,  # UserAdaptationState — typed loosely to avoid cycle
    max_candidates: int = MAX_CANDIDATES,
) -> list[ProactiveTask]:
    """Build up to ``max_candidates`` anchor-local task candidates.

    The returned list is sorted by descending intrinsic confidence — the
    RuleEvaluator may reorder by score, but for the case where the
    evaluator only takes the top-1 (current behavior) the factory's order
    is effectively the tie-breaker.
    """
    _ = user_adaptation  # reserved for future preference-aware shortlisting

    out: list[ProactiveTask] = []

    if not anchor.is_active_suggestion_capable():
        # Spec §3.2: low-confidence anchor → no active task. The orchestrator
        # will still emit a NullPrediction for telemetry.
        return out

    # evidence_or_citation_prompt was previously auto-triggered by lexical
    # keyword detection ("근거", "출처", ...). That heuristic is gone per the
    # user's directive against hard-coded keyword features — the task type
    # stays in the catalog but can only be invoked via an explicit signal
    # (e.g. a future "find evidence" UI button), never inferred from text.
    for builder in (
        _maybe_next_sentence,
        _maybe_local_copyedit,
        _maybe_paragraph_rewrite,
        _maybe_logic_flow_review,
        _maybe_recovery_or_integration_note,
        _maybe_long_paragraph_split,
    ):
        task = builder(anchor, signals, surface)
        if task is not None and surface.supports(task.render_mode):
            out.append(task)
        if len(out) >= max_candidates:
            break
    return out


# ----------------------------------------------------------- per-type builders


def _native_or_external(
    surface: SurfaceCapabilities,
    *,
    native: RenderMode,
    external: RenderMode,
) -> Optional[RenderMode]:
    if surface.surface == "native_editor" and surface.supports(native):
        return native
    if surface.surface == "external_app" and surface.supports(external):
        return external
    return None


def _maybe_next_sentence(
    anchor: ActiveAnchor,
    signals: PrimitiveSignals,
    surface: SurfaceCapabilities,
) -> Optional[ProactiveTask]:
    if anchor.confidence < MIN_CONFIDENCE_FOR_ACTIVE_SUGGESTION + 0.10:
        return None
    if not (anchor.cursor_index is not None or _has_sentence_fragment(anchor.sentence_text)):
        return None

    # The "user is paused" signal is surface-specific:
    # - **native_editor**: the frontend's debounce QTimer triggers /editor/suggest
    #   only AFTER the user stops typing. By the time observe runs, the pause
    #   has already happened — but the orchestrator's ``_DocumentTrack`` resets
    #   ``_last_mutation_ts`` to the captured_ts on every text-change, so
    #   ``idle_sec`` reads as 0 even though the user paused. We'd be double-
    #   counting if we re-gated on it. Trust the debounce.
    # - **external_app**: observe is a periodic capture poll (every ~5s) and
    #   fires regardless of activity. The idle gate is what distinguishes
    #   "actively typing in Word" from "paused in Word".
    if surface.surface != "native_editor":
        if signals.idle_sec < 2.0 or signals.stable_capture_count < 1:
            return None

    if not anchor.paragraph_text:
        return None
    if not (_at_end_of_paragraph(anchor) or _has_sentence_fragment(anchor.sentence_text)):
        return None
    render = _native_or_external(
        surface, native="native_ghost", external="external_card_blue"
    )
    if render is None:
        return None
    return ProactiveTask(
        task_type="next_sentence",
        target_anchor_id=anchor.anchor_id,
        context_scope="cursor_previous_sentences",
        render_mode=render,
        reason=(
            "native debounce implies pause"
            if surface.surface == "native_editor"
            else "external poll observed idle pause"
        ),
        confidence=anchor.confidence,
    )


def _maybe_paragraph_rewrite(
    anchor: ActiveAnchor,
    signals: PrimitiveSignals,
    surface: SurfaceCapabilities,
) -> Optional[ProactiveTask]:
    if anchor.confidence < MIN_CONFIDENCE_FOR_REWRITE:
        return None
    if signals.paragraph_len < 80:
        return None
    rewrite_signal = signals.churn_score >= 0.30 or signals.recent_undo or signals.recent_paste
    if not rewrite_signal:
        return None
    render = _native_or_external(
        surface, native="native_inline_diff", external="external_card_orange"
    )
    if render is None:
        return None
    return ProactiveTask(
        task_type="paragraph_rewrite",
        target_anchor_id=anchor.anchor_id,
        context_scope="current_paragraph",
        render_mode=render,
        reason="long paragraph with high churn / undo signal",
        confidence=anchor.confidence,
    )


def _maybe_local_copyedit(
    anchor: ActiveAnchor,
    signals: PrimitiveSignals,
    surface: SurfaceCapabilities,
) -> Optional[ProactiveTask]:
    if anchor.confidence < MIN_CONFIDENCE_FOR_REWRITE:
        return None
    target_text = anchor.sentence_text or anchor.paragraph_text
    if not target_text:
        return None
    if not _has_local_polish_signal(target_text):
        return None
    render = _native_or_external(
        surface, native="native_inline_marker", external="external_card_red"
    )
    if render is None:
        return None
    scope: ContextScope = "current_sentence" if anchor.sentence_text else "current_paragraph"
    return ProactiveTask(
        task_type="local_copyedit",
        target_anchor_id=anchor.anchor_id,
        context_scope=scope,
        render_mode=render,
        reason="repeated phrase / runon / awkward local",
        confidence=anchor.confidence,
    )


def _maybe_logic_flow_review(
    anchor: ActiveAnchor,
    signals: PrimitiveSignals,
    surface: SurfaceCapabilities,
) -> Optional[ProactiveTask]:
    if anchor.confidence < MIN_CONFIDENCE_FOR_FLOW_REVIEW:
        return None
    if not anchor.paragraph_text:
        return None
    if not (anchor.prev_paragraph or anchor.next_paragraph):
        return None
    if _actively_typing(signals):
        return None
    if signals.document_len < 300 and signals.paragraph_len < 200:
        return None
    render = _native_or_external(
        surface, native="native_inline_marker", external="external_card_gray"
    )
    if render is None:
        return None
    scope: ContextScope = (
        "current_prev_next_paragraphs"
        if (anchor.prev_paragraph and anchor.next_paragraph)
        else "current_and_previous_paragraph"
    )
    return ProactiveTask(
        task_type="logic_flow_review",
        target_anchor_id=anchor.anchor_id,
        context_scope=scope,
        render_mode=render,
        reason="paragraph + neighbor available, user idle enough",
        confidence=anchor.confidence,
    )


# _maybe_evidence_or_citation_prompt removed (2026-05-28). The auto-trigger
# relied on hard-coded Korean lexical keywords ("근거", "출처", "자료", ...)
# which don't generalize across domains/languages. ``evidence_or_citation_prompt``
# stays in the TaskType catalog so an explicit-invocation path (a future UI
# button, RAG retrieval signal, etc.) can still produce one — but the
# candidate factory will never propose it from free text again.


def _maybe_recovery_or_integration_note(
    anchor: ActiveAnchor,
    signals: PrimitiveSignals,
    surface: SurfaceCapabilities,
) -> Optional[ProactiveTask]:
    if anchor.confidence < MIN_CONFIDENCE_FOR_ACTIVE_SUGGESTION + 0.10:
        return None
    if not signals.recent_diff_overlaps_anchor:
        return None
    if not (signals.recent_large_delete or signals.recent_paste or signals.recent_undo):
        return None
    render = _native_or_external(
        surface,
        native="native_inline_diff" if surface.native_inline_diff else "native_inline_marker",
        external="external_card_gray",
    )
    if render is None:
        return None
    return ProactiveTask(
        task_type="recovery_or_integration_note",
        target_anchor_id=anchor.anchor_id,
        context_scope="anchor_diff_region",
        render_mode=render,
        reason="recent diff overlaps current anchor",
        confidence=anchor.confidence,
    )


def _maybe_long_paragraph_split(
    anchor: ActiveAnchor,
    signals: PrimitiveSignals,
    surface: SurfaceCapabilities,
) -> Optional[ProactiveTask]:
    if anchor.confidence < MIN_CONFIDENCE_FOR_ACTIVE_SUGGESTION + 0.10:
        return None
    if signals.paragraph_len < 500:
        return None
    if _actively_typing(signals):
        return None
    render = _native_or_external(
        surface, native="native_inline_marker", external="external_card_orange"
    )
    if render is None:
        return None
    return ProactiveTask(
        task_type="long_paragraph_split",
        target_anchor_id=anchor.anchor_id,
        context_scope="current_paragraph",
        render_mode=render,
        reason="paragraph ≥500 chars, user paused",
        confidence=anchor.confidence,
    )
