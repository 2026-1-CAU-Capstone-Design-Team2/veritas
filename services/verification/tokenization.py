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

import shutil
import tempfile
import re
from pathlib import Path

from kiwipiepy import Kiwi
from kiwipiepy_model import __version__ as _KIWI_MODEL_VERSION
from kiwipiepy_model import get_model_path

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
_ASCII_MODEL_PATH: str | None = None


def _is_ascii_path(path: Path) -> bool:
    return all(ord(ch) < 128 for ch in str(path))


def _copy_model_to_ascii_cache(src: Path) -> Path:
    """Return an ASCII-only model path for Kiwi's native file loader."""
    global _ASCII_MODEL_PATH
    if _ASCII_MODEL_PATH is not None:
        return Path(_ASCII_MODEL_PATH)

    dst = Path(tempfile.gettempdir()) / "veritas_kiwipiepy_model" / _KIWI_MODEL_VERSION
    dst.mkdir(parents=True, exist_ok=True)

    for item in src.iterdir():
        if item.name == "__pycache__":
            continue
        target = dst / item.name
        if item.is_dir():
            if not target.exists():
                shutil.copytree(item, target)
            continue
        if not target.exists() or target.stat().st_size != item.stat().st_size:
            shutil.copy2(item, target)

    _ASCII_MODEL_PATH = str(dst)
    return dst


def create_kiwi() -> Kiwi:
    """Build Kiwi with the installed model package path.

    Some Windows virtualenvs fail to resolve kiwipiepy_model implicitly. Also,
    Kiwi's native loader can fail to open model files when the package lives
    under a non-ASCII path, so cache the model under an ASCII temp path.
    """
    model_path = Path(get_model_path()).resolve()
    if not _is_ascii_path(model_path):
        model_path = _copy_model_to_ascii_cache(model_path)
    return Kiwi(model_path=str(model_path))


class HybridTokenizer:
    """Callable ``str -> list[str]`` tokenizer. Kiwi morphemes + Latin spans.

    Construct once and reuse for the whole service lifetime — instantiating
    :class:`~kiwipiepy.Kiwi` costs a few hundred milliseconds.
    """

    def __init__(self) -> None:
        self._kiwi = create_kiwi()

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


__all__ = ["HybridTokenizer", "create_kiwi"]
