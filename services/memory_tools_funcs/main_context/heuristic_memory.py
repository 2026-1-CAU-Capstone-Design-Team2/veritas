"""Deterministic extraction of explicit user facts for working context."""

from __future__ import annotations

import re


_MAX_FACT_CHARS = 180


# Each pattern carries the category its fact belongs to. "name"/"project" are
# single-valued (a new declaration replaces the old one); "remember" is
# multi-valued (each fact accumulates). The working layer uses the category to
# decide replace-vs-append, so "내 이름은 A" later followed by "내 이름은 B"
# leaves only B instead of both.
_EXPLICIT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:기억해줘|기억해|remember(?:\s+that)?)[\s:,-]+(.+)", re.IGNORECASE), "remember"),
    (re.compile(r"(?:내\s*이름은|제\s*이름은)\s+(.+)", re.IGNORECASE), "name"),
    (re.compile(r"(?:my\s+name\s+is|call\s+me)\s+(.+)", re.IGNORECASE), "name"),
    (re.compile(r"(?:프로젝트(?:\s*이름)?은|프로젝트(?:\s*명)?은)\s+(.+)", re.IGNORECASE), "project"),
    (re.compile(r"(?:project(?:\s+name)?\s+is|project\s+is)\s+(.+)", re.IGNORECASE), "project"),
)


def extract_explicit_facts(text: str) -> list[tuple[str, str]]:
    """Extract ``(category, fact)`` for each explicit stable declaration."""
    source = " ".join(str(text or "").split())
    if not source:
        return []

    facts: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pattern, category in _EXPLICIT_PATTERNS:
        match = pattern.search(source)
        if not match:
            continue
        fact = _clean_fact(match.group(1))
        if not fact:
            continue
        key = (category, fact.casefold())
        if key in seen:
            continue
        facts.append((category, fact))
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
