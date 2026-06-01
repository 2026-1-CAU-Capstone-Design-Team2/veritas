from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.memory.models import MemoryItem, MemoryRole, MemoryTier
from core.memory.request import CallConstraints, CallRequest
from llm.memory_aware_llm import MemoryAwareLLMClient
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.queue_manage import QueueManager
from services.memory_tools_funcs.runtime import MemoryRuntime
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


class _FakeRawLLM:
    def ask(self, *_args, **_kwargs) -> str:
        return "summary"


class _RefreshRawLLM(_FakeRawLLM):
    n_ctx = 4096
    model = "fake"

    def refresh_model_info(self) -> None:
        return None

    def tokenize_count(self, _text: str, *, timeout_sec: float = 0.5) -> int:
        _ = timeout_sec
        return 1


class _ToolRawLLM(_FakeRawLLM):
    n_ctx = 4096
    model = "fake"

    def __init__(self) -> None:
        self.chat_tools = None
        self.chat_tool_runner = None
        self.iter_chat_called = False

    def chat(self, _messages, **kwargs) -> str:
        self.chat_tools = kwargs.get("tools")
        self.chat_tool_runner = kwargs.get("tool_runner")
        return "tool answer"

    def iter_chat(self, *_args, **_kwargs):
        self.iter_chat_called = True
        yield "unexpected"

    def tokenize_count(self, _text: str, *, timeout_sec: float = 0.5) -> int:
        _ = timeout_sec
        return 1


class MemoryRuntimePr1Tests(unittest.TestCase):
    def test_store_and_runtime_do_not_create_memory_dir_until_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "api"
            store = MemoryStore(workspace)
            self.assertFalse(store.memory_dir.exists())

            runtime = MemoryRuntime(
                raw_llm=_FakeRawLLM(),
                workspace_root=workspace,
                max_context_tokens=8192,
            )
            self.assertFalse(runtime.store.memory_dir.exists())

            runtime.configure_workspace(Path(tmp) / "real_workspace")
            self.assertFalse(runtime.store.memory_dir.exists())
            runtime.close()

    def test_fifo_compaction_preserves_tail_and_late_appends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            self.addCleanup(store.close)
            recall = RecallStorage(store)
            queue = QueueManager(store, TokenCounter(), recall)

            for index in range(25):
                queue.append_event(
                    role=MemoryRole.USER,
                    content=f"message-{index}",
                    source="test",
                )

            evicted_rows = queue.select_evicted_rows(keep_tail=20)
            self.assertEqual([row["content"] for row in evicted_rows], [f"message-{i}" for i in range(5)])

            queue.append_event(
                role=MemoryRole.ASSISTANT,
                content="late-message",
                source="test",
            )

            queue.reset_fifo_with_summary("compressed", evicted_rows=evicted_rows)

            fifo_rows = queue.recent_rows(limit=100)
            self.assertEqual(len(fifo_rows), 21)
            self.assertEqual(fifo_rows[0]["content"], "message-5")
            self.assertEqual(fifo_rows[-1]["content"], "late-message")
            self.assertNotIn("message-0", [row["content"] for row in fifo_rows])

            self.assertEqual(store.load_latest_summary(), "compressed")

    def test_prepare_records_record_content_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MemoryRuntime(
                raw_llm=_FakeRawLLM(),
                workspace_root=Path(tmp),
                max_context_tokens=8192,
            )

            runtime.prepare(
                CallRequest(
                    task_instruction="system",
                    user_content="large prompt with tool output",
                    record_content="actual user message",
                    stream_label="chat",
                )
            )

            fifo_rows = runtime.queue.recent_rows(limit=1)
            self.assertEqual(fifo_rows[-1]["content"], "actual user message")
            runtime.close()

    def test_prepare_no_record_skips_invocation_and_turn_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MemoryRuntime(
                raw_llm=_FakeRawLLM(),
                workspace_root=Path(tmp),
                max_context_tokens=8192,
            )

            runtime.prepare(
                CallRequest(
                    task_instruction="system",
                    user_content="transient prompt",
                    constraints=CallConstraints(no_record=True),
                )
            )

            self.assertFalse(runtime.store.invocations_path.exists())
            self.assertFalse(runtime.store.db_path.exists())
            self.assertFalse(runtime.store.fifo_db_path.exists())
            self.assertFalse(runtime.store.fifo_path.exists())
            runtime.close()

    def test_pressure_counts_fifo_rows_beyond_legacy_tail_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            self.addCleanup(store.close)
            recall = RecallStorage(store)
            queue = QueueManager(store, TokenCounter(), recall)

            for index in range(501):
                queue.append_event(
                    role=MemoryRole.USER,
                    content=f"m-{index}",
                    source="test",
                )

            self.assertEqual(queue.total_fifo_tokens(), 501)
            self.assertEqual(queue.total_fifo_tokens(limit=500), 500)

    def test_fifo_token_total_is_cached_and_updates_on_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            self.addCleanup(store.close)
            recall = RecallStorage(store)
            queue = QueueManager(store, TokenCounter(), recall)

            for index in range(3):
                queue.append_event(role=MemoryRole.USER, content=f"m-{index}", source="test")
            self.assertEqual(queue.total_fifo_tokens(), 3)

            with patch.object(store, "read_jsonl", side_effect=AssertionError("full scan")):
                self.assertEqual(queue.total_fifo_tokens(), 3)
                queue.append_event(role=MemoryRole.USER, content="m-3", source="test")
                self.assertEqual(queue.total_fifo_tokens(), 4)

    def test_fifo_append_uses_sqlite_without_jsonl_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            self.addCleanup(store.close)
            recall = RecallStorage(store)
            queue = QueueManager(store, TokenCounter(), recall)

            queue.append_event(role=MemoryRole.USER, content="sqlite fifo", source="test")

            self.assertTrue(store.db_path.exists())
            self.assertFalse(store.fifo_db_path.exists())
            self.assertFalse(store.fifo_path.exists())
            self.assertEqual(queue.recent_rows(limit=1)[0]["content"], "sqlite fifo")

    def test_fifo_migrates_legacy_jsonl_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            self.addCleanup(store.close)
            row = store.item_to_dict(
                MemoryItem(
                    id="legacy-fifo",
                    tier=MemoryTier.FIFO,
                    role=MemoryRole.USER,
                    content="legacy fifo row",
                    source="test",
                    created_at="2026-05-29T00:00:00+00:00",
                    token_count=3,
                )
            )
            store.append_jsonl(store.fifo_path, row)
            self.assertFalse(store.db_path.exists())
            self.assertFalse(store.fifo_db_path.exists())

            queue = QueueManager(store, TokenCounter(), RecallStorage(store))
            queue.append_event(role=MemoryRole.ASSISTANT, content="new sqlite row", source="test")
            rows = queue.recent_rows(limit=2)

            self.assertTrue(store.db_path.exists())
            self.assertFalse(store.fifo_db_path.exists())
            self.assertEqual([row["content"] for row in rows], ["legacy fifo row", "new sqlite row"])

    def test_runtime_profile_policy_controls_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MemoryRuntime(
                raw_llm=_FakeRawLLM(),
                workspace_root=Path(tmp),
                max_context_tokens=8192,
            )
            try:
                runtime.recall.append(
                    MemoryItem(
                        id="recall-alpha",
                        tier=MemoryTier.RECALL,
                        role=MemoryRole.USER,
                        content="alpha profile memory",
                        source="test",
                        created_at="2026-05-29T00:00:00+00:00",
                        token_count=3,
                    )
                )
                chat_prepared = runtime.prepare(
                    CallRequest(
                        task_instruction="system",
                        user_content="what is alpha?",
                        record_content="alpha",
                        constraints=CallConstraints(no_record=True),
                        profile="chat",
                    )
                )
                rag_prepared = runtime.prepare(
                    CallRequest(
                        task_instruction="system",
                        user_content="what is alpha?",
                        record_content="alpha",
                        constraints=CallConstraints(no_record=True),
                        profile="rag",
                    )
                )

                chat_system = str(chat_prepared.messages[0]["content"])
                rag_system = str(rag_prepared.messages[0]["content"])
                # Both profiles pull recall — secondary context that the
                # answer LLM can use without violating RAG's grounding rule
                # (RAG_SYSTEM_PROMPT still enforces "answer only from documents"
                # for content questions).
                self.assertIn("Retrieved Recall Context", chat_system)
                self.assertIn("Retrieved Recall Context", rag_system)
            finally:
                runtime.close()

    def test_memory_aware_refresh_resets_token_counter_remote_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = _RefreshRawLLM()
            runtime = MemoryRuntime(
                raw_llm=raw,
                workspace_root=Path(tmp),
                max_context_tokens=8192,
            )
            runtime.token_counter._remote_disabled = True

            wrapper = MemoryAwareLLMClient(raw_llm=raw, memory_runtime=runtime)
            wrapper.refresh_model_info()

            self.assertFalse(runtime.token_counter._remote_disabled)
            self.assertEqual(runtime.max_context_tokens, 4096)
            runtime.close()

    def test_iter_call_with_memory_tools_uses_non_stream_tool_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = _ToolRawLLM()
            runtime = MemoryRuntime(
                raw_llm=raw,
                workspace_root=Path(tmp),
                max_context_tokens=8192,
            )
            wrapper = MemoryAwareLLMClient(raw_llm=raw, memory_runtime=runtime)

            chunks = list(
                wrapper.iter_call(
                    CallRequest(
                        task_instruction="system",
                        user_content="remember alpha",
                        stream_label="chat",
                        enable_memory_tools=True,
                    )
                )
            )

            self.assertEqual(chunks, ["tool answer"])
            self.assertFalse(raw.iter_chat_called)
            self.assertIsNotNone(raw.chat_tools)
            self.assertTrue(callable(raw.chat_tool_runner))
            tool_names = {
                str(tool.get("function", {}).get("name") or "")
                for tool in raw.chat_tools
            }
            self.assertIn("working_context_append", tool_names)
            self.assertIn("recall_search", tool_names)
            runtime.close()


if __name__ == "__main__":
    unittest.main()
