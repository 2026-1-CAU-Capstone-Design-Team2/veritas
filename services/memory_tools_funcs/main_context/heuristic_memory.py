"""Deterministic extraction of explicit user facts for working context."""

from __future__ import annotations

import re


_MAX_FACT_CHARS = 180


_EXPLICIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:기억해줘|기억해|remember(?:\s+that)?)[\s:,-]+(.+)", re.IGNORECASE),
    re.compile(r"(?:내\s*이름은|제\s*이름은)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:my\s+name\s+is|call\s+me)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:프로젝트(?:\s*이름)?은|프로젝트(?:\s*명)?은)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:project(?:\s+name)?\s+is|project\s+is)\s+(.+)", re.IGNORECASE),
)


def extract_explicit_facts(text: str) -> list[str]:
    """Extract facts only when the user explicitly declares something stable."""
    source = " ".join(str(text or "").split())
    if not source:
        return []

    facts: list[str] = []
    seen: set[str] = set()
    for pattern in _EXPLICIT_PATTERNS:
        match = pattern.search(source)
        if not match:
            continue
        fact = _clean_fact(match.group(1))
        if not fact:
            continue
        key = fact.casefold()
        if key in seen:
            continue
        facts.append(fact)
        seen.add(key)
    return facts


def _clean_fact(raw: str) -> str:
    fact = str(raw or "").strip()
    fact = re.split(r"(?:[.!?。！？]\s+|[\n\r]+)", fact, maxsplit=1)[0].strip()
    fact = fact.strip(" \t:;,.!?。！？\"'`")
    if not fact:
        return ""
    if len(fact) > _MAX_FACT_CHARS:
        fact = fact[:_MAX_FACT_CHARS].rstrip()
    return fact
