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

import html
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
pre {{ background-color: #F1F5F9; color: #0F172A; padding: 10px 12px; border-radius: 8px; border: 1px solid #E2E8F0; font-family: 'Consolas', 'Cascadia Mono', monospace; font-size: 12px; }}
pre code {{ background: transparent; color: #0F172A; padding: 0; }}
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


# --- LaTeX math ($$…$$, \[…\], \(…\), $…$) → lightweight inline HTML --------
#
# Qt's rich text engine can't render LaTeX, but it does render <sup>/<sub> and
# Unicode. Each formula is converted — pure Python, no images, no extra
# dependency — to a small HTML fragment: symbols become Unicode, super/sub-
# scripts become <sup>/<sub>. Complex constructs (nested fractions, matrices)
# degrade to a readable plain form rather than a typeset one.

_MATH_DISPLAY = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_MATH_DISPLAY_BRACKET = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
_MATH_INLINE_PAREN = re.compile(r"\\\((.+?)\\\)", re.DOTALL)
_MATH_INLINE = re.compile(r"\$([^\n$]+?)\$")
_MATH_CURRENCY = re.compile(r"^[\s\d.,]+$")  # "$5", "$1,000.00" — currency, not math

_MATH_SYMBOLS = {
	"alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
	"varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
	"iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
	"pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
	"varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
	"Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
	"Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
	"times": "×", "div": "÷", "cdot": "·", "pm": "±", "mp": "∓", "ast": "∗",
	"star": "⋆", "circ": "∘", "bullet": "•", "oplus": "⊕", "otimes": "⊗",
	"leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
	"equiv": "≡", "approx": "≈", "cong": "≅", "sim": "∼", "simeq": "≃",
	"propto": "∝", "ll": "≪", "gg": "≫",
	"to": "→", "rightarrow": "→", "Rightarrow": "⇒", "leftarrow": "←",
	"Leftarrow": "⇐", "leftrightarrow": "↔", "Leftrightarrow": "⇔",
	"mapsto": "↦", "implies": "⟹", "iff": "⟺", "uparrow": "↑", "downarrow": "↓",
	"sum": "∑", "prod": "∏", "int": "∫", "iint": "∬", "oint": "∮",
	"partial": "∂", "nabla": "∇", "infty": "∞", "prime": "′",
	"forall": "∀", "exists": "∃", "nexists": "∄", "neg": "¬", "lnot": "¬",
	"land": "∧", "lor": "∨", "wedge": "∧", "vee": "∨",
	"in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆",
	"supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩", "setminus": "∖",
	"emptyset": "∅", "varnothing": "∅",
	"cdots": "⋯", "ldots": "…", "dots": "…", "vdots": "⋮",
	"angle": "∠", "perp": "⊥", "parallel": "∥", "degree": "°",
	"sqrt": "√", "ell": "ℓ", "hbar": "ℏ", "aleph": "ℵ",
}

_MATH_SPACING = re.compile(
	r"\\(?:left|right|[bB]igg?[lr]?|displaystyle|limits|quad|qquad)(?![A-Za-z])"
	r"|\\[,;:!> ]"
)
_MATH_FRAC = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_MATH_SQRT = re.compile(r"\\sqrt\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}")
_MATH_KEEP_ARG = re.compile(
	r"\\(?:text|operatorname|mathrm|mathbf|mathit|mathsf|mathtt|mathcal|mathbb"
	r"|mathfrak|boldsymbol|bm|hat|bar|vec|dot|ddot|tilde|widehat|widetilde"
	r"|overline|underline|overrightarrow)\s*\{([^{}]*)\}"
)
_MATH_SUP_BRACED = re.compile(r"\^\{([^{}]*)\}")
_MATH_SUB_BRACED = re.compile(r"_\{([^{}]*)\}")
_MATH_CMD = re.compile(r"\\([A-Za-z]+)")
_MATH_SUP_ONE = re.compile(r"\^([A-Za-z0-9])")
_MATH_SUB_ONE = re.compile(r"_([A-Za-z0-9])")


def _latex_to_html(latex: str) -> str:
	"""Convert a LaTeX snippet to a small HTML fragment.

	Uses only Unicode symbols and <sup>/<sub> tags — both rendered natively by
	Qt's rich text engine — so there are no images and no extra dependency.
	"""
	s = latex.strip()
	if not s:
		return ""
	# Spacing / sizing commands and hard line breaks → drop or collapse.
	s = _MATH_SPACING.sub(" ", s)
	s = s.replace("\\\\", " ")
	# HTML-escape now, before any <sup>/<sub> tags are introduced.
	s = html.escape(s, quote=False)
	# Brace-consuming constructs, resolved innermost-first by repeating the
	# pass — the [^{}]* groups only ever match the deepest level.
	for _ in range(8):
		before = s
		s = _MATH_FRAC.sub(r"(\1)⁄(\2)", s)
		s = _MATH_SQRT.sub(r"√(\1)", s)
		s = _MATH_KEEP_ARG.sub(r"\1", s)
		s = _MATH_SUP_BRACED.sub(r"<sup>\1</sup>", s)
		s = _MATH_SUB_BRACED.sub(r"<sub>\1</sub>", s)
		if s == before:
			break
	# Remaining \commands → Unicode; unknown ones keep their bare name, which
	# is the right thing for \sin, \log, \lim, …
	s = _MATH_CMD.sub(lambda m: _MATH_SYMBOLS.get(m.group(1), m.group(1)), s)
	# Single-token super/subscripts (x^2, a_i) — after the command pass so a
	# symbol exponent like ^\alpha has already become ^α.
	s = _MATH_SUP_ONE.sub(r"<sup>\1</sup>", s)
	s = _MATH_SUB_ONE.sub(r"<sub>\1</sub>", s)
	# Drop any leftover grouping braces.
	return s.replace("{", "").replace("}", "").strip()


def _extract_math(text: str) -> tuple[str, dict[str, str]]:
	"""Lift LaTeX math out of *text*, replacing each with an inert placeholder.

	Math is extracted before markdown runs so markdown cannot mangle the
	``_`` / ``^`` / ``\\`` inside formulas; the converted HTML is spliced back
	into the document afterwards. Placeholders use private-use code points,
	which neither ``_normalize_for_qt`` nor markdown touch.
	"""
	replacements: dict[str, str] = {}
	counter = [0]

	def _sub(content: str, display: bool, original: str) -> str:
		converted = _latex_to_html(content)
		if not converted:
			return original  # nothing usable — keep the raw text
		token = f"MATH{counter[0]}"
		counter[0] += 1
		replacements[token] = converted
		# Blank lines around a display token so markdown treats it as a block.
		return f"\n\n{token}\n\n" if display else token

	def _inline_dollar(match: re.Match[str]) -> str:
		body = match.group(1)
		# Skip currency: a pure number ("$5"), or — when the span carries no
		# LaTeX syntax at all — something that reads like "$5 and $10" prose.
		if _MATH_CURRENCY.match(body):
			return match.group(0)
		has_syntax = any(ch in body for ch in "\\^_{}")
		if not has_syntax and body[:1].isdigit() and " " in body:
			if any("a" <= ch <= "z" for ch in body):
				return match.group(0)
		return _sub(body, False, match.group(0))

	text = _MATH_DISPLAY.sub(lambda m: _sub(m.group(1), True, m.group(0)), text)
	text = _MATH_DISPLAY_BRACKET.sub(lambda m: _sub(m.group(1), True, m.group(0)), text)
	text = _MATH_INLINE_PAREN.sub(lambda m: _sub(m.group(1), False, m.group(0)), text)
	text = _MATH_INLINE.sub(_inline_dollar, text)
	return text, replacements


def render_markdown_html(text: str, *, font_size: str | None = "13px") -> str:
	"""Convert markdown to a self-contained HTML fragment for QTextEdit.

	Returns an HTML string with embedded stylesheet. If the optional `markdown`
	package is missing, returns an empty string so callers can fall back to
	`setMarkdown`. Pass ``font_size=None`` to let the host widget's font drive
	the base text size. LaTeX math (``$$…$$``, ``\\[…\\]``, ``\\(…\\)``, ``$…$``)
	is converted to lightweight inline HTML.
	"""
	if _markdown is None:
		return ""
	protected, math_map = _extract_math(text or "")
	source = _normalize_for_qt(protected)
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
	for token, fragment in math_map.items():
		html_body = html_body.replace(token, fragment)
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
