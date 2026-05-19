"""Parse ``runs/<ws>/summary/batch_*.md`` to map doc_ids -> batch mentions.

Batch summary files carry inline ``[doc_<id>]`` citation markers (enforced by
``BATCH_SUMMARY_PROMPT``) so each finding can be traced back to the source
documents it came from. The reliability pipeline uses this inverted index to
attach concrete batch evidence to the per-doc card payload:

* a finding under ``## New Findings``     -> kind = ``"new_finding"``
* a finding under ``## Reliability Notes`` -> kind = ``"reliability_note"``
* anything else                            -> ignored

We intentionally skip the ``## Repeated Findings`` section: the card is about
what *this* doc uniquely contributes / how trustworthy it is — repeated
findings are workspace-level signal that lives in the summary stripe, not the
per-doc card. We also skip the ``## Gaps`` section, which is *missing*
content (no citations belong there).

The parser is a string parser, not a Markdown AST one, because the batch
markdown is LLM-authored and only loosely conforms to a strict spec. We
tolerate:

* bullets prefixed with ``-``, ``*``, or ``•``;
* multiple citations on one bullet — ``[doc_001][doc_004][doc_009]``;
* indented sub-bullets that continue the parent's body (a doc cited on the
  parent line still gets credit for the sub-bullet snippet);
* legacy batch files without any markers — they produce an empty index, which
  callers must treat as "no batch context available" rather than an error.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DOC_MARKER_RE = re.compile(r"\[doc_(\d{1,4})\]")
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
_BULLET_PREFIX_RE = re.compile(r"^\s*[-*•]\s+")

_NEW_FINDINGS_KEYS = {"new findings", "new finding"}
_RELIABILITY_KEYS = {"reliability notes", "reliability note"}


@dataclass(frozen=True)
class BatchMention:
    """One bullet from one batch_*.md that cites a doc.

    ``batch_id`` is the three-digit string from the filename (``"001"``,
    ``"017"``), kept as-is so the UI can render it next to the snippet.
    ``snippet`` is the bullet text *with* the inline ``[doc_<id>]`` markers
    stripped, so it reads naturally in a card.
    """

    batch_id: str
    kind: str  # "new_finding" | "reliability_note"
    snippet: str


def _section_kind(heading: str) -> str | None:
    """Map an LLM-authored ``## Heading`` to a ``BatchMention.kind`` slug.

    Returns ``None`` for sections we deliberately ignore (Repeated Findings,
    Gaps, Batch Summary). Case-insensitive so a stray Title Case heading
    still matches.
    """
    normalized = heading.strip().lower()
    if normalized in _NEW_FINDINGS_KEYS:
        return "new_finding"
    if normalized in _RELIABILITY_KEYS:
        return "reliability_note"
    return None


def _normalize_doc_id(raw: str) -> str:
    """Pad a captured ``doc_<id>`` group to the canonical three-digit form.

    The prompt asks the LLM to use the exact 3-digit form (``[doc_007]``) but
    a small local model can still emit ``[doc_7]``. We re-pad so an off-by-one
    formatting issue does not silently drop the citation.
    """
    digits = raw.strip()
    if not digits.isdigit():
        return digits
    return digits.zfill(3)


def _iter_batch_files(summary_dir: Path) -> list[Path]:
    if not summary_dir.exists():
        return []
    files = sorted(summary_dir.glob("batch_*.md"))
    return [path for path in files if path.is_file()]


def _batch_id_from_path(path: Path) -> str:
    """Extract the numeric id from ``batch_<NNN>.md``.

    Falls back to the bare stem when the filename does not follow the
    expected shape, so an oddly-named legacy file still gets *some* id.
    """
    stem = path.stem  # e.g. "batch_001"
    if "_" in stem:
        tail = stem.split("_", 1)[1]
        if tail.isdigit():
            return tail
    return stem


def _emit_mentions_from_bullet(
    text: str,
    *,
    batch_id: str,
    kind: str,
    out: dict[str, list[BatchMention]],
) -> None:
    """Pull every ``[doc_<id>]`` marker off ``text`` and emit one mention each.

    ``text`` is the full bullet body (including continuation sub-lines that
    we already joined). We dedupe doc_ids inside the same bullet so a
    finding citing ``[doc_001][doc_001]`` does not double-emit. The stored
    snippet is the bullet body with the markers stripped — read-friendly.
    """
    matches = list(_DOC_MARKER_RE.finditer(text))
    if not matches:
        return
    snippet = _DOC_MARKER_RE.sub("", text).strip()
    # Tidy leading bullet markers / stray punctuation that the strip exposed.
    snippet = _BULLET_PREFIX_RE.sub("", snippet)
    snippet = snippet.strip(" ,;.")
    if not snippet:
        return

    seen_in_bullet: set[str] = set()
    for match in matches:
        doc_id = _normalize_doc_id(match.group(1))
        if not doc_id or doc_id in seen_in_bullet:
            continue
        seen_in_bullet.add(doc_id)
        out.setdefault(doc_id, []).append(
            BatchMention(batch_id=batch_id, kind=kind, snippet=snippet)
        )


def _parse_single_batch(
    path: Path,
    *,
    out: dict[str, list[BatchMention]],
) -> None:
    """Stream through one batch_*.md, emitting mentions into ``out``.

    Bullets are concatenated with their indented continuation lines before
    we look for markers, so a multi-line bullet still resolves to one snippet.
    A ``[doc_<id>]`` outside any tracked section (e.g. someone wrote it in
    the introductory paragraph) is ignored — we only credit findings under
    the two whitelisted sections.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("reliability: failed to read %s: %s", path, exc)
        return

    batch_id = _batch_id_from_path(path)
    current_kind: str | None = None
    bullet_buffer: list[str] = []

    def flush() -> None:
        if bullet_buffer and current_kind is not None:
            joined = " ".join(line.strip() for line in bullet_buffer if line.strip())
            if joined:
                _emit_mentions_from_bullet(
                    joined, batch_id=batch_id, kind=current_kind, out=out
                )
        bullet_buffer.clear()

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        # New top-level ``## Heading`` ends whatever bullet we were building.
        heading_match = _SECTION_RE.match(line)
        if heading_match:
            flush()
            current_kind = _section_kind(heading_match.group(1))
            continue

        if current_kind is None:
            continue

        stripped = line.lstrip()
        # Top-level bullet starts a new logical entry.
        if _BULLET_PREFIX_RE.match(line):
            flush()
            bullet_buffer.append(stripped)
            continue
        # Indented continuation (sub-bullet or wrapped prose) -> attach to the
        # current bullet so a doc cited on the parent line gets credit for
        # the whole entry.
        if line.startswith((" ", "\t")) and bullet_buffer:
            bullet_buffer.append(stripped)
            continue
        # Blank line ends the current bullet.
        if not stripped:
            flush()

    flush()


def build_batch_index(summary_dir: str | Path) -> dict[str, list[BatchMention]]:
    """Inverted index: ``doc_id`` -> ordered list of :class:`BatchMention`.

    The returned dict is empty when no ``batch_*.md`` exist *or* when every
    batch file is from a legacy run with no citation markers (the prompt
    rev). The reliability pipeline treats both cases the same: it still
    judges every doc, just without batch-grounded snippets in the card.
    """
    summary_dir = Path(summary_dir)
    out: dict[str, list[BatchMention]] = {}
    for path in _iter_batch_files(summary_dir):
        _parse_single_batch(path, out=out)
    return out


__all__ = ["BatchMention", "build_batch_index"]
