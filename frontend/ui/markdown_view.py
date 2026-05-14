"""Markdown rendering helpers for Qt text widgets.

Qt's built-in `QTextDocument.setMarkdown` has known issues rendering GFM tables
correctly — alignment rows are sometimes treated as body cells and tables that
sit immediately after another block break entirely. To keep parity with the
preview the user sees in other markdown viewers, we convert markdown to HTML
in Python (using the standard `markdown` package) and call `setHtml`.

If the `markdown` package is unavailable the helper falls back to
`setMarkdown`, so the document page still works without the new dependency.
"""

from __future__ import annotations

import re

try:
	import markdown as _markdown  # type: ignore
except Exception:  # pragma: no cover - optional dependency
	_markdown = None


# Minimal CSS so QTextDocument renders tables with visible borders and
# reasonable spacing. QTextDocument supports a subset of CSS; the rules below
# are known to work for tables, code blocks, and quotes.
def _doc_style(font_size: str | None = "13px") -> str:
	"""Build the embedded stylesheet.

	``font_size`` is omitted from the ``body`` rule when ``None`` so the host
	widget's own font drives sizing — needed for the chat bubbles, whose font
	size is controlled by a Ctrl +/- zoom stylesheet on the widget.
	"""
	body_size = f" font-size: {font_size};" if font_size else ""
	return f"""
<style>
body {{ font-family: 'Segoe UI Variable', 'Segoe UI', 'Malgun Gothic', 'Noto Sans KR', sans-serif;{body_size} color: #1F2937; line-height: 1.55; }}
h1, h2, h3, h4 {{ color: #0F172A; font-weight: 800; margin: 18px 0 8px 0; }}
h1 {{ font-size: 22px; }}
h2 {{ font-size: 18px; }}
h3 {{ font-size: 15px; }}
p {{ margin: 6px 0; }}
code {{ background-color: #F1F5F9; color: #0F172A; padding: 1px 4px; border-radius: 4px; font-family: 'Consolas', 'Cascadia Mono', monospace; font-size: 12px; }}
pre {{ background-color: #0F172A; color: #E2E8F0; padding: 10px 12px; border-radius: 8px; font-family: 'Consolas', 'Cascadia Mono', monospace; font-size: 12px; }}
pre code {{ background: transparent; color: inherit; padding: 0; }}
blockquote {{ border-left: 3px solid #C7D2FE; color: #4B5563; margin: 8px 0; padding: 2px 10px; }}
ul, ol {{ margin: 6px 0 6px 20px; }}
li {{ margin: 2px 0; }}
table {{ border-collapse: collapse; margin: 10px 0; width: 100%; }}
th, td {{ border: 1px solid #CBD5E1; padding: 6px 9px; text-align: left; vertical-align: top; }}
th {{ background-color: #F1F5F9; color: #0F172A; font-weight: 700; }}
tr:nth-child(even) td {{ background-color: #F8FAFC; }}
a {{ color: #2563EB; text-decoration: none; }}
hr {{ border: none; border-top: 1px solid #E2E8F0; margin: 14px 0; }}
</style>
"""


def render_markdown_html(text: str, *, font_size: str | None = "13px") -> str:
	"""Convert markdown to a self-contained HTML fragment for QTextEdit.

	Returns an HTML string with embedded stylesheet. If the optional `markdown`
	package is missing, returns an empty string so callers can fall back to
	`setMarkdown`. Pass ``font_size=None`` to let the host widget's font drive
	the base text size.
	"""
	source = _normalize_for_qt(text or "")
	if _markdown is None:
		return ""
	html_body = _markdown.markdown(
		source,
		extensions=[
			"tables",
			"fenced_code",
			"sane_lists",
			"nl2br",
		],
		output_format="html5",
	)
	return f"{_doc_style(font_size)}\n{html_body}"


def apply_markdown(widget, text: str) -> None:
	"""Render markdown into a QTextEdit-compatible widget.

	Uses Python's markdown library when available (correct GFM tables), and
	falls back to Qt's `setMarkdown` otherwise.
	"""
	html = render_markdown_html(text)
	if html:
		widget.setHtml(html)
		return
	widget.setMarkdown(text or "")


_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_BULLET_ITEM = re.compile(r"^\s*[-*+]\s+\S")
_ORDERED_ITEM = re.compile(r"^\s*\d+[.)]\s+\S")
_ORDERED_FIRST = re.compile(r"^\s*1[.)]\s+\S")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_BLOCKQUOTE = re.compile(r"^\s*>")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def _is_list_item(line: str) -> bool:
	return bool(_BULLET_ITEM.match(line) or _ORDERED_ITEM.match(line))


def _needs_blank_before(line: str, prev: str) -> bool:
	"""True when *line* opens a block construct that Python-Markdown only
	recognises after a blank line, while *prev* is paragraph text it would
	otherwise be swallowed into — which is why such blocks show up as raw
	``-`` / ``|`` / ``#`` syntax in answers that omit the blank line.
	"""
	if not prev.strip():
		return False
	if _TABLE_ROW.match(line):
		return not _TABLE_ROW.match(prev)
	if _HEADING.match(line):
		return True
	if _BLOCKQUOTE.match(line):
		return not _BLOCKQUOTE.match(prev)
	# Lists: a bullet, or the first item of an ordered list. Ordered lists only
	# trigger on "1." so prose like "2026. was a turning point" is left alone;
	# later items don't need this since their prev is already a list item.
	if _BULLET_ITEM.match(line) or _ORDERED_FIRST.match(line):
		if _is_list_item(prev):
			return False
		# An indented prev line is a continuation/nested block — keep it joined.
		return prev[:1] not in (" ", "\t")
	return False


def _normalize_for_qt(text: str) -> str:
	"""Light pre-processing that improves parity with external previewers.

	LLM answers frequently omit the blank line Python-Markdown needs before a
	block construct (list, table, heading, blockquote); without it the block
	is absorbed into the preceding paragraph and renders as raw syntax. We
	insert the missing blank line, skipping the insides of fenced code blocks
	so their contents are never rewritten. BOM/zero-width chars that land in
	LLM output are also stripped.
	"""
	normalized = text.replace("﻿", "").replace("​", "")
	lines = normalized.split("\n")
	out: list[str] = []
	in_fence = False
	fence_char = ""
	for line in lines:
		fence = _FENCE.match(line)
		if fence:
			marker = fence.group(1)[0]
			if not in_fence:
				in_fence, fence_char = True, marker
			elif marker == fence_char:
				in_fence, fence_char = False, ""
			out.append(line)
			continue
		if in_fence:
			out.append(line)
			continue
		prev = out[-1] if out else ""
		if _needs_blank_before(line, prev):
			out.append("")
		out.append(line)
	# Ensure a trailing blank line after a table block so following text renders.
	if out and _TABLE_ROW.match(out[-1]):
		out.append("")
	return "\n".join(out)
