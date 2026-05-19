"""Thin adapter — wraps :class:`VerifyFlowPlannerTool` into a function the
section pipeline can call directly.

The LLM call, prompt and JSON validation all live in the tool itself
(``tools/verify_flow_planner_tool/``) so the same capability is registrable
in the chat tool registry. This module:

1. Builds ``doc_hints`` from the loaded :class:`DocRecord` list (the tool
   itself stays I/O-free and doesn't know about ``DocRecord``).
2. Calls :meth:`VerifyFlowPlannerTool.run` and unpacks its
   :class:`ToolResult` into the pipeline's :class:`FlowSection` dataclasses.
3. Falls back to a ``must_cover``-derived outline if the tool reports a
   failure, so a missing or flaky LLM doesn't crash verification.
"""

from __future__ import annotations

import logging
from typing import Any

from tools.verify_flow_planner_tool import VerifyFlowPlannerTool

from ..models import DocRecord, FlowSection, VerificationConfig

logger = logging.getLogger(__name__)


def _doc_hint(doc: DocRecord) -> str | None:
    """One short '- Title: first summary line' string the tool can read.

    Title comes from index.json (already in DocRecord); the summary first
    line is the LLM-authored doc summary's opening sentence, kept short so
    a large workspace doesn't blow up the planner prompt.
    """
    title = (doc.title or "").strip() or doc.doc_id
    summary_first_line = ""
    if doc.summary:
        for line in doc.summary.splitlines():
            line = line.strip()
            if line:
                summary_first_line = line[:200]
                break
    if not summary_first_line:
        return f"- {title}"
    return f"- {title}: {summary_first_line}"


def _build_doc_hints(docs: list[DocRecord], limit: int) -> list[str]:
    eligible = [doc for doc in docs if not doc.is_duplicate][: max(0, int(limit))]
    return [hint for hint in (_doc_hint(doc) for doc in eligible) if hint]


def _coerce_flow_sections(raw_sections: list[dict[str, Any]]) -> list[FlowSection]:
    """Translate the tool's normalized section dicts into pipeline dataclasses."""
    out: list[FlowSection] = []
    for index, item in enumerate(raw_sections or []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        out.append(
            FlowSection(
                id=index,
                order=index,
                title=title,
                description=str(item.get("description") or "").strip(),
                role=str(item.get("role") or "body"),
                keywords=[
                    str(k).strip()
                    for k in (item.get("keywords") or [])
                    if isinstance(k, str) and str(k).strip()
                ],
            )
        )
    return out


def _fallback_sections(plan: dict, cfg: VerificationConfig) -> list[FlowSection]:
    """Degraded must_cover-based outline when the LLM tool is unavailable.

    Not a good report flow — but useful enough for sentence retrieval to
    still produce something. The caller flips ``flow_source`` to
    ``"fallback"`` so the UI warns the user to retry once the LLM is back.
    """
    must_cover = [
        str(item).strip()
        for item in (plan.get("must_cover") or [])
        if isinstance(item, str) and str(item).strip()
    ]
    if not must_cover:
        return []
    items = must_cover[: int(cfg.flow_planner_max_sections)]
    total = len(items)
    sections: list[FlowSection] = []
    for index, text in enumerate(items):
        if index == 0:
            role = "intro"
        elif index == total - 1 and total > 1:
            role = "conclusion"
        else:
            role = "body"
        sections.append(
            FlowSection(
                id=index,
                order=index,
                title=text,
                description="",
                role=role,
                keywords=[text],
            )
        )
    return sections


def plan_report_flow(
    *,
    flow_planner_tool: VerifyFlowPlannerTool,
    request_text: str,
    plan: dict,
    grounding: dict,
    docs: list[DocRecord],
    cfg: VerificationConfig,
) -> tuple[list[FlowSection], str]:
    """Return ``(sections, source)``. ``source`` ∈ ``{"llm", "fallback"}``.

    The tool is the only LLM consumer in verification (§11 amended). Any
    failure is logged and degraded to a ``must_cover``-based outline; the
    rest of the pipeline (sentence retrieval) still produces useful
    assignments on top of the degraded outline.
    """
    doc_hints = _build_doc_hints(docs, int(cfg.flow_planner_doc_hints))
    result = flow_planner_tool.run(
        request_text=request_text or "",
        plan=plan or {},
        grounding=grounding or {},
        doc_hints=doc_hints,
        min_sections=int(cfg.flow_planner_min_sections),
        max_sections=int(cfg.flow_planner_max_sections),
        timeout_sec=float(cfg.flow_planner_timeout_sec),
    )

    if not result.success or not isinstance(result.data, dict):
        logger.warning(
            "verification: flow planner tool failed (%s); falling back to must_cover.",
            result.error or "unknown error",
        )
        return _fallback_sections(plan, cfg), "fallback"

    sections = _coerce_flow_sections(list(result.data.get("sections") or []))
    if len(sections) < int(cfg.flow_planner_min_sections):
        logger.warning(
            "verification: flow planner returned %d sections (< min %d); falling back",
            len(sections),
            cfg.flow_planner_min_sections,
        )
        return _fallback_sections(plan, cfg), "fallback"
    return sections, "llm"


__all__ = ["plan_report_flow"]
