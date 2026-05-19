"""Tolerant parser for the cleanup tool's plain-text section response.

JSON output proved fragile across languages — a single un-escaped quote
inside a Korean key-point breaks the whole document. We switched to a
plain-text four-section response (``BOILERPLATE_PARAGRAPHS`` / ``SUMMARY``
/ ``KEYWORDS`` / ``KEY_POINTS``) so the LLM never has to escape body text.
This module parses that response into a canonical dict the rest of the tool
consumes.

The parser is intentionally tolerant:

* Any of ``===``, ``---`` or simple line breaks before the header can act as
  a section boundary.
* Header lines are matched case-insensitively and accept a few variants
  (``KEY POINTS`` / ``KEY-POINTS`` / ``KEY_POINTS``).
* Bullets accept ``-`` / ``*`` / ``•`` / ``·``, with or without surrounding
  whitespace; lines without a bullet marker are still kept as items.
* ``BOILERPLATE_PARAGRAPHS`` accepts comma-separated, bracketed, or
  whitespace-separated digit lists. Any integer in the body is picked up.
* ``SUMMARY`` is collected as free-form prose: bullets are stripped, lines
  joined with single newlines, and runs of blank lines collapse to a
  paragraph break.

If the LLM only produced some of the four sections, the missing sections
come back as empty (list or string) — the tool then treats those as "no
signal" rather than failing the whole document.
"""

from __future__ import annotations

import re

# Section boundary — two or more ``=`` (or ``-``) on a line, optional whitespace.
_SECTION_BOUNDARY_RE = re.compile(r"^[=\-]{2,}\s*$", re.MULTILINE)
# Bullet marker at line start (dash, asterisk, bullet point, middle dot).
_BULLET_RE = re.compile(r"^\s*[-*•·]\s+(.*)$")
# Any integer token in a free-form line.
_INT_TOKEN_RE = re.compile(r"\d+")

# Header keyword fragments mapped to canonical section names. Case-folded
# before matching so ``Key Points`` / ``KEY POINTS`` / ``KEY_POINTS`` all
# resolve to the same canonical key. ``key point`` is checked BEFORE
# ``keyword`` because ``keypoint`` would otherwise match the ``keyword``
# fragment by substring rule (``keyword``? no — but the iteration order is
# safer when key_points hints are listed first).
_HEADER_HINTS: dict[str, str] = {
    "key point": "key_points",
    "key_point": "key_points",
    "key-point": "key_points",
    "keypoint": "key_points",
    "boilerplate": "boilerplate_paragraphs",
    "keyword": "keywords",
    "summary": "summary",
}


def _normalize_header(line: str) -> str | None:
    """Return the canonical section name a header line refers to, or None."""
    text = line.strip().lower()
    if not text:
        return None
    for fragment, canonical in _HEADER_HINTS.items():
        if fragment in text:
            return canonical
    return None


def _strip_bullet(line: str) -> str:
    """Return the bullet body, or the trimmed line itself when no bullet."""
    match = _BULLET_RE.match(line)
    if match:
        return match.group(1).strip()
    return line.strip()


def _parse_bullet_list(body: str) -> list[str]:
    """Pull bullet-list items out of a section body.

    Lines without a bullet marker are still kept as items — the LLM
    sometimes drops the marker on the first item or wraps a long key-point
    across two lines without re-indenting; treating bare lines as items
    keeps the result usable. Adjacent blank lines act as item separators.
    """
    items: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        cleaned = _strip_bullet(stripped)
        if cleaned:
            items.append(cleaned)
    # Deduplicate by case-folded text while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _parse_int_list(body: str) -> list[int]:
    """Pull every integer token out of a free-form line / list.

    Tolerates ``"3, 7, 12"``, ``"[3, 7, 12]"``, ``"- 3\n- 7\n- 12"``, and
    even ``"P3, P7, P12"`` — any non-digit run is just a separator.
    """
    return [int(token) for token in _INT_TOKEN_RE.findall(body)]


def _parse_prose(body: str) -> str:
    """Collect free-form prose from a section body.

    Used by the ``SUMMARY`` section. Strips leading bullet markers (the LLM
    sometimes wraps the summary as a single bullet by habit), keeps each
    non-blank line, and collapses runs of blank lines to a single paragraph
    break so the rendered ``doc_<id>.md`` keeps the LLM's paragraphing.
    """
    paragraphs: list[list[str]] = []
    current: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                paragraphs.append(current)
                current = []
            continue
        cleaned = _strip_bullet(stripped)
        if cleaned:
            current.append(cleaned)
    if current:
        paragraphs.append(current)
    return "\n\n".join(" ".join(lines) for lines in paragraphs).strip()


def parse_cleanup_response(text: str) -> dict[str, list | str]:
    """Parse the cleanup tool's plain-text response into the canonical dict.

    Returns ``{"boilerplate_paragraphs": [int], "summary": str,
    "keywords": [str], "key_points": [str]}`` with every missing section
    coming back as an empty list/string — the tool's caller treats those as
    'no signal' rather than failing the document.
    """
    canonical: dict[str, list | str] = {
        "boilerplate_paragraphs": [],
        "summary": "",
        "keywords": [],
        "key_points": [],
    }
    if not text or not text.strip():
        return canonical

    # Split on ``===``/``---`` boundary lines first. Sections then start with
    # a header line + a body. When the boundary is missing the LLM often
    # still emits the headers in order — so we *also* sweep through the
    # whole text line-by-line and reattach orphan lines to the most recently
    # seen header.
    sections = _SECTION_BOUNDARY_RE.split(text)

    for section in sections:
        section = section.strip()
        if not section:
            continue
        lines = section.splitlines()
        # Find the first non-empty line; treat it as the header.
        header_index: int | None = None
        for index, line in enumerate(lines):
            if line.strip():
                header_index = index
                break
        if header_index is None:
            continue
        canonical_key = _normalize_header(lines[header_index])
        if canonical_key is None:
            # The whole section is body for a header that ran into another
            # block — try the line-by-line sweep below instead of dropping it.
            continue
        body = "\n".join(lines[header_index + 1 :])
        if canonical_key == "boilerplate_paragraphs":
            canonical[canonical_key] = _parse_int_list(body)
        elif canonical_key == "summary":
            canonical[canonical_key] = _parse_prose(body)
        else:
            canonical[canonical_key] = _parse_bullet_list(body)

    # Line-by-line fallback for responses without boundary markers.
    if not _has_any_signal(canonical):
        current_key: str | None = None
        buffer: list[str] = []

        def _flush() -> None:
            nonlocal buffer
            if current_key is None or not buffer:
                buffer = []
                return
            body = "\n".join(buffer)
            if current_key == "boilerplate_paragraphs":
                canonical[current_key] = _parse_int_list(body)
            elif current_key == "summary":
                canonical[current_key] = _parse_prose(body)
            else:
                canonical[current_key] = _parse_bullet_list(body)
            buffer = []

        for raw_line in text.splitlines():
            header = _normalize_header(raw_line)
            if header is not None:
                _flush()
                current_key = header
                continue
            buffer.append(raw_line)
        _flush()

    return canonical


def _has_any_signal(canonical: dict[str, list | str]) -> bool:
    """True iff any parsed section ended up non-empty."""
    for value in canonical.values():
        if isinstance(value, str):
            if value.strip():
                return True
        elif value:
            return True
    return False


__all__ = ["parse_cleanup_response"]
