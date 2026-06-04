"""LLM-based working-fact extraction (4B JSON judgment + regex fallback).

Measured against the same long-term-fact test set as the alternatives:

    regex   F1 0.182  (recall 0.10 — only fixed patterns)
    embed   F1 0.857  (recall 0.90, precision 0.82 — 2 false positives)
    LLM-4B  F1 0.889  (recall 0.80, precision 1.00 — 0 false positives)

The LLM's precision 1.00 is the deciding factor: working facts sit in the
system prompt every turn, so a stored piece of chatter is permanent noise. The
LLM judges *and* extracts the clean fact + category in one call. Falls back to
the regex extractor when the LLM endpoint or JSON parse fails, so working
extraction never hard-fails.
"""

from __future__ import annotations

from typing import Any

from services.memory_tools_funcs.debug import mem_debug
from services.memory_tools_funcs.main_context.heuristic_memory import (
    _clean_fact,
    extract_explicit_facts,
)

_VALID_CATEGORIES = frozenset(
    {"name", "project", "preference", "profile", "constraint", "habit", "remember"}
)

_SYSTEM = (
    "사용자 발화에서 여러 대화에 걸쳐 기억할 안정적인 사용자 속성"
    "(이름, 프로젝트, 선호/성향, 전공/직업, 제약/알레르기, 습관/취미)을 추출합니다. "
    "일시적인 질문·요청·잡담에서는 추출하지 않습니다."
)
_PROMPT = (
    '발화: "{text}"\n\n'
    "이 발화가 여러 대화에 걸쳐 기억할 사용자 속성을 담고 있으면 다음 JSON으로만 답하세요:\n"
    '{{"fact": "추출한 핵심 사실", "category": "name|project|preference|profile|constraint|habit"}}\n'
    "일시적인 질문·요청·잡담이면 다음으로만 답하세요:\n"
    '{{"fact": null}}'
)


class LLMFactExtractor:
    """Judge+extract a long-term fact from one utterance via the chat LLM."""

    def __init__(self, raw_llm: Any) -> None:
        self.raw_llm = raw_llm
        self._can_json = callable(getattr(raw_llm, "ask_json", None))

    def extract(self, text: str) -> list[tuple[str, str]]:
        """Return ``[(category, fact)]`` for a long-term fact, else empty.

        Falls back to the regex extractor when the LLM is unavailable or its
        output can't be used."""
        cleaned = _clean_fact(text)
        if not cleaned:
            return []
        result = self._judge(cleaned)
        if result is None:
            facts = extract_explicit_facts(text)
            mem_debug("working", f"extract: LLM unavailable → regex fallback {facts} (utterance={cleaned!r})")
            return facts
        fact = _clean_fact(result.get("fact") if isinstance(result, dict) else "")
        if not fact:
            mem_debug("working", f"extract: rejected (no long-term fact) utterance={cleaned!r} llm_raw={result}")
            return []
        category = str((result.get("category") or "remember")).strip().lower()
        if category not in _VALID_CATEGORIES:
            category = "remember"
        mem_debug("working", f"extract: selected {category}={fact!r} (utterance={cleaned!r} llm_raw={result})")
        return [(category, fact)]

    def _judge(self, text: str) -> dict[str, Any] | None:
        """Ask the LLM; None signals "fall back to regex"."""
        if not self._can_json:
            return None
        try:
            result = self.raw_llm.ask_json(
                _SYSTEM, _PROMPT.format(text=text), reasoning=False
            )
            return result if isinstance(result, dict) else None
        except Exception as e:
            print(f"[memory][working_extract][warn] llm judge failed: {type(e).__name__}: {e}")
            return None
