"""ActiveAnchor — where the user is editing right now.

The rule-based proactive system makes one fundamental commitment: every
suggestion targets the user's *current* editing location. That commitment is
formalized as ``ActiveAnchor`` — a snapshot of (document, surface, cursor,
selection, surrounding sentence/paragraph, section) extracted at observe()
time.

The anchor's ``confidence`` field tells downstream code how much to trust
this snapshot. Native cursor reads are near-perfect (0.9~1.0); UIA caret
reads from external apps are good (0.75~0.95); OCR-only fallbacks are bad
enough that we explicitly *should not* generate active writing suggestions
from them — passive low-confidence cards are reserved for a future passive
mode.

Anchor IDs are stable per (document_id, anchor_position_hash) so the
UserAdaptationMemory's anchor_cooldowns map can suppress a *specific
anchor + task_type* pair after the user rejected it — without poisoning the
global task_type EMA.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal, Optional


AnchorSource = Literal[
    "native_cursor",       # text directly typed in the editor's QTextEdit
    "native_selection",    # explicit user range selection
    "uia_caret",           # Windows UIAutomation caret position
    "uia_selection",       # Windows UIAutomation selection range
    "ocr_visible_text",    # screen capture only, no reliable caret
    "unknown",
]


# Confidence floors used elsewhere — kept here so callers don't have to
# remember magic numbers. See spec §3.2 for the bands.
MIN_CONFIDENCE_FOR_ACTIVE_SUGGESTION: float = 0.45
MIN_CONFIDENCE_FOR_REWRITE: float = 0.60
MIN_CONFIDENCE_FOR_FLOW_REVIEW: float = 0.65


@dataclass
class ActiveAnchor:
    """One observe-time snapshot of where the user is editing.

    All text fields are ephemeral — only ``anchor_id`` and ``confidence`` are
    safe to persist. ``section_id`` is a hash, not raw heading text.
    """

    document_id: str
    surface: Literal["native_editor", "external_app"]

    cursor_index: Optional[int] = None
    selection_start: Optional[int] = None
    selection_end: Optional[int] = None

    sentence_text: Optional[str] = None
    paragraph_text: Optional[str] = None
    section_heading: Optional[str] = None
    section_id: Optional[str] = None

    prev_sentence: Optional[str] = None
    next_sentence: Optional[str] = None
    prev_paragraph: Optional[str] = None
    next_paragraph: Optional[str] = None

    paragraph_id: Optional[str] = None
    anchor_id: str = ""
    confidence: float = 0.0
    source: AnchorSource = "unknown"

    extras: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.anchor_id:
            self.anchor_id = compute_anchor_id(self)
        if not self.section_id and self.section_heading:
            self.section_id = _hash16(self.section_heading)
        if not self.paragraph_id and self.paragraph_text:
            self.paragraph_id = _hash16(self.paragraph_text)

    def is_active_suggestion_capable(self) -> bool:
        """Return True iff confidence is high enough for an active writing
        suggestion. Below this floor the orchestrator returns NullPrediction
        regardless of any other signal."""
        return float(self.confidence) >= MIN_CONFIDENCE_FOR_ACTIVE_SUGGESTION


# --------------------------------------------------------------- helpers


def _hash16(text: str) -> str:
    """16-char hex digest of a short text. Used for section/paragraph IDs in
    logs — short enough to be readable, collision-resistant enough for the
    scale of a single workspace's per-document history."""
    h = hashlib.blake2b(text.encode("utf-8"), digest_size=8)
    return h.hexdigest()


def compute_anchor_id(anchor: "ActiveAnchor") -> str:
    """Stable ID for an anchor across observe ticks at the same location.

    Combines document_id with cursor position (or selection range) and a
    short hash of the surrounding paragraph. Two ticks at the same cursor
    in the same paragraph collapse to the same anchor_id — that's what makes
    "same anchor / same task" cooldown actually work.
    """
    parts = [str(anchor.document_id or "")]
    if anchor.selection_start is not None and anchor.selection_end is not None:
        parts.append(f"sel:{anchor.selection_start}:{anchor.selection_end}")
    elif anchor.cursor_index is not None:
        # Bucket cursor positions to 80-char windows so a few keystrokes
        # don't shift the anchor ID — otherwise the cooldown map would never
        # match the same anchor twice.
        parts.append(f"cur:{int(anchor.cursor_index) // 80}")
    else:
        parts.append("cur:none")
    if anchor.paragraph_text:
        parts.append(f"p:{_hash16(anchor.paragraph_text)}")
    return "anc_" + _hash16("|".join(parts))


def confidence_from_source(
    *,
    source: AnchorSource,
    has_cursor: bool,
    has_paragraph: bool,
    has_section: bool,
) -> float:
    """Project an AnchorSource onto a confidence value in the spec's bands.

    Concrete extraction code (native editor's keyPressEvent, UIA polling,
    OCR fallback) can call this so the floors stay in one place.
    """
    if source == "native_cursor":
        base = 0.95 if has_cursor else 0.85
        return _bump(base, has_paragraph, has_section)
    if source == "native_selection":
        return _bump(0.92, has_paragraph, has_section)
    if source == "uia_caret":
        base = 0.85 if has_cursor else 0.78
        return _bump(base, has_paragraph, has_section)
    if source == "uia_selection":
        return _bump(0.85, has_paragraph, has_section)
    if source == "ocr_visible_text":
        # OCR without a reliable caret: paragraph approximation OK but
        # cursor is a guess at best.
        base = 0.55 if has_paragraph else 0.25
        return _bump(base, False, has_section, ceiling=0.65)
    return 0.0


def _bump(base: float, has_paragraph: bool, has_section: bool, *, ceiling: float = 1.0) -> float:
    delta = 0.0
    if has_paragraph:
        delta += 0.03
    if has_section:
        delta += 0.02
    return min(ceiling, max(0.0, base + delta))
