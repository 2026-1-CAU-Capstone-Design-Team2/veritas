from __future__ import annotations

import json
import re
from typing import Any

from core.prompts import INITIAL_PLANNER_PROMPT, REPLANNER_PROMPT
from tools.llm_tooling import build_llm_tooling
from tools.tool import BaseTool, ToolResult


class QueryPlanTool(BaseTool):
    PLAN_REASONING_ENABLED = True
    LLM_EXPOSED_TOOL_NAMES = ("current_time",)

    def __init__(self, schema: dict[str, Any], llm, run_store_service, tool_registry=None) -> None:
        super().__init__(schema=schema)
        self._llm = llm
        self._run_store_service = run_store_service
        self._tool_registry = tool_registry

    @property
    def name(self) -> str:
        return "query_plan"

    def run(
        self,
        user_request: str,
        force: bool = False,
        *,
        mode: str = "initial",
        grounding: dict[str, Any] | None = None,
        prior_plan: dict[str, Any] | None = None,
        gap_directions: list[str] | None = None,
        used_queries: list[str] | None = None,
        save: bool = True,
    ) -> ToolResult:
        user_request = (user_request or "").strip()
        if not user_request:
            return ToolResult(success=False, error="`user_request` must be a non-empty string.")

        mode = (mode or "initial").strip().lower()
        if mode not in {"initial", "replan"}:
            return ToolResult(success=False, error="`mode` must be one of: initial, replan")

        gap_directions = [
            str(item).strip()
            for item in (gap_directions or [])
            if str(item).strip()
        ]
        used_queries = [
            str(item).strip()
            for item in (used_queries or [])
            if str(item).strip()
        ]

        prepared_gap_directions = gap_directions
        if mode == "replan":
            prepared_gap_directions = self._prepare_replan_gap_directions(
                gap_directions,
                user_request=user_request,
                prior_plan=prior_plan,
            )

        llm_tools, llm_tool_runner = self._build_llm_tooling(user_request)

        try:
            can_use_cached_initial = (
                mode == "initial"
                and self._run_store_service.plan_exists()
                and not force
                and not grounding
                and not prior_plan
                and not prepared_gap_directions
            )

            if can_use_cached_initial:
                plan = self._normalize_plan(
                    self._run_store_service.load_plan(),
                    user_request=user_request,
                    mode=mode,
                    used_queries=used_queries,
                    allow_empty_search_queries=False,
                )
            else:
                planner_prompt = REPLANNER_PROMPT if mode == "replan" else INITIAL_PLANNER_PROMPT
                planner_input = {
                    "mode": mode,
                    "user_request": user_request,
                    "grounding": grounding or {},
                    "prior_plan": prior_plan or {},
                    "gap_directions": prepared_gap_directions,
                    "used_queries": used_queries,
                }

                plan = self._llm.ask_json(
                    planner_prompt,
                    json.dumps(planner_input, ensure_ascii=False, indent=2),
                    reasoning=self.PLAN_REASONING_ENABLED,
                    max_retries=2,
                    stream=False,
                    stream_label=f"plan:{mode}",
                    tools=llm_tools,
                    tool_runner=llm_tool_runner,
                    max_tool_rounds=2,
                )

                plan = self._normalize_plan(
                    plan,
                    user_request=user_request,
                    mode=mode,
                    used_queries=used_queries,
                    allow_empty_search_queries=(mode == "replan"),
                )

                if mode == "replan":
                    plan = self._refresh_replan_fields(
                        plan,
                        user_request=user_request,
                        prior_plan=prior_plan,
                        gap_directions=prepared_gap_directions,
                        used_queries=used_queries,
                        gaps_already_filtered=True,
                    )

            if save:
                self._run_store_service.save_plan(plan)

            return ToolResult(
                success=True,
                content=(
                    f"Built {mode} research plan with "
                    f"{len(plan.get('search_queries', []))} search queries."
                ),
                data=plan,
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to build query plan: {e}")

    def _normalize_plan(
        self,
        payload: Any,
        *,
        user_request: str,
        mode: str,
        used_queries: list[str],
        allow_empty_search_queries: bool,
    ) -> dict[str, Any]:
        plan = dict(payload) if isinstance(payload, dict) else {}

        topic = str(plan.get("topic") or user_request).strip() or user_request
        goal = str(plan.get("goal") or "").strip()
        must_cover = self._normalize_list(plan.get("must_cover"), max_items=20)
        keywords = self._normalize_list(plan.get("keywords"), max_items=20)
        search_queries = self._normalize_list(plan.get("search_queries"), max_items=20)

        used_query_keys = {self._normalize_query(q) for q in used_queries}
        filtered_queries: list[str] = []
        for query in search_queries:
            if self._normalize_query(query) in used_query_keys:
                continue
            filtered_queries.append(query)

        if not filtered_queries and not allow_empty_search_queries:
            filtered_queries = [user_request]

        plan["topic"] = topic
        plan["goal"] = goal
        plan["must_cover"] = must_cover
        plan["keywords"] = keywords
        plan["search_queries"] = filtered_queries
        plan["plan_mode"] = mode
        plan["used_query_count"] = len(used_queries)
        return plan

    def _refresh_replan_fields(
        self,
        plan: dict[str, Any],
        *,
        user_request: str,
        prior_plan: dict[str, Any] | None,
        gap_directions: list[str],
        used_queries: list[str],
        gaps_already_filtered: bool = False,
    ) -> dict[str, Any]:
        cleaned_gaps = self._clean_gap_directions(gap_directions, max_items=12)
        if gaps_already_filtered:
            relevant_gaps = cleaned_gaps
        else:
            relevant_gaps = self._filter_relevant_items(
                cleaned_gaps,
                user_request=user_request,
                prior_plan=prior_plan,
            )

        search_queries = self._normalize_list(plan.get("search_queries"), max_items=20)
        relevant_queries = self._filter_relevant_items(
            search_queries,
            user_request=user_request,
            prior_plan=prior_plan,
        )

        if search_queries and not relevant_queries:
            # Avoid no-op loops when lexical relevance filtering is over-strict.
            relevant_queries = search_queries

        plan["search_queries"] = relevant_queries

        if cleaned_gaps and not relevant_gaps and not gaps_already_filtered:
            print("[replan][relevance-filter] dropped all provided gaps as non-relevant")

        previous_plan = prior_plan if isinstance(prior_plan, dict) else {}

        if relevant_gaps:
            must_cover_pool = (
                relevant_gaps
                + self._normalize_list(plan.get("must_cover"), max_items=20)
                + self._normalize_list(previous_plan.get("must_cover"), max_items=20)
            )
            keyword_pool = (
                self._keywords_from_gap_directions(relevant_gaps, max_items=20)
                + self._normalize_list(plan.get("keywords"), max_items=20)
                + self._normalize_list(previous_plan.get("keywords"), max_items=20)
            )

            plan["must_cover"] = self._normalize_list(must_cover_pool, max_items=20)
            plan["keywords"] = self._normalize_list(keyword_pool, max_items=20)

        if not plan.get("search_queries"):
            fallback_queries = self._build_gap_queries(
                relevant_gaps,
                used_queries=used_queries,
                max_items=10,
            )
            if not fallback_queries:
                fallback_queries = self._recover_unused_prior_queries(
                    prior_plan=previous_plan,
                    used_queries=used_queries,
                    max_items=10,
                )
            plan["search_queries"] = fallback_queries

        return plan

    def _prepare_replan_gap_directions(
        self,
        gap_directions: list[str],
        *,
        user_request: str,
        prior_plan: dict[str, Any] | None,
    ) -> list[str]:
        cleaned_gaps = self._clean_gap_directions(gap_directions, max_items=12)
        relevant_gaps = self._filter_relevant_items(
            cleaned_gaps,
            user_request=user_request,
            prior_plan=prior_plan,
        )
        if cleaned_gaps and not relevant_gaps:
            print(
                "[replan][relevance-filter] dropped all provided gaps as non-relevant; "
                "fallback=cleaned-gaps"
            )
            return cleaned_gaps
        return relevant_gaps

    def _recover_unused_prior_queries(
        self,
        *,
        prior_plan: dict[str, Any] | None,
        used_queries: list[str],
        max_items: int,
    ) -> list[str]:
        previous = prior_plan if isinstance(prior_plan, dict) else {}
        previous_queries = self._normalize_list(previous.get("search_queries"), max_items=50)
        if not previous_queries:
            return []

        used = {self._normalize_query(query) for query in used_queries}
        recovered: list[str] = []
        for query in previous_queries:
            key = self._normalize_query(query)
            if not key or key in used:
                continue
            recovered.append(query)
            if len(recovered) >= max_items:
                break

        return recovered

    def _normalize_list(self, value: Any, *, max_items: int) -> list[str]:
        if not isinstance(value, list):
            return []

        deduped: list[str] = []
        seen: set[str] = set()

        for item in value:
            text = str(item).strip()
            if not text:
                continue
            key = self._normalize_query(text)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(text)
            if len(deduped) >= max_items:
                break

        return deduped

    def _clean_gap_directions(self, gaps: list[str], *, max_items: int) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()

        for raw in gaps:
            text = str(raw).strip()
            if not text:
                continue

            if text.startswith("|"):
                continue

            if re.fullmatch(r"[-_*=]{3,}", text):
                continue

            text = re.sub(r"^[\-*]\s+", "", text)
            text = re.sub(r"^\d+\.\s+", "", text)
            text = re.sub(r"^\*\*note:\*\*\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s+", " ", text).strip(" -*")

            if not text:
                continue

            key = self._normalize_query(text)
            if key in seen:
                continue

            seen.add(key)
            cleaned.append(text)
            if len(cleaned) >= max_items:
                break

        return cleaned

    def _keywords_from_gap_directions(self, gaps: list[str], *, max_items: int) -> list[str]:
        candidates: list[str] = []

        for gap in gaps:
            quoted = re.findall(r'"([^"]+)"', gap)
            candidates.extend(quoted)

            head = gap.split(":", 1)[0].strip()
            head = re.sub(r"^\d+\.\s+", "", head)
            if head and not head.startswith("|"):
                candidates.append(head)

        return self._normalize_list(candidates, max_items=max_items)

    def _build_gap_queries(
        self,
        gaps: list[str],
        *,
        used_queries: list[str],
        max_items: int,
    ) -> list[str]:
        used = {self._normalize_query(query) for query in used_queries}
        queries: list[str] = []
        seen: set[str] = set()

        for gap in gaps:
            query = f'"{gap}"'
            key = self._normalize_query(query)
            if not key or key in seen or key in used:
                continue
            seen.add(key)
            queries.append(query)
            if len(queries) >= max_items:
                break

        return queries

    def _filter_relevant_items(
        self,
        items: list[str],
        *,
        user_request: str,
        prior_plan: dict[str, Any] | None,
    ) -> list[str]:
        if not items:
            return []

        anchors = self._build_relevance_anchors(user_request=user_request, prior_plan=prior_plan)
        anchor_texts = anchors.get("texts", [])
        anchor_tokens = anchors.get("tokens", set())

        if not anchor_texts and not anchor_tokens:
            return items

        filtered: list[str] = []
        for item in items:
            if self._is_relevant_to_request(
                item,
                anchor_texts=anchor_texts,
                anchor_tokens=anchor_tokens,
            ):
                filtered.append(item)

        if len(filtered) != len(items):
            print(
                "[replan][relevance-filter] "
                f"kept={len(filtered)} dropped={len(items) - len(filtered)}"
            )

        return filtered

    def _build_relevance_anchors(
        self,
        *,
        user_request: str,
        prior_plan: dict[str, Any] | None,
    ) -> dict[str, Any]:
        anchor_texts: list[str] = []

        request_text = str(user_request or "").strip()
        if request_text:
            anchor_texts.append(request_text)

        previous = prior_plan if isinstance(prior_plan, dict) else {}
        for field in ("topic", "goal"):
            text = str(previous.get(field) or "").strip()
            if text:
                anchor_texts.append(text)

        for field in ("must_cover", "keywords"):
            values = previous.get(field)
            if not isinstance(values, list):
                continue
            for item in values:
                text = str(item).strip()
                if text:
                    anchor_texts.append(text)

        normalized_anchor_texts: list[str] = []
        tokens: set[str] = set()
        for text in anchor_texts:
            normalized = self._normalize_query(text)
            if not normalized:
                continue
            normalized_anchor_texts.append(normalized)
            tokens.update(self._extract_relevance_tokens(normalized))

        return {
            "texts": normalized_anchor_texts,
            "tokens": tokens,
        }

    def _is_relevant_to_request(
        self,
        text: str,
        *,
        anchor_texts: list[str],
        anchor_tokens: set[str],
    ) -> bool:
        normalized = self._normalize_query(text)
        if not normalized:
            return False

        for anchor in anchor_texts:
            if not anchor:
                continue
            if normalized in anchor or anchor in normalized:
                return True

        item_tokens = self._extract_relevance_tokens(normalized)
        if item_tokens and (item_tokens & anchor_tokens):
            return True

        for token in item_tokens:
            if len(token) < 4:
                continue
            for anchor in anchor_texts:
                if token in anchor:
                    return True

        return False

    def _extract_relevance_tokens(self, text: str) -> set[str]:
        if not text:
            return set()
        return {
            token
            for token in re.findall(r"[A-Za-z0-9가-힣]{2,}", text)
            if token
        }

    def _normalize_query(self, query: str) -> str:
        return " ".join(str(query).lower().split())

    def _build_llm_tooling(self, _user_request: str):
        return build_llm_tooling(
            self._tool_registry,
            stage_label="planning",
            allowed_tool_names=self.LLM_EXPOSED_TOOL_NAMES,
        )
