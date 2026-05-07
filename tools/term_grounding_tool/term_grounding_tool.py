from __future__ import annotations

import json
import re
from typing import Any

from core.prompts import TERM_GROUNDING_PROMPT
from tools.llm_tooling import build_llm_tooling
from tools.tool import BaseTool, ToolResult


class TermGroundingTool(BaseTool):
    LLM_EXPOSED_TOOL_NAMES = ("current_time",)

    def __init__(self, schema: dict[str, Any], llm=None, tool_registry=None) -> None:
        super().__init__(schema=schema)
        self._llm = llm
        self._tool_registry = tool_registry

    @property
    def name(self) -> str:
        return "term_grounding"

    def run(self, user_request: str, max_seed_queries: int = 6) -> ToolResult:
        user_request = self._normalize_request(user_request)
        if not user_request:
            return ToolResult(success=False, error="`user_request` must be a non-empty string.")

        max_seed_queries = max(2, min(int(max_seed_queries), 12))

        request_language = self._detect_language(user_request)
        heuristic_terms = self._extract_grounded_terms(user_request)
        heuristic_seed_queries = self._build_seed_queries(
            user_request=user_request,
            grounded_terms=heuristic_terms,
            request_language=request_language,
            max_seed_queries=max_seed_queries,
        )

        grounded_terms = heuristic_terms
        candidate_entities: list[str] = []
        disambiguation_notes: list[str] = []
        seed_queries = heuristic_seed_queries

        if self._llm is not None:
            llm_tools, llm_tool_runner = self._build_llm_tooling(user_request)
            payload = {
                "user_request": user_request,
                "request_language": request_language,
                "heuristic_grounded_terms": heuristic_terms,
                "heuristic_seed_queries": heuristic_seed_queries,
            }
            try:
                grounded_payload = self._llm.ask_json(
                    TERM_GROUNDING_PROMPT,
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    reasoning=False,
                    max_retries=2,
                    stream=False,
                    stream_label="term-grounding",
                    tools=llm_tools,
                    tool_runner=llm_tool_runner,
                    max_tool_rounds=2,
                )

                request_language = (
                    str(grounded_payload.get("request_language") or request_language)
                    .strip()
                    .lower()
                ) or request_language

                grounded_terms = self._normalize_string_list(
                    grounded_payload.get("grounded_terms"),
                    fallback=heuristic_terms,
                    max_items=10,
                )
                candidate_entities = self._normalize_string_list(
                    grounded_payload.get("candidate_entities"),
                    fallback=[],
                    max_items=10,
                )
                disambiguation_notes = self._normalize_string_list(
                    grounded_payload.get("disambiguation_notes"),
                    fallback=[],
                    max_items=10,
                )
                seed_queries = self._normalize_string_list(
                    grounded_payload.get("seed_queries"),
                    fallback=heuristic_seed_queries,
                    max_items=max_seed_queries,
                )
            except Exception as e:
                print(f"[grounding][fallback] LLM grounding failed: {e}")

        return ToolResult(
            success=True,
            content=f"Grounded {len(grounded_terms)} term(s); built {len(seed_queries)} seed querie(s).",
            data={
                "request_language": request_language,
                "grounded_terms": grounded_terms,
                "candidate_entities": candidate_entities,
                "disambiguation_notes": disambiguation_notes,
                "seed_queries": seed_queries,
            },
        )

    def _normalize_request(self, text: str) -> str:
        text = (text or "").strip()
        return re.sub(r"\s+", " ", text)

    def _detect_language(self, text: str) -> str:
        if re.search(r"[가-힣]", text):
            return "ko"
        return "en"

    def _extract_grounded_terms(self, user_request: str) -> list[str]:
        terms: list[str] = []

        for match in re.findall(r"\*\*(.+?)\*\*", user_request):
            clean = self._clean_term(match)
            if clean:
                terms.append(clean)

        for match in re.findall(r"[\"“”'‘’]([^\"“”'‘’]{2,120})[\"“”'‘’]", user_request):
            clean = self._clean_term(match)
            if clean:
                terms.append(clean)

        for full, abbr in re.findall(
            r"([A-Za-z][A-Za-z0-9\-\s]{2,80})\(([A-Z][A-Z0-9\-]{1,12})\)",
            user_request,
        ):
            clean_full = self._clean_term(full)
            clean_abbr = self._clean_term(abbr)
            if clean_full:
                terms.append(clean_full)
            if clean_abbr:
                terms.append(clean_abbr)

        for acronym in re.findall(r"\b[A-Z][A-Z0-9\-]{1,12}\b", user_request):
            clean = self._clean_term(acronym)
            if clean:
                terms.append(clean)

        deduped = self._dedupe_preserve_order(terms)
        if deduped:
            return deduped[:10]

        fallback = self._clean_term(user_request[:120])
        return [fallback] if fallback else []

    def _clean_term(self, term: str) -> str:
        term = re.sub(r"\s+", " ", (term or "")).strip()
        return term.strip("-:;,. ")

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

    def _build_seed_queries(
        self,
        *,
        user_request: str,
        grounded_terms: list[str],
        request_language: str,
        max_seed_queries: int,
    ) -> list[str]:
        queries: list[str] = []

        for term in grounded_terms:
            queries.append(f'"{term}" arXiv')
            if request_language == "ko":
                queries.append(f'"{term}" 정의')
                queries.append(f'"{term}" conference paper')
            else:
                queries.append(f'"{term}" definition')
                queries.append(f'"{term}" conference paper')

        queries = self._dedupe_preserve_order([q.strip() for q in queries if q.strip()])
        if len(queries) < 2:
            shortened = self._clean_term(user_request[:120])
            queries.extend([
                f'"{shortened}" arXiv',
                f'"{shortened}" conference paper',
            ])
            queries = self._dedupe_preserve_order([q.strip() for q in queries if q.strip()])

        return queries[:max_seed_queries]

    def _normalize_string_list(
        self,
        value: Any,
        *,
        fallback: list[str],
        max_items: int,
    ) -> list[str]:
        items: list[str] = []

        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    clean = self._clean_term(item)
                elif isinstance(item, dict):
                    candidate = item.get("term") or item.get("name") or item.get("query") or ""
                    clean = self._clean_term(str(candidate))
                else:
                    clean = self._clean_term(str(item))
                if clean:
                    items.append(clean)

        if not items:
            items = [self._clean_term(x) for x in fallback if self._clean_term(x)]

        return self._dedupe_preserve_order(items)[:max_items]

    def _build_llm_tooling(self, _user_request: str):
        return build_llm_tooling(
            self._tool_registry,
            stage_label="grounding",
            allowed_tool_names=self.LLM_EXPOSED_TOOL_NAMES,
        )
