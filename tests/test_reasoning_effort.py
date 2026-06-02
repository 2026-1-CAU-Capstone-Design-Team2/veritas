from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace

from llm.llama_server_llm import LLMClient
from llm.openai_chat_llm import OpenAIChatLLMClient


class _FakeCompletions:
    def __init__(self, responses: list[str], *, fail_on_reasoning_effort: bool = False) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.fail_on_reasoning_effort = fail_on_reasoning_effort

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_on_reasoning_effort and "reasoning_effort" in kwargs:
            raise RuntimeError("Unsupported parameter: 'reasoning_effort'")
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
    def __init__(self, responses: list[str], **fake_kwargs) -> None:
        self.completions = _FakeCompletions(responses, **fake_kwargs)
        self.chat = SimpleNamespace(completions=self.completions)


def make_client(
    model: str = "gpt-5-mini",
    responses: tuple[str, ...] = ("ok",),
    **fake_kwargs,
) -> tuple[OpenAIChatLLMClient, _FakeOpenAIClient]:
    fake = _FakeOpenAIClient(list(responses), **fake_kwargs)
    client = OpenAIChatLLMClient(
        api_key="",
        client=fake,
        model=model,
        trace_latency=False,
    )
    return client, fake


class ReasoningEffortRequestTests(unittest.TestCase):
    def test_effort_sent_for_gpt5_family(self) -> None:
        client, fake = make_client(model="gpt-5-mini")
        client.ask("system", "user", reasoning_effort="low")
        self.assertEqual(fake.completions.calls[-1]["reasoning_effort"], "low")

    def test_all_valid_efforts_accepted(self) -> None:
        for effort in ("minimal", "low", "medium", "high"):
            with self.subTest(effort=effort):
                client, fake = make_client(model="gpt-5-mini")
                client.ask("system", "user", reasoning_effort=effort)
                self.assertEqual(fake.completions.calls[-1]["reasoning_effort"], effort)

    def test_effort_not_sent_for_non_reasoning_models(self) -> None:
        # gpt-4o family rejects reasoning_effort — it must never be sent.
        client, fake = make_client(model="gpt-4o-mini")
        client.ask("system", "user", reasoning_effort="low")
        self.assertNotIn("reasoning_effort", fake.completions.calls[-1])

    def test_effort_not_sent_when_omitted(self) -> None:
        client, fake = make_client(model="gpt-5-mini")
        client.ask("system", "user")
        self.assertNotIn("reasoning_effort", fake.completions.calls[-1])

    def test_invalid_effort_dropped_instead_of_crashing(self) -> None:
        client, fake = make_client(model="gpt-5-mini")
        text = client.ask("system", "user", reasoning_effort="ultra")
        self.assertEqual(text, "ok")
        self.assertNotIn("reasoning_effort", fake.completions.calls[-1])

    def test_rejected_effort_falls_back_without_it(self) -> None:
        # If the API rejects reasoning_effort (model/account mismatch), the
        # call retries without it instead of failing the survey.
        client, fake = make_client(
            model="gpt-5-mini",
            responses=("ok",),
            fail_on_reasoning_effort=True,
        )
        text = client.ask("system", "user", reasoning_effort="low")
        self.assertEqual(text, "ok")
        self.assertEqual(len(fake.completions.calls), 2)
        self.assertIn("reasoning_effort", fake.completions.calls[0])
        self.assertNotIn("reasoning_effort", fake.completions.calls[1])

    def test_ask_json_passes_effort_through(self) -> None:
        client, fake = make_client(model="gpt-5-mini", responses=('{"ok": true}',))
        payload = client.ask_json("system", "user", max_retries=0, reasoning_effort="low")
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(fake.completions.calls[-1]["reasoning_effort"], "low")

    def test_effort_coexists_with_service_tier(self) -> None:
        fake = _FakeOpenAIClient(["ok"])
        client = OpenAIChatLLMClient(
            api_key="",
            client=fake,
            model="gpt-5-mini",
            service_tier="priority",
            trace_latency=False,
        )
        client.ask("system", "user", reasoning_effort="low")
        request = fake.completions.calls[-1]
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertEqual(request["service_tier"], "priority")


class LocalClientCompatibilityTests(unittest.TestCase):
    def test_local_client_accepts_reasoning_effort_kwarg(self) -> None:
        # AutoSurvey tools pass reasoning_effort unconditionally; the local
        # llama-server client must accept (and ignore) it so the local
        # provider path keeps working.
        self.assertIn("reasoning_effort", inspect.signature(LLMClient.ask).parameters)
        self.assertIn("reasoning_effort", inspect.signature(LLMClient.ask_json).parameters)


class ToolEffortWiringTests(unittest.TestCase):
    """The per-call-type efforts the AutoSurvey tools request.

    low    — extraction-style calls: term grounding, document cleanup
             (both batch metadata and per-doc boilerplate flagging)
    medium — synthesis-style calls: per-doc summaries, batch summaries,
             final report
    """

    def test_cleanup_batch_metadata_requests_low_effort(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from services.run_store_tool_funcs import RunStoreService
        from tools.document_cleanup_tool import DocumentCleanupTool
        from tools.loader import TOOLS_DIR, load_schema

        class RecordingOpenAILLM:
            def __init__(self) -> None:
                self.ask_json_kwargs: list[dict] = []
                self.n_ctx = 400_000

            def ask_json(self, system, user, *args, **kwargs):
                self.ask_json_kwargs.append(kwargs)
                return {"documents": []}

            def map_parallel(self, items, worker, **kwargs):
                return [worker(item) for item in items]

        with tempfile.TemporaryDirectory() as tmp:
            store = RunStoreService(Path(tmp))
            (store.paths.raw_md_dir / "1.md").write_text("# Doc\n\nBody text.", encoding="utf-8")
            store.paths.index_path.write_text(
                json.dumps({"records": [{"doc_id": "1", "title": "Doc 1"}]}),
                encoding="utf-8",
            )
            llm = RecordingOpenAILLM()
            tool = DocumentCleanupTool(
                schema=load_schema(TOOLS_DIR / "document_cleanup_tool" / "tool_schema.json"),
                llm=llm,
                run_store_service=store,
                cleanup_mode="batch",
            )

            tool.run(doc_ids=["1"])

            self.assertEqual(llm.ask_json_kwargs[-1].get("reasoning_effort"), "low")

    def test_final_report_requests_medium_effort(self) -> None:
        from tools.final_report_tool import FinalReportTool
        from tools.loader import TOOLS_DIR, load_schema

        class RecordingLLM:
            def __init__(self) -> None:
                self.ask_kwargs: list[dict] = []

            def ask(self, system, user, *args, **kwargs):
                self.ask_kwargs.append(kwargs)
                return "# Final Report"

        class FakeRunStore:
            final_path = "final.md"

            def load_request(self):
                return "request"

            def load_plan(self):
                return {}

            def load_records(self):
                return []

            def load_all_batch_summaries(self):
                return ["batch 1"]

            def save_final_report(self, content):
                self.saved = content

        llm = RecordingLLM()
        tool = FinalReportTool(
            schema=load_schema(TOOLS_DIR / "final_report_tool" / "tool_schema.json"),
            llm=llm,
            run_store_service=FakeRunStore(),
        )

        result = tool.run(user_request="request")

        self.assertTrue(result.success)
        self.assertEqual(llm.ask_kwargs[-1].get("reasoning_effort"), "medium")


if __name__ == "__main__":
    unittest.main()
