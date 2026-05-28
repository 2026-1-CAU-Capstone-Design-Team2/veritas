"""ContextBundle materialization — anchor-relative, *never* whole-document.

The rule-based pivot's most important invariant: the context the generator
sees must be derived from the user's current cursor / selection / paragraph.
No part of this module is allowed to search the whole document and pick an
unrelated section — if the anchor doesn't carry the data a task needs, the
RuleEvaluator should have hard-gated the candidate with
``context_insufficient``.

Output:

    ContextBundle(
        task_type, anchor_id, scope,
        text_parts={"current_paragraph": "...", "prev_paragraph": "..."},
        char_counts={"current_paragraph": 312, ...},
        source_snippets=[...],   # RAG only at generation time, evidence task
    )

Only ``text_parts`` and ``source_snippets`` carry raw text. The orchestrator
persists ``char_counts`` (a derived diagnostic) but NOT ``text_parts``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .anchors import ActiveAnchor
from .proposal_models import ContextScope, ProactiveTask


# Single-line sentence splitter — same heuristic as the bandit-era impl,
# kept inline so this module has no cross-package dependency.
_SENT_END = re.compile(r"(?<=[\.\?\!。？！])\s+")


def _split_sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENT_END.split(text) if p.strip()]
    return parts or [text]


@dataclass
class ContextBundle:
    """The materialized context for one ProactiveTask.

    ``text_parts`` and ``source_snippets`` are ephemeral (in-memory only).
    ``char_counts`` is what the persisted decision log records.
    """

    task_type: str
    anchor_id: str
    scope: str
    text_parts: dict[str, str] = field(default_factory=dict)
    char_counts: dict[str, int] = field(default_factory=dict)
    source_snippets: list[str] = field(default_factory=list)

    def meta_dict(self) -> dict[str, Any]:
        """Persistence-safe view (no raw text)."""
        return {
            "task_type": self.task_type,
            "anchor_id": self.anchor_id,
            "scope": self.scope,
            "char_counts": dict(self.char_counts),
            "source_snippets_count": len(self.source_snippets),
        }


def _add_part(bundle: ContextBundle, name: str, text: str | None) -> None:
    """Append a named text part if non-empty. Also records the char count
    so the orchestrator can log it without keeping the text."""
    if not text:
        return
    s = str(text)
    bundle.text_parts[name] = s
    bundle.char_counts[name] = len(s)


def materialize_context(
    *,
    task: ProactiveTask,
    anchor: ActiveAnchor,
) -> ContextBundle:
    """Build the ``ContextBundle`` for ``task`` from ``anchor``'s already-
    extracted slices. Never reaches outside the anchor's neighborhood.
    """
    bundle = ContextBundle(
        task_type=task.task_type,
        anchor_id=anchor.anchor_id,
        scope=task.context_scope,
    )

    scope: ContextScope = task.context_scope

    if scope == "cursor_previous_sentences":
        # Last 1-2 sentences before the cursor + the current fragment.
        # Prefer prev_sentence + sentence_text; fall back to splitting
        # paragraph_text from the end.
        if anchor.prev_sentence:
            _add_part(bundle, "prev_sentence", anchor.prev_sentence)
        elif anchor.paragraph_text:
            sents = _split_sentences(anchor.paragraph_text)
            tail = sents[-2:-1]
            if tail:
                _add_part(bundle, "prev_sentence", tail[0])
        _add_part(bundle, "current_fragment", anchor.sentence_text or "")
        return bundle

    if scope == "current_sentence":
        _add_part(bundle, "current_sentence", anchor.sentence_text)
        # Add paragraph context as auxiliary (the generator may or may not use it).
        _add_part(bundle, "current_paragraph", anchor.paragraph_text)
        return bundle

    if scope == "current_paragraph":
        _add_part(bundle, "current_paragraph", anchor.paragraph_text)
        return bundle

    if scope == "current_and_previous_paragraph":
        _add_part(bundle, "prev_paragraph", anchor.prev_paragraph)
        _add_part(bundle, "current_paragraph", anchor.paragraph_text)
        if anchor.section_heading:
            _add_part(bundle, "section_heading", anchor.section_heading)
        return bundle

    if scope == "current_prev_next_paragraphs":
        _add_part(bundle, "prev_paragraph", anchor.prev_paragraph)
        _add_part(bundle, "current_paragraph", anchor.paragraph_text)
        _add_part(bundle, "next_paragraph", anchor.next_paragraph)
        if anchor.section_heading:
            _add_part(bundle, "section_heading", anchor.section_heading)
        return bundle

    if scope == "claim_window":
        # Claim sentence ±2 sentences, cropped to the current paragraph so we
        # never escape the anchor's neighborhood.
        if anchor.paragraph_text:
            sents = _split_sentences(anchor.paragraph_text)
            focus = anchor.sentence_text
            window_text = anchor.paragraph_text
            if focus and sents:
                try:
                    idx = sents.index(focus)
                except ValueError:
                    idx = -1
                if idx >= 0:
                    lo = max(0, idx - 2)
                    hi = min(len(sents), idx + 3)
                    window_text = " ".join(sents[lo:hi]).strip()
            _add_part(bundle, "claim_window", window_text)
        else:
            _add_part(bundle, "claim_window", anchor.sentence_text)
        return bundle

    if scope == "anchor_diff_region":
        # The diff region itself is set by the candidate factory via metadata
        # when available; otherwise we fall back to current paragraph.
        diff_text = str(task.metadata.get("diff_text") or "") or anchor.paragraph_text
        _add_part(bundle, "diff_region", diff_text)
        _add_part(bundle, "surrounding_paragraph", anchor.paragraph_text)
        return bundle

    if scope == "section_local_excerpt":
        _add_part(bundle, "section_heading", anchor.section_heading)
        _add_part(bundle, "current_paragraph", anchor.paragraph_text)
        return bundle

    # Unknown scope — degrade gracefully to current paragraph rather than
    # opening up whole-document search.
    _add_part(bundle, "current_paragraph", anchor.paragraph_text)
    return bundle
