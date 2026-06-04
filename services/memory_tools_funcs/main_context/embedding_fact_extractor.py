"""DEPRECATED — superseded by LLMFactExtractor (llm_fact_extractor.py).

Kept as a measured baseline; NOT wired into the runtime. Embedding anchor-
contrast reached F1 0.857 for working-fact extraction, but the 4B LLM judge
reached F1 0.889 with precision 1.00 (zero false positives) on the same test
set, so the runtime uses the LLM extractor. This module is retained for
comparison/benchmarking only.

---

Embedding-based working-fact extraction (contrast against negative anchors).
A regex catches only a handful of fixed patterns (measured recall ~0.10),
while contrasting an utterance's embedding against positive (attribute-
declaration) vs negative (chatter) prototype anchors reaches ~0.90 recall /
~0.86 F1 with the small granite multilingual model.

Why contrast, not an absolute threshold: the small model packs every Korean
sentence into a narrow cosine band (0.79–0.88), so a fixed threshold cannot
separate facts from chatter (measured overlap). Nearest-anchor (pos vs neg) is
a *relative* decision and survives the low spread.

Degrades to the regex extractor when the embedding endpoint is unavailable.
"""

from __future__ import annotations

import math
from typing import Any

from services.memory_tools_funcs.main_context.heuristic_memory import (
    _clean_fact,
    extract_explicit_facts,
)


# Positive anchors per single-valued/multi category. "OOO" is the value slot —
# anchors describe the *shape* of an attribute declaration, not a specific value.
_POS_PROTOTYPES: dict[str, tuple[str, ...]] = {
    "name": ("내 이름은 OOO이야", "제 이름은 OOO입니다", "저를 OOO라고 불러주세요"),
    "project": ("내 프로젝트는 OOO야", "프로젝트 이름은 OOO입니다"),
    "preference": ("저는 OOO를 선호해요", "저는 OOO하는 편이에요", "저는 OOO를 좋아합니다"),
    "profile": ("저는 OOO 전공이에요", "제 직업은 OOO입니다", "저는 OOO 분야에서 일해요"),
    "constraint": ("저는 OOO를 못 먹어요", "OOO 알레르기가 있어요"),
    "habit": ("저는 매일 OOO를 합니다", "제 취미는 OOO예요"),
}

# Negative anchors: requests / chatter that must NOT become a long-term fact.
_NEG_PROTOTYPES: tuple[str, ...] = (
    "그거 좀 해줄래?", "정보 좀 알려줘", "다시 한번 보여줘", "수고했어요",
    "뭐 추천해줄래?", "이것 좀 분석해줘", "지금 상황이 어떤지 알려줘",
)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class EmbeddingFactExtractor:
    """[DEPRECATED — see module docstring] Classify an utterance as a long-term
    fact via positive/negative anchor contrast. Superseded by LLMFactExtractor."""

    # Required margin by which the best positive anchor must beat the best
    # negative one. 0.0 maximizes recall; a small positive value trades a little
    # recall for precision (fewer chatter utterances stored).
    MARGIN = 0.0

    def __init__(self, raw_llm: Any, *, margin: float | None = None) -> None:
        self.raw_llm = raw_llm
        self.margin = self.MARGIN if margin is None else float(margin)
        self._disabled = not callable(getattr(raw_llm, "embed", None))
        self._pos: dict[str, list[list[float]]] | None = None
        self._neg: list[list[float]] | None = None

    def _ensure_anchors(self) -> bool:
        """Embed the prototype anchors once; disable on failure."""
        if self._disabled:
            return False
        if self._pos is not None:
            return True
        try:
            self._pos = {
                cat: [self.raw_llm.embed(p) for p in prompts]
                for cat, prompts in _POS_PROTOTYPES.items()
            }
            self._neg = [self.raw_llm.embed(p) for p in _NEG_PROTOTYPES]
        except Exception as e:
            print(f"[memory][working_extract][warn] disabled: {type(e).__name__}: {e}")
            self._disabled = True
            self._pos = None
            self._neg = None
            return False
        return True

    def extract(self, text: str) -> list[tuple[str, str]]:
        """Return ``[(category, fact)]`` — one entry if the utterance is a
        long-term fact, else empty.

        Hybrid: regex first, embedding second. A regex matches name/project
        declarations with perfect precision (and pulls the value out cleanly),
        where the small embedding model can misjudge them. For everything regex
        can't pattern-match (preferences, profile, habits...), the embedding
        anchor-contrast extends recall. Embedding is skipped (returns regex
        result, possibly empty) when the endpoint is unavailable."""
        cleaned = _clean_fact(text)
        if not cleaned:
            return []
        regex_facts = extract_explicit_facts(text)
        if regex_facts:
            return regex_facts
        if not self._ensure_anchors():
            return []
        try:
            vec = self.raw_llm.embed(cleaned)
        except Exception:
            return []

        best_cat, best_pos = None, -1.0
        for cat, anchors in self._pos.items():
            c = max(_cosine(vec, a) for a in anchors)
            if c > best_pos:
                best_pos, best_cat = c, cat
        best_neg = max((_cosine(vec, a) for a in self._neg), default=0.0)

        if best_cat is not None and best_pos > best_neg + self.margin:
            return [(best_cat, cleaned)]
        return []
