from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from core.memory.request import CallRequest
from services.memory_tools_funcs.main_context.heuristic_memory import extract_explicit_facts
from services.memory_tools_funcs.main_context.working_context import WorkingContextManager
from services.memory_tools_funcs.runtime import MemoryRuntime
from services.memory_tools_funcs.store import MemoryStore


class _WordCounter:
    def count(self, text: str) -> int:
        return len(str(text or "").split())


class _FakeRawLLM:
    def ask(self, *_args, **_kwargs) -> str:
        return "summary"


class WorkingContextTests(unittest.TestCase):
    def test_append_fact_deduplicates_and_enforces_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            working = WorkingContextManager(MemoryStore(Path(tmp)), _WordCounter())

            self.assertTrue(working.append_fact("alpha one", max_tokens=6))
            self.assertFalse(working.append_fact("alpha one", max_tokens=6))
            self.assertTrue(working.append_fact("beta two", max_tokens=6))
            self.assertTrue(working.append_fact("gamma three", max_tokens=6))

            text = working.load()
            self.assertNotIn("alpha one", text)
            self.assertIn("beta two", text)
            self.assertIn("gamma three", text)
            self.assertLessEqual(_WordCounter().count(text), 6)

            records = working.records()
            self.assertEqual([row["text"] for row in records], ["beta two", "gamma three"])
            self.assertTrue(all(row["source"] == "heuristic" for row in records))

    def test_legacy_working_context_is_loaded_as_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            store.working_path.parent.mkdir(parents=True, exist_ok=True)
            store.working_path.write_text(
                json.dumps({"content": "- alpha one\n- beta two"}),
                encoding="utf-8",
            )
            working = WorkingContextManager(store, _WordCounter())

            self.assertEqual(
                [row["text"] for row in working.records()],
                ["alpha one", "beta two"],
            )
            self.assertIn("alpha one", working.load())

            working.append_fact("gamma three", source="tool", tags=["tool"])
            records = working.records()

            self.assertFalse(store.working_path.exists())
            self.assertTrue(Path(f"{store.working_path}.migrated").exists())
            self.assertEqual(records[-1]["source"], "tool")
            self.assertIn("tool", records[-1]["tags"])

    def test_explicit_fact_extractor_is_conservative(self) -> None:
        self.assertEqual(extract_explicit_facts("what is alpha memory?"), [])
        self.assertEqual(
            extract_explicit_facts("기억해줘: 내 프로젝트는 alpha memory layer야."),
            ["내 프로젝트는 alpha memory layer야"],
        )
        self.assertEqual(
            extract_explicit_facts("my name is Dana. what do you remember?"),
            ["Dana"],
        )

    def test_prepare_heuristically_appends_explicit_user_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MemoryRuntime(
                raw_llm=_FakeRawLLM(),
                workspace_root=Path(tmp),
                max_context_tokens=8192,
            )

            runtime.prepare(
                CallRequest(
                    task_instruction="system",
                    user_content="large prompt",
                    record_content="기억해줘: 내 프로젝트는 alpha memory layer야.",
                    stream_label="chat",
                )
            )

            self.assertIn("alpha memory layer", runtime.working.load())
            runtime.close()


if __name__ == "__main__":
    unittest.main()
