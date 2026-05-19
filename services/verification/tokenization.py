"""Korean/English hybrid tokenizer for the verification layer's IR channels.

Used by both the BM25 index and the c-TF-IDF section labeller. The corpus mixes
Korean and English, so:

* Korean / CJK / numeric morphemes come from Kiwi, keeping only content-bearing
  tags (nouns, predicates, Han characters, numerals) and dropping particles,
  endings and suffixes.
* Latin spans are taken from a regex pass instead of Kiwi's ``SL`` tag. Kiwi
  already emits clean ``SL`` tokens, so keeping *both* would double-count every
  English word in BM25 term frequencies; the regex also keeps compound
  identifiers (``gpt-4``, ``oauth2.0``) intact, which Kiwi fragments.

This is general language processing — no domain assumptions, no keyword lists
(VERIFY_DESIGN.md §1.9).
"""

from __future__ import annotations

import re

from kiwipiepy import Kiwi

# Kiwi POS tags worth keeping — content-bearing morphemes only. Deliberately
# narrower than a blanket "all N*/V*": bound nouns (NNB: 수/것/등), pronouns
# (NP), numeral words (NR), auxiliary verbs (VX) and copulas (VCP/VCN) are
# function morphemes, not the "의미 형태소" the design calls for — keeping them
# pollutes both BM25 term frequencies and the c-TF-IDF section labels. NNG/NNP =
# common/proper nouns, VV/VA = verbs/adjectives, SH = Han, SN = numerals. SL
# (Latin) is handled by the regex pass instead — see module docstring.
_KEEP_TAGS = frozenset({"NNG", "NNP", "VV", "VA", "SH", "SN"})

# Latin / alphanumeric identifier spans (>= 2 chars). Surrounding punctuation is
# stripped afterwards so "guide." and "guide" tokenize the same.
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-.]+")


class HybridTokenizer:
    """Callable ``str -> list[str]`` tokenizer. Kiwi morphemes + Latin spans.

    Construct once and reuse for the whole service lifetime — instantiating
    :class:`~kiwipiepy.Kiwi` costs a few hundred milliseconds.
    """

    def __init__(self) -> None:
        self._kiwi = Kiwi()

    def __call__(self, text: str) -> list[str]:
        if not text:
            return []

        tokens: list[str] = []

        for tok in self._kiwi.tokenize(text):
            # ``split("-")`` guards against Kiwi sub-tagged forms (e.g. VV-I).
            if tok.tag.split("-", 1)[0] in _KEEP_TAGS:
                form = tok.form.lower()
                if form:
                    tokens.append(form)

        for match in _LATIN_RE.findall(text):
            cleaned = match.strip("._-").lower()
            if cleaned:
                tokens.append(cleaned)

        return tokens


__all__ = ["HybridTokenizer"]
