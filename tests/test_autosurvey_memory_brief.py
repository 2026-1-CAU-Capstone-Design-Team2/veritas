from __future__ import annotations

import json
import unittest
from typing import Any

from services.autosurvey_memory_brief import build_autosurvey_memory_brief
from tools.autosurvey_tool.autosurvey_tool import AutoSurveyTool
from tools.query_plan_tool.query_plan_tool import QueryPlanTool
from tools.tool import ToolResult
from workflows.autosurvey_workflow import AutoSurveyWorkflow


class _FakeWorkingContext:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def records(self) -> list[dict[str, Any]]:
        return list(self._records)


class _FakeMemoryRuntime:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.working = _FakeWorkingContext(records)


class _FakeWorkflow:
    def __init__(self) -> None:
        self.max_docs = 10
        self.scout_docs = 3
        self.run_store_service = self
        self.calls: list[dict[str, Any]] = []

    def list_non_duplicate_records(self) -> list[Any]:
        return []

    def run_all(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "initial_plan": {"topic": kwargs.get("user_request")},
            "iterations": [],
            "final_result": {"final_path": None},
        }


class _FakeRunStore:
    def __init__(self) -> None:
        self.saved_plans: list[dict[str, Any]] = []
        self.plan_history: list[dict[str, Any]] = []

    def plan_exists(self) -> bool:
        return False

    def load_plan(self) -> dict[str, Any]:
        return {}

    def save_plan(self, plan: dict[str, Any]) -> None:
        self.saved_plans.append(dict(plan))

    def save_request(self, _request: str) -> None:
        pass

    def load_query_state(self) -> dict[str, Any]:
        return {"used_queries": []}

    def append_plan_history(self, **kwargs: Any) -> None:
        self.plan_history.append(kwargs)


class _CapturingQueryTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult(
            success=True,
            data={
                "topic": kwargs.get("user_request") or "topic",
                "goal": "goal",
                "search_queries": ["query"],
                "must_cover": [],
                "keywords": [],
            },
        )


class _FakeRegistry:
    def __init__(self, query_tool: _CapturingQueryTool) -> None:
        self.query_tool = query_tool

    def get(self, name: str) -> _CapturingQueryTool:
        if name != "query_plan":
            raise KeyError(name)
        return self.query_tool


class _CapturingLlm:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def ask_json(self, _system: str, user: str, **_kwargs: Any) -> dict[str, Any]:
        self.payloads.append(json.loads(user))
        return {
            "topic": "topic",
            "goal": "goal",
            "search_queries": ["query"],
            "must_cover": [],
            "keywords": [],
        }


class AutoSurveyMemoryBriefTests(unittest.TestCase):
    def test_build_brief_uses_only_allowed_working_categories(self) -> None:
        runtime = _FakeMemoryRuntime(
            [
                {"text": "Prefer Korean output", "tags": ["category:preference"]},
                {"text": "Use official documentation first", "tags": ["category:constraint"]},
                {"text": "VERITAS project", "tags": ["category:project"]},
                {"text": "Computer science student", "tags": ["category:profile"]},
                {"text": "Alice", "tags": ["category:name"]},
                {"text": "Raw recall should stay out", "tags": ["category:remember"]},
                {"text": "Untagged chat residue", "tags": []},
            ]
        )

        brief = build_autosurvey_memory_brief(runtime, "search request")

        self.assertIn("Prefer Korean output", brief)
        self.assertIn("Use official documentation first", brief)
        self.assertIn("VERITAS project", brief)
        self.assertIn("Computer science student", brief)
        self.assertNotIn("Alice", brief)
        self.assertNotIn("Raw recall should stay out", brief)
        self.assertNotIn("Untagged chat residue", brief)

    def test_autosurvey_tool_passes_brief_without_exposing_raw_text_in_result(self) -> None:
        workflow = _FakeWorkflow()
        runtime = _FakeMemoryRuntime(
            [{"text": "Prefer official docs", "tags": ["category:preference"]}]
        )
        tool = AutoSurveyTool(
            schema={},
            workflow=workflow,
            memory_runtime=runtime,
        )

        result = tool.run(request="Find embedding server setup")

        self.assertTrue(result.success)
        self.assertEqual(len(workflow.calls), 1)
        memory_brief = workflow.calls[0].get("memory_brief", "")
        self.assertIn("Prefer official docs", memory_brief)
        self.assertTrue(result.data["memory_brief_used"])
        self.assertNotIn("Prefer official docs", json.dumps(result.data, ensure_ascii=False))

    def test_workflow_passes_memory_brief_only_to_initial_plan(self) -> None:
        query_tool = _CapturingQueryTool()
        workflow = AutoSurveyWorkflow(
            _FakeRegistry(query_tool),
            _FakeRunStore(),
        )

        workflow.run_plan(
            "topic",
            mode="initial",
            memory_brief="Planning-only user context",
            save_request=False,
        )
        workflow.run_plan(
            "topic",
            mode="replan",
            prior_plan={"topic": "topic", "search_queries": []},
            memory_brief="Planning-only user context",
            save_request=False,
        )

        self.assertEqual(query_tool.calls[0]["memory_brief"], "Planning-only user context")
        self.assertEqual(query_tool.calls[1]["memory_brief"], "")

    def test_query_plan_input_includes_memory_brief_only_for_initial_plan(self) -> None:
        llm = _CapturingLlm()
        tool = QueryPlanTool(
            schema={},
            llm=llm,
            run_store_service=_FakeRunStore(),
        )

        initial = tool.run(
            "topic",
            mode="initial",
            memory_brief="Planning-only user context",
        )
        replan = tool.run(
            "topic",
            mode="replan",
            prior_plan={"topic": "topic", "search_queries": []},
            memory_brief="Planning-only user context",
        )

        self.assertTrue(initial.success)
        self.assertTrue(replan.success)
        self.assertEqual(llm.payloads[0]["memory_brief"], "Planning-only user context")
        self.assertNotIn("memory_brief", llm.payloads[1])


if __name__ == "__main__":
    unittest.main()
