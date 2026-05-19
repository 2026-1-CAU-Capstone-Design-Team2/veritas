"""Verify-flow planner tool.

Single LLM-driven step of the verification layer (the only one):
turn the user's request + AutoSurvey plan + grounded terms + short doc
hints into an *ordered* report-flow outline. Sentence-level retrieval
downstream (``services/verification/sections/sentence_retrieval.py``) then
assigns corpus sentences to each section.

Same shape as the project's other LLM tools:

* :class:`BaseTool` subclass with a function-calling schema (see
  ``tool_schema.json``) — registrable in the chat ToolRegistry so the same
  capability is discoverable from other entry points if needed.
* Owns *no* business state; the verification facade builds the doc hints
  and passes them in.
* Prompt lives in :mod:`core.prompts` (``VERIFY_FLOW_PLANNER_PROMPT``) so
  it sits alongside every other system prompt.
* Output is shape-validated *here* (one place) — the rest of the verify
  pipeline treats the returned sections list as already trustworthy.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.prompts import VERIFY_FLOW_PLANNER_PROMPT
from tools.tool import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# Hard caps the schema can't enforce from the LLM side. The verification
# config carries the user-visible knobs; these are last-line safeties.
_ABS_MIN_SECTIONS = 1
_ABS_MAX_SECTIONS = 12


class VerifyFlowPlannerTool(BaseTool):
    """LLM-based report-flow outliner used by the verification layer's Task 1."""

    def __init__(self, schema: dict[str, Any], llm=None) -> None:
        super().__init__(schema=schema)
        self._llm = llm

    @property
    def name(self) -> str:
        return "verify_flow_planner"

    def run(
        self,
        request_text: str = "",
        plan: dict[str, Any] | None = None,
        grounding: dict[str, Any] | None = None,
        doc_hints: list[str] | None = None,
        min_sections: int = 3,
        max_sections: int = 6,
        timeout_sec: float | None = 90.0,
    ) -> ToolResult:
        if self._llm is None:
            return ToolResult(
                success=False,
                error="verify_flow_planner requires an LLM client.",
            )

        min_sections = max(_ABS_MIN_SECTIONS, int(min_sections or _ABS_MIN_SECTIONS))
        max_sections = max(min_sections, min(int(max_sections or _ABS_MAX_SECTIONS), _ABS_MAX_SECTIONS))
        plan = plan or {}
        grounding = grounding or {}
        doc_hints = [str(hint).strip() for hint in (doc_hints or []) if str(hint).strip()]

        payload = self._build_payload(
            request_text=request_text,
            plan=plan,
            grounding=grounding,
            doc_hints=doc_hints,
            min_sections=min_sections,
            max_sections=max_sections,
        )

        try:
            raw = self._llm.ask_json(
                VERIFY_FLOW_PLANNER_PROMPT,
                json.dumps(payload, ensure_ascii=False, indent=2),
                reasoning=False,
                max_retries=2,
                stream=False,
                stream_label="verify-flow-planner",
                timeout_sec=timeout_sec,
            )
        except Exception as exc:  # pragma: no cover — surfaced via ToolResult
            logger.warning("verify_flow_planner: LLM call failed: %s", exc)
            return ToolResult(
                success=False,
                error=f"flow planner LLM call failed: {exc}",
            )

        sections = self._normalize_sections(raw, min_sections, max_sections)
        if len(sections) < min_sections:
            return ToolResult(
                success=False,
                error=(
                    f"flow planner returned {len(sections)} sections "
                    f"(< min {min_sections}); not enough to drive a report flow."
                ),
                data={"sections": sections},
            )

        return ToolResult(
            success=True,
            content=f"Planned {len(sections)} report section(s).",
            data={"sections": sections},
        )

    # -- internals -----------------------------------------------------------

    def _build_payload(
        self,
        *,
        request_text: str,
        plan: dict[str, Any],
        grounding: dict[str, Any],
        doc_hints: list[str],
        min_sections: int,
        max_sections: int,
    ) -> dict[str, Any]:
        must_cover = [
            str(item).strip()
            for item in (plan.get("must_cover") or [])
            if isinstance(item, str) and str(item).strip()
        ]
        keywords = [
            str(kw).strip()
            for kw in (plan.get("keywords") or [])
            if isinstance(kw, str) and str(kw).strip()
        ]
        grounded_terms = [
            str(gt).strip()
            for gt in (grounding.get("grounded_terms") or [])
            if isinstance(gt, str) and str(gt).strip()
        ]
        return {
            "request_text": (request_text or "").strip()[:2000],
            "plan": {
                "topic": str(plan.get("topic") or ""),
                "goal": str(plan.get("goal") or ""),
                "must_cover": must_cover,
                "keywords": keywords,
            },
            "grounding": {
                "grounded_terms": grounded_terms,
            },
            "doc_hints": doc_hints,
            "min_sections": min_sections,
            "max_sections": max_sections,
        }

    def _normalize_sections(
        self,
        raw: Any,
        min_sections: int,
        max_sections: int,
    ) -> list[dict[str, Any]]:
        """Validate + clean the LLM's section list. One place for all defensive
        parsing so callers can treat the returned list as trustworthy."""
        if not isinstance(raw, dict):
            return []
        raw_sections = raw.get("sections")
        if not isinstance(raw_sections, list):
            return []
        raw_sections = raw_sections[:max_sections]
        total = len(raw_sections)
        out: list[dict[str, Any]] = []
        for index, item in enumerate(raw_sections):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            description = str(item.get("description") or "").strip()
            keywords = [
                str(k).strip()
                for k in (item.get("keywords") or [])
                if isinstance(k, str) and str(k).strip()
            ]
            role = self._coerce_role(item.get("role"), index=index, total=total)
            out.append(
                {
                    "title": title,
                    "description": description,
                    "role": role,
                    "keywords": keywords,
                }
            )
        if len(out) < min_sections:
            return out  # caller decides what to do with an under-sized result
        return out

    @staticmethod
    def _coerce_role(value: Any, *, index: int, total: int) -> str:
        """Normalize a role string and enforce intro-first / conclusion-last."""
        text = str(value or "").strip().lower()
        if "intro" in text or text in {"서론", "도입", "개요"}:
            text = "intro"
        elif "conclus" in text or "summary" in text or text in {"결론", "마무리"}:
            text = "conclusion"
        elif text not in {"intro", "body", "conclusion"}:
            text = "body"
        # Positional sanity: a one-section outline is its own intro+conclusion,
        # so anything else stays body in the middle.
        if total <= 1:
            return "intro"
        if index == 0:
            return "intro"
        if index == total - 1:
            return "conclusion"
        if text == "intro" and index != 0:
            return "body"
        if text == "conclusion" and index != total - 1:
            return "body"
        return text
