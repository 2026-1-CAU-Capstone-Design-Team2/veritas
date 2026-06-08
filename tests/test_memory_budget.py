from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.memory.budget import MemoryBudget
from core.memory.models import MemoryRole
from core.memory.request import CallConstraints, CallRequest
from llm.llama_server_llm import LLMClient
from services.memory_tools_funcs.context_builder import build_messages
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.queue_manage import QueueManager
from services.memory_tools_funcs.main_context.working_context import WorkingContextManager
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


class _WordCounter:
    def count(self, text: str) -> int:
        return len(str(text or "").split())


class _Response:
    status_code = 200

    def json(self):
        return {"tokens": [1, 2, 3]}


class _Raw:
    def __init__(self) -> None:
        self.calls = 0
        self.enabled = True

    def tokenize_count(self, _text: str, *, timeout_sec: float = 0.5) -> int | None:
        self.calls += 1
        _ = timeout_sec
        return 3 if self.enabled else None


class MemoryBudgetTests(unittest.TestCase):
    def test_token_counter_uses_remote_tokenize_and_caches(self) -> None:
        raw = _Raw()
        counter = TokenCounter(raw)
        self.assertEqual(counter.count("hello world"), 3)
        self.assertEqual(counter.count("hello world"), 3)

        self.assertEqual(raw.calls, 1)

    def test_token_counter_can_retry_remote_after_reset(self) -> None:
        raw = _Raw()
        raw.enabled = False
        counter = TokenCounter(raw)
        self.assertEqual(counter.count("hello world"), 2)
        self.assertTrue(counter._remote_disabled)

        raw.enabled = True
        counter.reset_remote()

        self.assertEqual(counter.count("hello world"), 3)
        self.assertFalse(counter._remote_disabled)

    def test_llm_client_tokenize_count_uses_public_endpoint(self) -> None:
        client = object.__new__(LLMClient)
        client._chat_host = "127.0.0.1"
        client._chat_port = 8080

        with patch("httpx.post", return_value=_Response()) as post:
            self.assertEqual(client.tokenize_count("hello world"), 3)

        self.assertEqual(post.call_count, 1)
        self.assertIn("/tokenize", str(post.call_args.args[0]))

    def test_llm_client_tokenize_count_returns_none_when_endpoint_fails(self) -> None:
        client = object.__new__(LLMClient)
        client._chat_host = "127.0.0.1"
        client._chat_port = 8080

        with patch("httpx.post", side_effect=RuntimeError("down")) as post:
            self.assertIsNone(client.tokenize_count("hello world"))

        self.assertEqual(post.call_count, 2)

    def test_build_messages_applies_working_summary_and_fifo_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            counter = _WordCounter()
            recall = RecallStorage(store)
            queue = QueueManager(store, counter, recall)
            working = WorkingContextManager(store, counter)
            working.save("one two three four five")
            store.append_jsonl(store.summaries_path, {"summary": "sum one two three four"})
            queue.append_event(role=MemoryRole.USER, content="older alpha beta gamma delta", source="test")
            queue.append_event(role=MemoryRole.USER, content="new one two", source="test")

            messages = build_messages(
                req=CallRequest(task_instruction="system", user_content="question"),
                budget=MemoryBudget(
                    max_context_tokens=1024,
                    reserve_output_tokens=0,
                    working_context_tokens=3,
                    fifo_tokens=4,
                ),
                store=store,
                working=working,
                queue=queue,
            )

        system = str(messages[0]["content"])
        self.assertIn("one two three", system)
        self.assertNotIn("four five", system)
        self.assertIn("sum one two three", system)
        self.assertNotIn("sum one two three four", system)
        self.assertEqual([m["content"] for m in messages[1:-1]], ["new one two"])

    def test_fifo_floor_survives_working_summary_pressure(self) -> None:
        # A large working context + summary that would otherwise consume the
        # whole prompt budget must NOT starve the most recent conversation: the
        # FIFO floor reserves a slice. Without the floor, FIFO history would be
        # 0 tokens here and the recent turn would be dropped.
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            counter = _WordCounter()
            recall = RecallStorage(store)
            queue = QueueManager(store, counter, recall)
            working = WorkingContextManager(store, counter)
            working.save(" ".join(["fact"] * 500))
            store.append_jsonl(store.summaries_path, {"summary": " ".join(["sum"] * 500)})
            queue.append_event(
                role=MemoryRole.USER, content="recent marker turn", source="test"
            )

            messages = build_messages(
                req=CallRequest(task_instruction="system", user_content="question"),
                budget=MemoryBudget(max_context_tokens=512, reserve_output_tokens=0),
                store=store,
                working=working,
                queue=queue,
            )

        history = [str(m["content"]) for m in messages[1:-1]]
        self.assertIn("recent marker turn", history)

    def test_inject_memory_context_false_skips_all_memory_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            counter = _WordCounter()
            recall = RecallStorage(store)
            queue = QueueManager(store, counter, recall)
            working = WorkingContextManager(store, counter)
            working.save("remembered fact")
            store.append_jsonl(store.summaries_path, {"summary": "old summary"})
            queue.append_event(role=MemoryRole.USER, content="old turn", source="test")

            messages = build_messages(
                req=CallRequest(
                    task_instruction="system",
                    user_content="question",
                    constraints=CallConstraints(inject_memory_context=False),
                ),
                budget=MemoryBudget(max_context_tokens=1024, reserve_output_tokens=0),
                store=store,
                working=working,
                queue=queue,
            )

        self.assertEqual(len(messages), 2)
        self.assertNotIn("remembered fact", str(messages[0]["content"]))
        self.assertNotIn("old summary", str(messages[0]["content"]))


if __name__ == "__main__":
    unittest.main()
