"""Derive multi-query intent queries from artifacts (VERIFY_DESIGN.md §4.3.1).

Every intent query is pulled from material the user (or AutoSurvey's planner)
already produced — ``request.md``, ``plan.{topic, goal, keywords}``,
``grounding.grounded_terms`` — so no domain cue list is hard-coded (§1.9).
``origin`` records provenance so a coverage gap can be traced back to whichever
artifact spawned the query.
"""

from __future__ import annotations

from typing import Iterable

from ..models import Query


def _dedupe_preserve_order(queries: Iterable[Query]) -> list[Query]:
    """Keep the first occurrence of each text (case-insensitive, whitespace-stripped).

    Origins of duplicate queries are *dropped*, not merged — the first origin
    is the most specific (``request`` outranks ``plan.topic`` which outranks
    a stray keyword echo). Tracking every origin would clutter the output
    without changing the retrieval.
    """
    seen: set[str] = set()
    out: list[Query] = []
    for query in queries:
        key = query.text.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(query)
    return out


def extract_intent_queries(
    request_text: str,
    plan: dict,
    grounding: dict,
) -> list[Query]:
    """Build the multi-query set covering one workspace's user intent.

    Order matters: ``request`` first, then increasingly granular planner /
    grounding items. ``_dedupe_preserve_order`` then drops textual repeats.
    """
    candidates: list[Query] = []

    request = (request_text or "").strip()
    if request:
        candidates.append(Query(origin="request", text=request, type="full"))

    topic = (plan.get("topic") or "").strip() if isinstance(plan, dict) else ""
    if topic:
        candidates.append(Query(origin="plan.topic", text=topic, type="topic"))

    goal = (plan.get("goal") or "").strip() if isinstance(plan, dict) else ""
    if goal:
        candidates.append(Query(origin="plan.goal", text=goal, type="goal"))

    keywords = plan.get("keywords") if isinstance(plan, dict) else None
    for index, keyword in enumerate(keywords or []):
        if isinstance(keyword, str) and keyword.strip():
            candidates.append(
                Query(origin=f"plan.keyword[{index}]", text=keyword.strip(), type="keyword")
            )

    grounded_terms = grounding.get("grounded_terms") if isinstance(grounding, dict) else None
    for index, term in enumerate(grounded_terms or []):
        if isinstance(term, str) and term.strip():
            candidates.append(
                Query(origin=f"grounding.term[{index}]", text=term.strip(), type="term")
            )

    return _dedupe_preserve_order(candidates)


__all__ = ["extract_intent_queries"]
