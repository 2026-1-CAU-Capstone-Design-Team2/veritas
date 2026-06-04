from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from llm.autosurvey_llm_factory import build_autosurvey_llm
from llm.openai_chat_llm import (
    DEFAULT_AUTOSURVEY_OPENAI_MODEL,
    OpenAIChatLLMClient,
)
from tools.loader import build_registry
from workflows import AutoSurveyConfig


class _FakeCompletions:
    def __init__(
        self,
        responses: list[str],
        *,
        fail_on_service_tier: bool = False,
    ) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.fail_on_service_tier = fail_on_service_tier

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_on_service_tier and "service_tier" in kwargs:
            raise RuntimeError("Unsupported service_tier for this model")
        if not self._responses:
            raise AssertionError("unexpected OpenAI call")
        content = self._responses.pop(0)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content, tool_calls=[]),
                )
            ]
        )


class _FakeOpenAIClient:
    def __init__(
        self,
        responses: list[str],
        *,
        fail_on_service_tier: bool = False,
    ) -> None:
        self.completions = _FakeCompletions(
            responses,
            fail_on_service_tier=fail_on_service_tier,
        )
        self.chat = SimpleNamespace(completions=self.completions)


class _FakeVectorStore:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs

    def close(self) -> None:
        pass


class _FakeScreenContextService:
    pass


class _FakeLLM:
    stream_summary = False

    def ask(self, *args, **kwargs) -> str:
        return "ok"

    def ask_json(self, *args, **kwargs) -> dict:
        return {"ok": True}

    def map_parallel(self, items, worker, **kwargs):
        return [worker(item) for item in items]

    def embed(self, _text):
        return [0.0]

    def embed_batch(self, texts):
        return [[0.0] for _ in texts]


def _fake_tool_module(class_name: str, tool_name: str):
    module = types.ModuleType(f"fake_{tool_name}")

    class FakeTool:
        def __init__(self, schema, **kwargs) -> None:
            self.schema = schema
            for key, value in kwargs.items():
                setattr(self, key, value)
            if "llm" in kwargs:
                self._llm = kwargs["llm"]

        @property
        def name(self) -> str:
            return tool_name

    FakeTool.__name__ = class_name
    setattr(module, class_name, FakeTool)
    return module


def _fake_loader_modules() -> dict[str, types.ModuleType]:
    fake_vector_module = types.ModuleType("storage.vector_store")
    fake_vector_module.VectorStore = _FakeVectorStore

    fake_screen_module = types.ModuleType("services.screen_tool_funcs")
    fake_screen_module.ScreenContextService = _FakeScreenContextService

    modules = {
        "storage.vector_store": fake_vector_module,
        "services.screen_tool_funcs": fake_screen_module,
        "tools.current_time_tool": _fake_tool_module("CurrentTimeTool", "current_time"),
        "tools.document_cleanup_tool": _fake_tool_module("DocumentCleanupTool", "document_cleanup"),
        "tools.document_summarize_tool": _fake_tool_module("DocumentSummarizeTool", "document_summarize"),
        "tools.fetch_webpage_tool": _fake_tool_module("FetchWebpageTool", "fetch_webpage"),
        "tools.final_report_tool": _fake_tool_module("FinalReportTool", "final_report"),
        "tools.query_plan_tool": _fake_tool_module("QueryPlanTool", "query_plan"),
        "tools.rag_tool": _fake_tool_module("RAGSearchTool", "rag_search"),
        "tools.screen_context_tool": _fake_tool_module("ScreenContextTool", "screen_context"),
        "tools.term_grounding_tool": _fake_tool_module("TermGroundingTool", "term_grounding"),
        "tools.verify_flow_planner_tool": _fake_tool_module("VerifyFlowPlannerTool", "verify_flow_planner"),
        "tools.web_search_tool": _fake_tool_module("WebSearchTool", "web_search"),
    }
    return modules


class OpenAIChatLLMClientTests(unittest.TestCase):
    def _client(self, responses: list[str]) -> tuple[OpenAIChatLLMClient, _FakeOpenAIClient]:
        fake = _FakeOpenAIClient(responses)
        client = OpenAIChatLLMClient(
            api_key="",
            client=fake,
            model="test-model",
            trace_latency=False,
        )
        return client, fake

    def test_ask_json_extracts_tolerant_json_without_llama_extra_body(self) -> None:
        samples = [
            '{"ok": true}',
            '```json\n{"ok": true}\n```',
            'Here is the result:\n{"ok": true,}\nDone.',
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                client, fake = self._client([sample])
                payload = client.ask_json("system", "user", max_retries=0)
                self.assertEqual(payload, {"ok": True})
                request = fake.completions.calls[-1]
                self.assertNotIn("extra_body", request)
                self.assertNotIn("/no_think", request["messages"][1]["content"])

    def test_gpt5_family_omits_unsupported_sampling_overrides(self) -> None:
        fake = _FakeOpenAIClient(['{"ok": true}'])
        client = OpenAIChatLLMClient(
            api_key="",
            client=fake,
            model="gpt-5-mini",
            trace_latency=False,
        )

        self.assertEqual(client.ask_json("system", "user", max_retries=0), {"ok": True})

        request = fake.completions.calls[-1]
        self.assertNotIn("temperature", request)
        self.assertNotIn("top_p", request)
        self.assertNotIn("presence_penalty", request)
        self.assertNotIn("frequency_penalty", request)

    def test_openai_service_tier_is_forwarded_when_configured(self) -> None:
        fake = _FakeOpenAIClient(["ok"])
        client = OpenAIChatLLMClient(
            api_key="",
            client=fake,
            model="gpt-5-mini",
            service_tier="priority",
            trace_latency=False,
        )

        self.assertEqual(client.ask("system", "user"), "ok")
        self.assertEqual(fake.completions.calls[-1]["service_tier"], "priority")

    def test_openai_service_tier_auto_is_not_sent(self) -> None:
        fake = _FakeOpenAIClient(["ok"])
        client = OpenAIChatLLMClient(
            api_key="",
            client=fake,
            model="gpt-5-mini",
            service_tier="auto",
            trace_latency=False,
        )

        self.assertEqual(client.ask("system", "user"), "ok")
        self.assertNotIn("service_tier", fake.completions.calls[-1])

    def test_openai_service_tier_rejection_falls_back_to_default(self) -> None:
        fake = _FakeOpenAIClient(["ok"], fail_on_service_tier=True)
        client = OpenAIChatLLMClient(
            api_key="",
            client=fake,
            model="gpt-5-mini",
            service_tier="priority",
            trace_latency=False,
        )

        self.assertEqual(client.ask("system", "user"), "ok")
        self.assertEqual(len(fake.completions.calls), 2)
        self.assertEqual(fake.completions.calls[0]["service_tier"], "priority")
        self.assertNotIn("service_tier", fake.completions.calls[1])

    def test_map_parallel_preserves_order_and_raises_input_order_error(self) -> None:
        client, _fake = self._client([])
        client.max_parallel = 3
        self.assertEqual(
            client.map_parallel([3, 1, 2], lambda value: value * 10),
            [30, 10, 20],
        )

        def worker(value: int) -> int:
            if value == 1:
                raise RuntimeError("first input error")
            if value == 2:
                raise ValueError("later input error")
            return value

        with self.assertRaisesRegex(RuntimeError, "first input error"):
            client.map_parallel([0, 1, 2], worker)

    def test_embedding_methods_fail_clearly(self) -> None:
        client, _fake = self._client([])
        with self.assertRaisesRegex(RuntimeError, "chat-only"):
            client.embed("text")
        with self.assertRaisesRegex(RuntimeError, "chat-only"):
            client.embed_batch(["text"])


class AutoSurveyLLMFactoryTests(unittest.TestCase):
    def test_local_provider_returns_default_client(self) -> None:
        default = object()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "llm.autosurvey_llm_factory._load_persisted_settings",
            return_value={},
        ):
            self.assertIs(build_autosurvey_llm(default), default)

    def test_openai_provider_requires_api_key(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"VERITAS_AUTOSURVEY_LLM_PROVIDER": "openai"},
            clear=True,
        ), mock.patch(
            "llm.autosurvey_llm_factory._load_persisted_settings",
            return_value={},
        ):
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                build_autosurvey_llm(object())

    def test_openai_provider_uses_model_defaults(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "VERITAS_AUTOSURVEY_LLM_PROVIDER": "openai",
                "OPENAI_API_KEY": "sk-test",
            },
            clear=True,
        ), mock.patch(
            "llm.autosurvey_llm_factory._load_persisted_settings",
            return_value={},
        ):
            client = build_autosurvey_llm(_FakeLLM())
        self.assertIsInstance(client, OpenAIChatLLMClient)
        self.assertEqual(client.model, DEFAULT_AUTOSURVEY_OPENAI_MODEL)
        self.assertEqual(client.n_ctx, 400_000)
        self.assertEqual(client.max_parallel, 1)

    def test_openai_provider_env_overrides_model_defaults(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "VERITAS_AUTOSURVEY_LLM_PROVIDER": "openai",
                "OPENAI_API_KEY": "sk-test",
                "VERITAS_AUTOSURVEY_OPENAI_MODEL": "gpt-5.5",
                "VERITAS_AUTOSURVEY_OPENAI_MAX_PARALLEL": "3",
                "VERITAS_AUTOSURVEY_OPENAI_SERVICE_TIER": "priority",
            },
            clear=True,
        ), mock.patch(
            "llm.autosurvey_llm_factory._load_persisted_settings",
            return_value={},
        ):
            client = build_autosurvey_llm(_FakeLLM())
        self.assertIsInstance(client, OpenAIChatLLMClient)
        self.assertEqual(client.model, "gpt-5.5")
        self.assertEqual(client.n_ctx, 1_050_000)
        self.assertEqual(client.max_parallel, 3)
        self.assertEqual(client.service_tier, "priority")

    def test_persisted_ui_key_enables_openai_when_env_provider_is_absent(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "llm.autosurvey_llm_factory._load_persisted_settings",
            return_value={
                "autosurveyOpenAI": {"provider": "openai", "apiKey": "sk-ui"},
                "llmParallel": 4,
            },
        ):
            client = build_autosurvey_llm(_FakeLLM())
        self.assertIsInstance(client, OpenAIChatLLMClient)
        self.assertEqual(client.model, DEFAULT_AUTOSURVEY_OPENAI_MODEL)
        self.assertEqual(client.max_parallel, 4)


class RegistryRoleInjectionTests(unittest.TestCase):
    def test_build_registry_splits_research_embedding_and_local_roles(self) -> None:
        local_llm = _FakeLLM()
        research_llm = _FakeLLM()
        embedding_llm = _FakeLLM()

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(sys.modules, _fake_loader_modules()):
                registry, _run_store, rag_service = build_registry(
                    llm=local_llm,
                    run_root=tmp,
                    autosurvey_llm=research_llm,
                    embedding_llm=embedding_llm,
                    enable_screen_context=False,
                )

        for name in (
            "term_grounding",
            "query_plan",
            "document_cleanup",
            "document_summarize",
            "final_report",
        ):
            self.assertIs(registry.get(name)._llm, research_llm)
        self.assertIs(registry.get("verify_flow_planner")._llm, local_llm)
        self.assertIs(rag_service.llm, embedding_llm)


class AutoSurveyConfigTests(unittest.TestCase):
    def test_fetch_cap_defaults_to_openai_research_limit(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"VERITAS_AUTOSURVEY_LLM_PROVIDER": "openai"},
            clear=True,
        ):
            self.assertEqual(AutoSurveyConfig.from_env().fetch_max_chars, 100_000)

    def test_fetch_cap_env_override_wins(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "VERITAS_AUTOSURVEY_LLM_PROVIDER": "openai",
                "VERITAS_AUTOSURVEY_FETCH_MAX_CHARS": "60000",
            },
            clear=True,
        ):
            self.assertEqual(AutoSurveyConfig.from_env().fetch_max_chars, 60_000)


if __name__ == "__main__":
    unittest.main()
