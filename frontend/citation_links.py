"""Citation-link presentation helpers for the document summary preview.

These are pure string functions — deliberately Qt-free so they unit-test
without importing PySide. They turn the ``doc_NNN`` citation markers already
present in ``final.md`` into clickable custom-scheme links at render time, and
parse those links back when the user clicks one. The ``final.md`` source is
never modified; linkification is purely a presentation concern.

Both marker spellings are linkified and **normalized to a bracketed
``[doc_NNN]`` label** so the user never has to care whether the model wrote
``[doc_000]``, ``doc_000``, ``doc-000`` or ``doc000`` — every citation looks and
clicks the same. A short id (``doc7`` / ``doc_7`` / ``doc-7``) is zero-padded to
the canonical three-digit form ``[doc_007]`` so the label never diverges from
``FINAL_PROMPT``'s ``[doc_NNN]`` rule. The bracket label is kept with a *nested*
Markdown link
(``[[doc_000]](href)``), which Python-Markdown renders as a link whose visible
text is ``[doc_000]``. (The obvious alternative — escaping the brackets as
``[\\[doc_000\\]]`` — is unusable here: ``markdown_view._extract_math`` treats
``\\[ … \\]`` as LaTeX *display math*, so it would eat the label, subscript the
``_0``, and split the link across blank lines.)

Link shape (single query param so HTML-attribute ``&`` escaping is never in
play)::

    [[doc_000]](veritas-citation:doc_000?claim=<percent-encoded claim>)

Markers inside fenced code blocks, inline code spans, existing Markdown link
targets, and URLs/paths are left untouched so code samples and hrefs keep their
literal text.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, unquote, urlsplit


CITATION_SCHEME = "veritas-citation"

# Cap the claim carried in each href; the matcher only needs a sentence's worth.
_MAX_CLAIM_CHARS = 500

# Markers we linkify: a bracketed ``[doc_000]`` or a *standalone* bare
# ``doc_000`` / ``doc-000`` / ``doc000``. The bare form is fenced by look-around
# so it never fires inside a longer word, a path segment, or a URL
# (``clean_md/doc_000.md`` and ``http://x/doc_000`` must stay literal).
_BRACKETED = r"\[doc[_-]?\d+\]"
_BARE = r"(?<![\w./:-])doc[_-]?\d+(?![\w-])"
_MARKER_RE = re.compile(rf"{_BRACKETED}|{_BARE}", re.IGNORECASE)
# Cheap pre-filter so marker-free lines/chunks skip the work entirely.
_HAS_DOC_RE = re.compile(r"doc[_-]?\d", re.IGNORECASE)

# Spans that must never be rewritten: inline code, existing Markdown
# links/images, autolinks/HTML tags, and bare URLs. A line is processed only in
# the gaps between these protected spans.
_PROTECTED_RE = re.compile(
    r"`[^`\n]*`"
    r"|!?\[[^\]]*\]\([^)]*\)"
    r"|<[^>\s]+>"
    r"|https?://\S+"
    r"|www\.\S+",
    re.IGNORECASE,
)

_DIGITS_RE = re.compile(r"\d+")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_LEADING_MD_RE = re.compile(r"^[\s>#*+\-]+")
# Strip both bracketed and bare markers when building the claim text.
_CLAIM_STRIP_RE = re.compile(r"\[?doc[_-]?\d+\]?", re.IGNORECASE)


def linkify_citations(text: str) -> str:
    """Rewrite ``doc_NNN`` markers into clickable, bracket-labelled links.

    Markers inside fenced code blocks are left untouched (a whole-block skip);
    every other line is processed in the gaps between inline-protected spans.
    """
    if not text:
        return text or ""
    out: list[str] = []
    in_fence = False
    fence_char = ""
    for line in text.split("\n"):
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0]
            if not in_fence:
                in_fence, fence_char = True, marker
            elif marker == fence_char:
                in_fence, fence_char = False, ""
            out.append(line)
            continue
        out.append(line if in_fence else _linkify_line(line))
    return "\n".join(out)


def _linkify_line(line: str) -> str:
    if not _HAS_DOC_RE.search(line):
        return line
    claim_enc = quote(extract_claim_from_line(line), safe="")
    out: list[str] = []
    pos = 0
    for protected in _PROTECTED_RE.finditer(line):
        out.append(_sub_markers(line[pos : protected.start()], claim_enc))
        out.append(protected.group(0))
        pos = protected.end()
    out.append(_sub_markers(line[pos:], claim_enc))
    return "".join(out)


def _sub_markers(chunk: str, claim_enc: str) -> str:
    if not chunk or not _HAS_DOC_RE.search(chunk):
        return chunk

    def _replace(match: re.Match[str]) -> str:
        digits = _DIGITS_RE.search(match.group(0)).group(0)  # type: ignore[union-attr]
        # Zero-pad to the canonical three-digit id so a short form (doc7,
        # doc_7, doc-7) renders and links as [doc_007] — keeping the label in
        # sync with FINAL_PROMPT's [doc_NNN] rule and the 3-digit clean_md files.
        doc = f"doc_{int(digits):03d}"
        href = f"{CITATION_SCHEME}:{doc}?claim={claim_enc}"
        # Nested brackets keep the visible label as [doc_007] after rendering.
        # (Escaped \[..\] would be mistaken for LaTeX display math upstream.)
        return f"[[{doc}]]({href})"

    return _MARKER_RE.sub(_replace, chunk)


def extract_claim_from_line(line: str) -> str:
    """Strip a Markdown line down to the prose used as the citation claim.

    Removes both bracketed and bare citation markers and light structural
    punctuation (heading/bullet/quote/table tokens), collapses whitespace, and
    caps length.
    """
    s = _CLAIM_STRIP_RE.sub(" ", line or "")
    s = _LEADING_MD_RE.sub(" ", s)
    s = s.replace("|", " ").replace("`", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:_MAX_CLAIM_CHARS]


def parse_citation_url(url_str: str) -> tuple[str, str] | None:
    """Parse a ``veritas-citation:`` URL back into ``(doc_id, claim)``.

    Returns ``None`` for any other scheme or an empty doc id. The caller must
    pass the fully *encoded* URL string (e.g. ``QUrl.toString(FullyEncoded)``)
    so the single percent-decode here round-trips the claim exactly.
    """
    if not url_str:
        return None
    parts = urlsplit(url_str)
    if parts.scheme != CITATION_SCHEME:
        return None
    doc_id = unquote(parts.path or "").strip()
    if not doc_id:
        return None
    claim_values = parse_qs(parts.query).get("claim") or [""]
    return doc_id, claim_values[0]


__all__ = [
    "CITATION_SCHEME",
    "linkify_citations",
    "extract_claim_from_line",
    "parse_citation_url",
]
