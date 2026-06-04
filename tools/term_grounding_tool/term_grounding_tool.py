from __future__ import annotations

import json
from typing import Any

from core.prompts import TERM_GROUNDING_PROMPT
from tools.tool import BaseTool, ToolResult


class TermGroundingTool(BaseTool):
    """Use the LLM to extract important literal terms from the user's request.

    This tool intentionally does not generate search queries. Query generation
    belongs to the query planner, which receives these grounded terms as input.
    """

    def __init__(self, schema: dict[str, Any], llm=None, tool_registry=None) -> None:
        super().__init__(schema=schema)
        self._llm = llm
        self._tool_registry = tool_registry

    @property
    def name(self) -> str:
        return "term_grounding"

    def run(
        self,
        user_request: str,
        max_terms: int = 8,
        max_seed_queries: int | None = None,  # kept only for backward-compatible callers
    ) -> ToolResult:
        del max_seed_queries

        user_request = self._normalize_request(user_request)
        if not user_request:
            return ToolResult(success=False, error="`user_request` must be a non-empty string.")

        if self._llm is None:
            return ToolResult(
                success=False,
                error="term_grounding requires an LLM; rule-based fallback extraction was removed.",
            )

        max_terms = self._normalize_max_terms(max_terms)
        payload = {
            "user_request": user_request,
            "max_terms": max_terms,
        }

        try:
            grounded_payload = self._llm.ask_json(
                TERM_GROUNDING_PROMPT,
                json.dumps(payload, ensure_ascii=False, indent=2),
                reasoning=False,
                max_retries=2,
                stream=False,
                stream_label="term-grounding",
                tools=None,
                tool_runner=None,
                max_tool_rounds=0,
                # Term extraction is a shallow task — cap reasoning spend on
                # API reasoning models (no-op for the local client).
                reasoning_effort="low",
            )
        except Exception as e:
            return ToolResult(success=False, error=f"LLM term extraction failed: {e}")

        request_language = str(grounded_payload.get("request_language") or "").strip().lower()
        grounded_terms = self._normalize_string_list(
            grounded_payload.get("grounded_terms"),
            max_items=max_terms,
        )
        candidate_entities = self._normalize_string_list(
            grounded_payload.get("candidate_entities"),
            max_items=max_terms,
        )
        disambiguation_notes = self._normalize_string_list(
            grounded_payload.get("disambiguation_notes"),
            max_items=max_terms,
        )

        if not grounded_terms:
            return ToolResult(
                success=False,
                error="LLM term extraction returned no grounded_terms.",
                data={
                    "request_language": request_language,
                    "grounded_terms": [],
                    "candidate_entities": candidate_entities,
                    "disambiguation_notes": disambiguation_notes,
                },
            )

        return ToolResult(
            success=True,
            content=f"Grounded {len(grounded_terms)} important term(s).",
            data={
                "request_language": request_language,
                "grounded_terms": grounded_terms,
                "candidate_entities": candidate_entities,
                "disambiguation_notes": disambiguation_notes,
            },
        )

    def _normalize_request(self, text: str) -> str:
        return " ".join(str(text or "").split())

    def _normalize_max_terms(self, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 8
        return max(1, min(parsed, 12))

    def _clean_term(self, term: Any) -> str:
        return " ".join(str(term or "").split()).strip()

    def _dedupe_preserve_order(self, items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            key = " ".join(item.lower().split())
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _normalize_string_list(self, value: Any, *, max_items: int) -> list[str]:
        items: list[str] = []

        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    clean = self._clean_term(item.get("term") or item.get("name") or "")
                else:
                    clean = self._clean_term(item)
                if clean:
                    items.append(clean)

        return self._dedupe_preserve_order(items)[:max_items]
