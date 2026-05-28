"""Pick the slice of document context each suggestion type needs.

The bandit decides *what* to suggest; this module decides *what to look at*
when generating. Kept separate so the generator can stay a thin SSE shell and
so context_scope can later become a bandit dimension without touching the
generator.

MVP: no learned context scope — each suggestion type uses its declared default
scope, and we extract a best-effort slice from the observation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

DEFAULT_CONTEXT_SCOPE: dict[str, str] = {
    "next_sentence": "previous_sentences",
    "paragraph_rewrite": "current_paragraph",
    "local_copyedit": "current_sentence_and_paragraph",
    "logic_flow_review": "previous_and_current_paragraph",
    "evidence_citation_prompt": "claim_window",
    "recovery_integration_note": "diff_region",
}


# Cheap Korean+latin sentence splitter — splits on . ? ! 。 ? ! followed by
# whitespace or end-of-string. Not perfect (abbreviations etc.) but the bandit
# only needs "previous 1-2 sentences" granularity, not parser-grade accuracy.
_SENT_END = re.compile(r"(?<=[\.\?\!。？！])\s+")


def _split_sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENT_END.split(text) if p.strip()]
    return parts or [text]


@dataclass
class SelectedContext:
    """The shape ``select_context`` returns. Kept as a dataclass so the
    generator can typecheck against it; serialized as a dict for the
    decision log (``selected_context_meta``)."""

    scope: str
    text: str
    prefix: str
    suffix: str
    focused_sentence: str
    current_paragraph: str
    previous_paragraph: str
    changed_text: str
    target_start: int
    target_end: int
    original_text: str
    needs_rag: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "text": self.text,
            "prefix": self.prefix,
            "suffix": self.suffix,
            "focused_sentence": self.focused_sentence,
            "current_paragraph": self.current_paragraph,
            "previous_paragraph": self.previous_paragraph,
            "changed_text": self.changed_text,
            "target_start": self.target_start,
            "target_end": self.target_end,
            "original_text": self.original_text,
            "needs_rag": self.needs_rag,
        }


def _find_paragraph_span(text: str, paragraph: str) -> tuple[int, int]:
    """Best-effort (start, end) span of ``paragraph`` inside ``text``.

    Falls back to ``(0, 0)`` when we can't locate it — the inline-diff renderer
    then defaults to "current paragraph" in the UI without anchoring to a hard
    range. This is the documented MVP fallback in §7.3 of the spec.
    """
    if not text or not paragraph:
        return 0, 0
    idx = text.find(paragraph)
    if idx < 0:
        return 0, 0
    return idx, idx + len(paragraph)


def select_context(
    *,
    observation: Any,
    suggestion_type: str,
    primitive: dict[str, Any],
) -> SelectedContext:
    """Return the context bundle for ``suggestion_type``.

    Observation is duck-typed to avoid a circular import — only the
    ``current_*``, ``previous_*``, ``prefix``, ``suffix``, ``text``,
    ``changed_text`` attributes are consulted.
    """
    _ = primitive  # kept in the signature for future scope-aware features

    text = str(getattr(observation, "text", "") or "")
    prefix = str(getattr(observation, "prefix", "") or "")
    suffix = str(getattr(observation, "suffix", "") or "")
    current_sentence = str(getattr(observation, "current_sentence", "") or "")
    current_paragraph = str(getattr(observation, "current_paragraph", "") or "")
    previous_paragraph = str(getattr(observation, "previous_paragraph", "") or "")
    changed_text = str(getattr(observation, "changed_text", "") or "")

    scope = DEFAULT_CONTEXT_SCOPE.get(suggestion_type, "current_paragraph")

    target_start = 0
    target_end = 0
    original_text = ""
    focused = current_sentence
    body = current_paragraph
    needs_rag = False

    if scope == "previous_sentences":
        sents = _split_sentences(prefix or current_paragraph)
        tail = sents[-2:] if sents else []
        body = " ".join(tail).strip()
        focused = current_sentence
    elif scope == "current_paragraph":
        body = current_paragraph
        target_start, target_end = _find_paragraph_span(text, current_paragraph)
        original_text = current_paragraph
    elif scope == "current_sentence_and_paragraph":
        body = current_paragraph
        focused = current_sentence
        if current_sentence and current_sentence in current_paragraph:
            offset = current_paragraph.find(current_sentence)
            para_start, _ = _find_paragraph_span(text, current_paragraph)
            target_start = para_start + offset
            target_end = target_start + len(current_sentence)
            original_text = current_sentence
        else:
            target_start, target_end = _find_paragraph_span(text, current_paragraph)
            original_text = current_paragraph
    elif scope == "previous_and_current_paragraph":
        body = "\n\n".join(p for p in (previous_paragraph, current_paragraph) if p)
        target_start, target_end = _find_paragraph_span(text, current_paragraph)
        original_text = current_paragraph
    elif scope == "claim_window":
        sents = _split_sentences(current_paragraph)
        # claim ±2 sentences around the focused sentence; fall back to the
        # whole paragraph when we can't locate the focused one.
        if current_sentence and sents:
            try:
                idx = sents.index(current_sentence)
            except ValueError:
                idx = -1
            if idx >= 0:
                lo = max(0, idx - 2)
                hi = min(len(sents), idx + 3)
                body = " ".join(sents[lo:hi]).strip()
            else:
                body = current_paragraph
        else:
            body = current_paragraph
        needs_rag = True
    elif scope == "diff_region":
        body = changed_text or current_paragraph
        if changed_text and changed_text in text:
            target_start = text.find(changed_text)
            target_end = target_start + len(changed_text)
            original_text = changed_text
        else:
            target_start, target_end = _find_paragraph_span(text, current_paragraph)
            original_text = current_paragraph
    else:
        body = current_paragraph or text

    return SelectedContext(
        scope=scope,
        text=body,
        prefix=prefix,
        suffix=suffix,
        focused_sentence=focused,
        current_paragraph=current_paragraph,
        previous_paragraph=previous_paragraph,
        changed_text=changed_text,
        target_start=target_start,
        target_end=target_end,
        original_text=original_text,
        needs_rag=needs_rag,
    )
