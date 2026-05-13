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
_DOC_STYLE = """
<style>
body { font-family: 'Segoe UI Variable', 'Segoe UI', 'Malgun Gothic', 'Noto Sans KR', sans-serif; font-size: 13px; color: #1F2937; line-height: 1.55; }
h1, h2, h3, h4 { color: #0F172A; font-weight: 800; margin: 18px 0 8px 0; }
h1 { font-size: 22px; }
h2 { font-size: 18px; }
h3 { font-size: 15px; }
p { margin: 6px 0; }
code { background-color: #F1F5F9; color: #0F172A; padding: 1px 4px; border-radius: 4px; font-family: 'Consolas', 'Cascadia Mono', monospace; font-size: 12px; }
pre { background-color: #0F172A; color: #E2E8F0; padding: 10px 12px; border-radius: 8px; font-family: 'Consolas', 'Cascadia Mono', monospace; font-size: 12px; }
pre code { background: transparent; color: inherit; padding: 0; }
blockquote { border-left: 3px solid #C7D2FE; color: #4B5563; margin: 8px 0; padding: 2px 10px; }
ul, ol { margin: 6px 0 6px 20px; }
li { margin: 2px 0; }
table { border-collapse: collapse; margin: 10px 0; width: 100%; }
th, td { border: 1px solid #CBD5E1; padding: 6px 9px; text-align: left; vertical-align: top; }
th { background-color: #F1F5F9; color: #0F172A; font-weight: 700; }
tr:nth-child(even) td { background-color: #F8FAFC; }
a { color: #2563EB; text-decoration: none; }
hr { border: none; border-top: 1px solid #E2E8F0; margin: 14px 0; }
</style>
"""


def render_markdown_html(text: str) -> str:
	"""Convert markdown to a self-contained HTML fragment for QTextEdit.

	Returns an HTML string with embedded stylesheet. If the optional `markdown`
	package is missing, returns an empty string so callers can fall back to
	`setMarkdown`.
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
	return f"{_DOC_STYLE}\n{html_body}"


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


def _normalize_for_qt(text: str) -> str:
	"""Light pre-processing that improves parity with external previewers.

	- Ensure a blank line before/after pipe tables so the parser detects them.
	- Strip BOM/zero-width chars that occasionally land in LLM outputs.
	"""
	normalized = text.replace("﻿", "").replace("​", "")
	lines = normalized.split("\n")
	out: list[str] = []
	for index, line in enumerate(lines):
		looks_like_table_row = bool(re.match(r"^\s*\|.*\|\s*$", line))
		prev = out[-1] if out else ""
		if looks_like_table_row and prev and not re.match(r"^\s*\|", prev) and prev.strip():
			out.append("")
		out.append(line)
	# Ensure trailing blank line after a table block so following text renders.
	if out and re.match(r"^\s*\|", out[-1]):
		out.append("")
	return "\n".join(out)
