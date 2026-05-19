"""Rule-based cleanup for over-escaped LaTeX in LLM-authored markdown.

Local llama-server models routinely double-escape backslashes when they emit
math expressions inside a markdown response — they confuse JSON escaping
("``\\\\``" means one backslash in JSON) with markdown source ("``\\\\``"
means a literal "``\\``", which is the LaTeX line-break command). The result
is that ``\\mathcal{L}`` (one backslash, the LaTeX font command) gets written
to ``final.md`` as ``\\\\mathcal{L}`` (two backslashes), so the markdown
renderer treats ``\\\\`` as a forced newline and breaks ``mathcal{L}`` out
into prose — the math expression becomes unreadable.

This module is the single non-LLM post-processor that runs after both
:data:`core.prompts.BATCH_SUMMARY_PROMPT` and :data:`core.prompts.FINAL_PROMPT`
output, before the markdown is written to disk. It is intentionally
conservative: it only re-balances backslash runs *inside detected math
blocks* (``$$…$$``, ``$…$``, ``\\(…\\)``, ``\\[…\\]``) so a literal "``\\\\``"
in surrounding prose stays untouched.

There is no widely-available library that does this specific de-escaping
("undo over-escaping back to canonical TeX"): mature TeX parsers like
``pylatexenc`` assume their input is already valid TeX and have no notion
of "the source has too many backslashes". A focused regex pass is the
right tool for the job, and the rules are short enough to audit by hand.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# A maximal run of backslashes followed by either a non-backslash character
# or end-of-string. The capture groups let the replacement function decide
# how many backslashes to keep (see :func:`_halve_run`).
_BACKSLASH_RUN_RE = re.compile(r"(\\+)([^\\]|\Z)")


def _halve_run(match: "re.Match[str]") -> str:
    """Convert a run of ``N`` backslashes followed by ``X`` into ``ceil(N/2)``
    backslashes + ``X``.

    Why halve: the LLM doubled every backslash. ``\\\\mathcal`` (2 backslashes,
    LLM output) should become ``\\mathcal`` (1 backslash, real LaTeX). A LaTeX
    line-break ``\\\\`` (LLM output: 4 backslashes) should become ``\\\\`` →
    ``\\\\`` after halving (2 backslashes — still the LaTeX newline). A
    forced-space ``\\ `` (LLM output: ``\\\\ `` with 2 backslashes + space) →
    ``\\ `` with 1 backslash + space.

    Why ceil for odd runs: an odd run is malformed (the LLM dropped one
    backslash) but we'd rather keep the command intact than throw away the
    last command char. ``\\\\\\mathcal`` (3 backslashes + mathcal) → 2
    backslashes + mathcal, which keeps the command visible even if the line
    break before it was malformed.
    """
    run = match.group(1)
    following = match.group(2)
    n = len(run)
    halved = (n + 1) // 2  # ceil(n / 2)
    return ("\\" * halved) + following


# Display math: ``$$...$$``. Captured non-greedily so two adjacent display
# blocks don't get merged into one. Uses ``re.DOTALL`` so multi-line math
# (the common case after the LLM puts ``\\\\`` line breaks inside) is
# handled correctly.
_DISPLAY_MATH_RE = re.compile(r"\$\$(.+?)\$\$", flags=re.DOTALL)

# Inline math: ``$…$``. Bounded to a single line so a stray ``$`` two
# paragraphs apart does not accidentally swallow the prose between them.
# Excludes ``$$`` (handled by the display rule above) and the dollar-as-
# currency case (``$50``) by requiring at least one non-``$`` char inside.
_INLINE_MATH_RE = re.compile(r"(?<!\$)\$([^$\n]+?)\$(?!\$)")

# ``\\[...\\]`` display math. Less common in LLM output but covered.
_BRACKET_DISPLAY_RE = re.compile(r"\\\[(.+?)\\\]", flags=re.DOTALL)

# ``\\(...\\)`` inline math.
_BRACKET_INLINE_RE = re.compile(r"\\\((.+?)\\\)", flags=re.DOTALL)


def _clean_math_block(inner: str) -> str:
    """Apply backslash halving inside one detected math block."""
    return _BACKSLASH_RUN_RE.sub(_halve_run, inner)


def clean_latex_in_markdown(markdown: str) -> str:
    """Return ``markdown`` with over-escaped backslashes fixed inside math
    blocks.

    Operates idempotently: running this on an already-canonical document is a
    no-op (a single backslash followed by a letter is left alone — the run
    has length 1 and ``ceil(1/2)`` is 1, so it stays at one backslash). This
    matters because the same final.md may be re-rendered or re-saved through
    the pipeline more than once.

    Math blocks outside of the four supported delimiters are left untouched.
    If a future template introduces a new math env (e.g. an MDX-style
    ``<Math>...</Math>`` tag) it will need its own delimiter handler here.
    """
    if not markdown:
        return markdown

    def _replace_display(match: "re.Match[str]") -> str:
        return "$$" + _clean_math_block(match.group(1)) + "$$"

    def _replace_inline(match: "re.Match[str]") -> str:
        return "$" + _clean_math_block(match.group(1)) + "$"

    def _replace_bracket_display(match: "re.Match[str]") -> str:
        return "\\[" + _clean_math_block(match.group(1)) + "\\]"

    def _replace_bracket_inline(match: "re.Match[str]") -> str:
        return "\\(" + _clean_math_block(match.group(1)) + "\\)"

    cleaned = _DISPLAY_MATH_RE.sub(_replace_display, markdown)
    cleaned = _BRACKET_DISPLAY_RE.sub(_replace_bracket_display, cleaned)
    cleaned = _INLINE_MATH_RE.sub(_replace_inline, cleaned)
    cleaned = _BRACKET_INLINE_RE.sub(_replace_bracket_inline, cleaned)
    return cleaned


__all__ = ["clean_latex_in_markdown"]
