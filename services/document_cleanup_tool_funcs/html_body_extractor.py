"""Structural main-content extraction from archived raw HTML.

Used by the external-API (batch) cleanup path to build ``clean_md`` from the
archived ``corpus/raw_html/<id>.html``. The goal is to drop page chrome
(navigation, footer, sidebars, related-post / tag / category clusters, comment
forms) so batch summary, RAG, verification, and the citation popup all read a
cleaner body.

**Hard rule — structure only.** Every decision is made from HTML tag names,
ARIA landmark roles, and *structural* statistics (text length, anchor-text
density, control-element count, table/heading kind). There is **no keyword
list, no class/id substring matching, and no site-specific selector**: only
HTML/ARIA semantics and structure, so the extractor stays language- and
domain-agnostic (the project's no-hard-coded-keyword guardrail).

Pipeline (see :func:`extract_main_text_with_stats`):

1. Drop chrome tags/roles outright (``nav`` / ``footer`` / ``script`` / …).
2. Pick the best content container (``article`` / ``main`` / ``[role=main]`` /
   ``body``) by a text-vs-link-density score — ranking, not "first article
   wins", so a teaser ``<article>`` or a link-heavy related box never wins.
3. Collect terminal blocks (heading / paragraph / list / table / pre) in order,
   each carrying structural stats.
4. Select the main-body block *run* as the maximum-weight contiguous window
   (Kadane), where chrome blocks (link-/control-heavy, tiny) carry negative
   weight. This drops a page's leading nav AND its trailing
   tags/related/category/comment cluster even when they survived as plain text,
   while keeping short body paragraphs in the middle.
5. Report the trimmed body plus a structural quality verdict so the caller can
   accept good low-retention bodies (a long article or a data table far smaller
   than the noisy raw) and reject promo/nav extractions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

try:  # BeautifulSoup is a hard project dependency (beautifulsoup4).
    from bs4 import BeautifulSoup
    from bs4.element import Tag
except Exception:  # pragma: no cover - extractor degrades to empty → raw fallback
    BeautifulSoup = None  # type: ignore[assignment]
    Tag = None  # type: ignore[assignment]


# Non-content tags removed outright (HTML semantics, not keywords).
_DROP_TAGS = {
    "script", "style", "noscript", "template", "svg", "canvas",
    "nav", "footer", "aside", "form", "button", "iframe", "dialog",
}
# ARIA landmark roles that denote page chrome rather than the document body.
_DROP_ROLES = {
    "navigation", "banner", "contentinfo", "complementary",
    "search", "form", "menu", "menubar", "dialog", "tablist",
}
_CONTROL_TAGS = ["input", "select", "textarea", "label", "button", "form"]

_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# --- Structural thresholds (all language/domain-agnostic) -------------------
_PROSE_MAX_LINK_DENSITY = 0.30   # a paragraph below this counts as prose body
_TABLE_MIN_CHARS = 40            # a table with at least this much text is content
_HEADING_MIN_CHARS = 20          # shorter headings read as section nav, not titles
# Acceptance gate for the selected body window.
_MIN_BODY_CHARS = 400        # prose-only body floor
_MIN_PROSE_CHARS = 400       # substantial-paragraph total to count as a prose body
_MIN_TABLE_BODY_CHARS = 200  # table bodies are dense data → a lower floor
_MAX_BODY_LINK_DENSITY = 0.50


@dataclass
class _Block:
    kind: str  # 'heading' | 'paragraph' | 'list' | 'table' | 'pre'
    markdown: str
    text_len: int
    link_len: int
    control_count: int

    @property
    def link_density(self) -> float:
        return self.link_len / self.text_len if self.text_len else 1.0

    @property
    def weight(self) -> int:
        """Content weight for window selection (chrome → negative)."""
        ld = self.link_density
        if self.control_count > 0:
            return -max(self.text_len, 40)
        if self.kind == "table":
            return self.text_len if self.text_len >= _TABLE_MIN_CHARS else -self.text_len
        if self.kind == "heading":
            if self.text_len < _HEADING_MIN_CHARS or ld > 0.5:
                return -self.text_len
            return min(self.text_len, 30)
        if self.kind == "list":
            if ld > _PROSE_MAX_LINK_DENSITY or self.text_len < 40:
                return -self.text_len  # nav / related / tag / category list
            return int(self.text_len * 0.5)
        # paragraph / pre
        if ld > 0.5 or self.text_len < 25:
            return -self.text_len
        return self.text_len


@dataclass
class ExtractionResult:
    text: str
    accepted: bool
    reason: str  # 'accepted' | 'too_short' | 'low_quality' | 'empty' | 'extractor_error'
    extracted_len: int
    prose_len: int
    table_count: int
    link_density: float
    block_count: int


def extract_main_text_with_stats(html: str) -> ExtractionResult:
    """Extract the main body of *html* plus a structural quality verdict."""
    if BeautifulSoup is None or not html or not html.strip():
        return ExtractionResult("", False, "empty", 0, 0, 0, 1.0, 0)
    try:
        soup = BeautifulSoup(html, "html.parser")
        _drop_chrome(soup)
        body = _pick_body(soup)
        window = _select_window(_collect_blocks(body)) if body is not None else []
    except Exception:  # noqa: BLE001 - any parse failure → raw fallback upstream
        return ExtractionResult("", False, "extractor_error", 0, 0, 0, 1.0, 0)

    if not window:
        return ExtractionResult("", False, "empty", 0, 0, 0, 1.0, 0)

    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(b.markdown for b in window)).strip()
    total_text = sum(b.text_len for b in window)
    total_link = sum(b.link_len for b in window)
    link_density = total_link / total_text if total_text else 1.0
    prose_len = sum(
        b.text_len
        for b in window
        if b.kind == "paragraph" and b.link_density < _PROSE_MAX_LINK_DENSITY
    )
    table_count = sum(1 for b in window if b.kind == "table")

    reason = _quality_reason(len(text), prose_len, table_count, link_density)
    return ExtractionResult(
        text=text,
        accepted=(reason == "accepted"),
        reason=reason,
        extracted_len=len(text),
        prose_len=prose_len,
        table_count=table_count,
        link_density=round(link_density, 3),
        block_count=len(window),
    )


def _quality_reason(
    extracted_len: int, prose_len: int, table_count: int, link_density: float
) -> str:
    if extracted_len == 0:
        return "empty"
    if link_density > _MAX_BODY_LINK_DENSITY:
        return "low_quality"
    has_prose = prose_len >= _MIN_PROSE_CHARS
    has_table = table_count >= 1
    if not has_prose and not has_table:
        # A prose-only body that never reached the prose floor.
        return "too_short" if extracted_len < _MIN_BODY_CHARS else "low_quality"
    # Table-bearing bodies are dense data and may be shorter than a prose body,
    # but still need a real-table floor so a stray 1-row nav table is rejected.
    floor = _MIN_TABLE_BODY_CHARS if has_table else _MIN_BODY_CHARS
    if extracted_len < floor:
        return "too_short"
    return "accepted"


def extract_main_text(html: str) -> str:
    """Backward-compatible wrapper: the trimmed body text (may be empty)."""
    return extract_main_text_with_stats(html).text


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------
def _drop_chrome(soup) -> None:
    # Two passes: collect chrome tags in a read-only sweep, then decompose.
    # Decomposing during the sweep destroys nested descendants still ahead in
    # find_all()'s list, and touching a destroyed tag's attrs raises.
    drop = [
        tag
        for tag in soup.find_all(True)
        if tag.name in _DROP_TAGS
        or str((tag.attrs or {}).get("role") or "").strip().lower() in _DROP_ROLES
    ]
    for tag in drop:
        if not getattr(tag, "decomposed", False):
            tag.decompose()


def _pick_body(soup):
    """Pick the content container with the most low-link-density text."""
    candidates: list = []
    candidates.extend(soup.find_all("article"))
    candidates.extend(soup.find_all("main"))
    candidates.extend(soup.find_all(attrs={"role": "main"}))
    if soup.body is not None:
        candidates.append(soup.body)
    candidates = [
        node
        for node in candidates
        if isinstance(node, Tag) and not getattr(node, "decomposed", False)
    ]
    if not candidates:
        return soup
    return max(candidates, key=content_score)


def content_score(node) -> float:
    """Text length discounted by anchor-text density (language/domain-agnostic)."""
    text_len = len(node.get_text(" ", strip=True))
    if text_len == 0:
        return 0.0
    link_len = sum(len(a.get_text(" ", strip=True)) for a in node.find_all("a"))
    link_density = min(1.0, link_len / text_len)
    return text_len * (1.0 - link_density)


def _select_window(blocks: list[_Block]) -> list[_Block]:
    """Return the maximum-weight contiguous block run, edges stripped of headings.

    Kadane over block weights isolates the main body: leading nav and trailing
    tags/related/comment clusters carry negative weight and fall outside the
    window, while short body paragraphs (positive) stay in. A window that begins
    or ends on a bare heading has that heading dropped (a body never ends on a
    section title).
    """
    if not blocks:
        return []
    best_sum = best_lo = best_hi = None
    cur_sum = 0
    cur_lo = 0
    for i, block in enumerate(blocks):
        w = block.weight
        if cur_sum <= 0:
            cur_sum, cur_lo = w, i
        else:
            cur_sum += w
        if best_sum is None or cur_sum > best_sum:
            best_sum, best_lo, best_hi = cur_sum, cur_lo, i
    if best_sum is None or best_sum <= 0:
        return []
    window = blocks[best_lo : best_hi + 1]
    while window and window[-1].kind == "heading":
        window.pop()
    while window and window[0].kind == "heading" and window[0].weight <= 0:
        window.pop(0)
    return window


def _collect_blocks(root) -> list[_Block]:
    blocks: list[_Block] = []
    _collect(root, blocks)
    return blocks


def _collect(element, out: list[_Block]) -> None:
    for child in element.find_all(recursive=False):
        name = (child.name or "").lower()
        if name in _HEADINGS:
            text = child.get_text(" ", strip=True)
            if text:
                out.append(_make_block("heading", "#" * int(name[1]) + " " + text, child, text))
        elif name == "li":
            text = child.get_text(" ", strip=True)
            if text:
                out.append(_make_block("list", "- " + text, child, text))
        elif name == "pre":
            code = child.get_text("\n", strip=False).strip("\n")
            if code.strip():
                out.append(_make_block("pre", "```\n" + code + "\n```", child, code))
        elif name == "table":
            md = _table_markdown(child)
            if md:
                out.append(_make_block("table", md, child, child.get_text(" ", strip=True)))
        elif name in ("p", "blockquote"):
            text = child.get_text(" ", strip=True)
            if text:
                out.append(_make_block("paragraph", text, child, text))
        else:
            _collect(child, out)


def _make_block(kind: str, markdown: str, element, raw_text: str) -> _Block:
    link_len = sum(len(a.get_text(" ", strip=True)) for a in element.find_all("a"))
    control_count = len(element.find_all(_CONTROL_TAGS))
    return _Block(
        kind=kind,
        markdown=markdown,
        text_len=len(raw_text),
        link_len=link_len,
        control_count=control_count,
    )


def _table_markdown(table) -> str:
    rows: list[str] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        values = [c.get_text(" ", strip=True).replace("|", "\\|") for c in cells]
        rows.append("| " + " | ".join(values) + " |")
    if not rows:
        return ""
    col_count = rows[0].count("|") - 1
    rows.insert(1, "| " + " | ".join(["---"] * max(1, col_count)) + " |")
    return "\n".join(rows)


__all__ = ["extract_main_text", "extract_main_text_with_stats", "ExtractionResult"]
