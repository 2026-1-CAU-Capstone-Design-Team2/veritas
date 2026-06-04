from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from services.run_store_tool_funcs import RunStoreService
from tools.document_cleanup_tool import DocumentCleanupTool
from tools.loader import TOOLS_DIR, load_schema


_LOCAL_CLEANUP_RESPONSE = (
    "BOILERPLATE_PARAGRAPHS\n2\n\n===\n\n"
    "SUMMARY\nLocal cleanup summary.\n\n===\n\n"
    "KEYWORDS\n- alpha\n- beta\n\n===\n\n"
    "KEY_POINTS\n- local point one\n- local point two\n"
)


class FakeLocalLLM:
    """Mimics the local llama-server client — no 'openai' in module/class name."""

    def __init__(self) -> None:
        self.ask_calls = 0
        self.ask_json_calls = 0
        self.n_ctx = 50_000

    def ask(self, system_prompt, user_prompt, reasoning=False, **kwargs):
        self.ask_calls += 1
        return _LOCAL_CLEANUP_RESPONSE

    def ask_json(self, system_prompt, user_prompt, reasoning=False, **kwargs):
        self.ask_json_calls += 1
        return {}

    def map_parallel(self, items, worker, **kwargs):
        return [worker(item) for item in items]


class FakeOpenAIChatLLMClient:
    """Class name contains 'openai' → cleanup_mode 'auto' resolves to batch."""

    def __init__(self, *, fail_json: bool = False) -> None:
        self.ask_calls = 0
        self.ask_json_calls = 0
        self.ask_json_inputs: list[str] = []
        self.fail_json = fail_json
        self.n_ctx = 400_000

    def ask(self, system_prompt, user_prompt, reasoning=False, **kwargs):
        # Only reached when this client is forced onto the per_doc path. Return
        # a well-formed cleanup response (in the retention band) so the
        # per_doc retry logic stays out of the call-count assertions.
        self.ask_calls += 1
        return _LOCAL_CLEANUP_RESPONSE

    def ask_json(self, system_prompt, user_prompt, reasoning=False, **kwargs):
        self.ask_json_calls += 1
        self.ask_json_inputs.append(user_prompt)
        if self.fail_json:
            raise RuntimeError("simulated API failure")
        doc_ids = re.findall(r"=== doc_(\S+) ===", user_prompt)
        return {
            "documents": [
                {
                    "doc_id": doc_id,
                    "summary": f"OpenAI summary for doc {doc_id}.",
                    "keywords": ["hbm", "메모리"],
                    "key_points": [
                        f"Doc {doc_id} key point one.",
                        f"Doc {doc_id} key point two.",
                    ],
                }
                for doc_id in doc_ids
            ]
        }

    def map_parallel(self, items, worker, **kwargs):
        return [worker(item) for item in items]


def make_run_store(root: Path, doc_count: int) -> RunStoreService:
    """Build a workspace with `doc_count` kept records + raw_md bodies."""
    store = RunStoreService(root)
    records = []
    for index in range(1, doc_count + 1):
        doc_id = str(index)
        (store.paths.raw_md_dir / f"{doc_id}.md").write_text(
            f"# Document {doc_id}\n\n"
            f"Body paragraph about topic {doc_id} with real findings.\n\n"
            "Navigation menu | Footer | Cookie banner",
            encoding="utf-8",
        )
        records.append(
            {
                "doc_id": doc_id,
                "title": f"Doc {doc_id}",
                "url": f"https://example.com/{doc_id}",
                "final_url": f"https://example.com/{doc_id}",
                "domain": "example.com",
                "search_query": "test query",
            }
        )
    store.paths.index_path.write_text(
        json.dumps({"records": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    return store


def make_tool(store: RunStoreService, llm, cleanup_mode: str = "auto") -> DocumentCleanupTool:
    return DocumentCleanupTool(
        schema=load_schema(TOOLS_DIR / "document_cleanup_tool" / "tool_schema.json"),
        llm=llm,
        run_store_service=store,
        cleanup_mode=cleanup_mode,
    )


class CleanupModeResolutionTests(unittest.TestCase):
    def test_auto_resolves_per_doc_for_local_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            llm = FakeLocalLLM()
            tool = make_tool(store, llm, cleanup_mode="auto")

            result = tool.run(doc_ids=["1", "2"])

            self.assertTrue(result.success)
            # per_doc path: one ask() per document, no ask_json().
            self.assertEqual(llm.ask_calls, 2)
            self.assertEqual(llm.ask_json_calls, 0)

    def test_auto_resolves_batch_for_openai_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            llm = FakeOpenAIChatLLMClient()
            tool = make_tool(store, llm, cleanup_mode="auto")

            result = tool.run(doc_ids=["1", "2"])

            self.assertTrue(result.success)
            # batch path: one ask_json() for the whole cycle, no per-doc ask().
            self.assertEqual(llm.ask_json_calls, 1)
            self.assertEqual(llm.ask_calls, 0)

    def test_explicit_mode_overrides_auto_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            # OpenAI-typed client forced onto the per_doc path.
            llm = FakeOpenAIChatLLMClient()
            tool = make_tool(store, llm, cleanup_mode="per_doc")

            result = tool.run(doc_ids=["1", "2"])

            self.assertEqual(llm.ask_calls, 2)
            self.assertEqual(llm.ask_json_calls, 0)
            # Both docs must actually be cleaned — a per-doc exception that
            # halves the call count must not slip through this assertion.
            self.assertEqual(sorted(result.data["cleaned_doc_ids"]), ["1", "2"])
            self.assertEqual(result.data["failed_documents"], [])

        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            # Local-typed client forced onto the batch path.
            llm = FakeLocalLLM()
            tool = make_tool(store, llm, cleanup_mode="batch")

            result = tool.run(doc_ids=["1", "2"])

            self.assertEqual(llm.ask_json_calls, 1)
            self.assertEqual(llm.ask_calls, 0)
            self.assertEqual(sorted(result.data["cleaned_doc_ids"]), ["1", "2"])
            self.assertEqual(result.data["failed_documents"], [])


class BatchModeTests(unittest.TestCase):
    def test_one_llm_call_covers_all_cycle_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=5)
            llm = FakeOpenAIChatLLMClient()
            tool = make_tool(store, llm)

            result = tool.run(doc_ids=["1", "2", "3", "4", "5"])

            self.assertTrue(result.success)
            self.assertEqual(llm.ask_json_calls, 1)
            self.assertEqual(sorted(result.data["cleaned_doc_ids"]), ["1", "2", "3", "4", "5"])
            self.assertEqual(result.data["failed_documents"], [])
            self.assertEqual(result.data["fallback_documents"], [])

    def test_clean_md_is_raw_passthrough(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=1)
            tool = make_tool(store, FakeOpenAIChatLLMClient())

            tool.run(doc_ids=["1"])

            raw = (store.paths.raw_md_dir / "1.md").read_text(encoding="utf-8")
            clean = (store.paths.clean_md_dir / "1.md").read_text(encoding="utf-8")
            # Boilerplate removal is skipped on the batch path — the body is
            # copied as-is so RAG / verification / batch summary inputs exist.
            self.assertEqual(clean, raw)

    def test_doc_metadata_written_from_batch_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            tool = make_tool(store, FakeOpenAIChatLLMClient())

            tool.run(doc_ids=["1", "2"])

            doc_md = store.paths.summary_path_for(1).read_text(encoding="utf-8")
            self.assertIn("OpenAI summary for doc 1.", doc_md)
            self.assertIn("Doc 1 key point one.", doc_md)
            self.assertIn("## Key Points", doc_md)
            self.assertIn("## Keywords", doc_md)
            self.assertIn("hbm", doc_md)

    def test_metadata_call_failure_degrades_to_fallback_not_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            tool = make_tool(store, FakeOpenAIChatLLMClient(fail_json=True))

            result = tool.run(doc_ids=["1", "2"])

            # Still a success: clean_md exists, docs are usable downstream.
            self.assertTrue(result.success)
            self.assertEqual(sorted(result.data["cleaned_doc_ids"]), ["1", "2"])
            self.assertEqual(result.data["failed_documents"], [])
            self.assertEqual(len(result.data["fallback_documents"]), 2)
            # clean_md still written.
            self.assertTrue((store.paths.clean_md_dir / "1.md").exists())
            # doc_<id>.md falls back to the title-caption summary.
            doc_md = store.paths.summary_path_for(1).read_text(encoding="utf-8")
            self.assertIn("Doc 1", doc_md)

    def test_emits_same_progress_events_as_per_doc_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            tool = make_tool(store, FakeOpenAIChatLLMClient())
            events: list[tuple[str, dict]] = []

            tool.run(
                doc_ids=["1", "2"],
                progress_callback=lambda kind, **info: events.append((kind, info)),
            )

            kinds = [kind for kind, _ in events]
            self.assertEqual(kinds.count("doc_cleanup_started"), 2)
            self.assertEqual(kinds.count("doc_cleanup_done"), 2)
            done_events = [info for kind, info in events if kind == "doc_cleanup_done"]
            for info in done_events:
                # The workflow forwards summary_path to the frontend so the
                # document card can flip to its ready state.
                self.assertTrue(str(info.get("summary_path") or ""))
                self.assertFalse(info.get("used_fallback"))

    def test_existing_clean_md_skipped_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            store.write_clean_md("1", "previously cleaned body")
            llm = FakeOpenAIChatLLMClient()
            tool = make_tool(store, llm)

            result = tool.run(doc_ids=["1", "2"])

            self.assertEqual(result.data["skipped_existing_doc_ids"], ["1"])
            self.assertEqual(result.data["cleaned_doc_ids"], ["2"])
            # Skipped doc keeps its existing clean_md untouched.
            self.assertEqual(
                (store.paths.clean_md_dir / "1.md").read_text(encoding="utf-8"),
                "previously cleaned body",
            )

    def test_empty_raw_md_reported_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            (store.paths.raw_md_dir / "2.md").write_text("", encoding="utf-8")
            tool = make_tool(store, FakeOpenAIChatLLMClient())

            result = tool.run(doc_ids=["1", "2"])

            self.assertEqual(result.data["cleaned_doc_ids"], ["1"])
            self.assertEqual(len(result.data["failed_documents"]), 1)
            self.assertEqual(result.data["failed_documents"][0]["docId"], "2")

    def test_large_cycle_chunks_into_multiple_calls(self) -> None:
        doc_count = DocumentCleanupTool._BATCH_METADATA_MAX_DOCS + 3
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=doc_count)
            llm = FakeOpenAIChatLLMClient()
            tool = make_tool(store, llm)

            result = tool.run(doc_ids=[str(i) for i in range(1, doc_count + 1)])

            self.assertEqual(llm.ask_json_calls, 2)
            self.assertEqual(len(result.data["cleaned_doc_ids"]), doc_count)


class PerDocModeRegressionTests(unittest.TestCase):
    def test_per_doc_mode_behavior_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=3)
            llm = FakeLocalLLM()
            tool = make_tool(store, llm)

            result = tool.run(doc_ids=["1", "2", "3"])

            self.assertTrue(result.success)
            self.assertEqual(llm.ask_calls, 3)
            self.assertEqual(sorted(result.data["cleaned_doc_ids"]), ["1", "2", "3"])
            # per_doc mode actually strips the flagged boilerplate paragraph.
            clean = (store.paths.clean_md_dir / "1.md").read_text(encoding="utf-8")
            self.assertNotIn("Navigation menu", clean)
            self.assertIn("Body paragraph about topic 1", clean)
            # doc_<id>.md carries the cleanup-pass metadata.
            doc_md = store.paths.summary_path_for(1).read_text(encoding="utf-8")
            self.assertIn("Local cleanup summary.", doc_md)
            self.assertIn("local point one", doc_md)


if __name__ == "__main__":
    unittest.main()
