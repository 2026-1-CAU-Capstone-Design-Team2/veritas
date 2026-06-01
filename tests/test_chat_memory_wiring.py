from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.chat_agent import ChatAgent
from core.memory.request import CallRequest
from llm.memory_aware_llm import MemoryAwareLLMClient
from services.memory_tools_funcs.runtime import MemoryRuntime


class _MemoryLLM:
    """Records the CallRequest it receives; returns trivial output."""

    def __init__(self) -> None:
        self.calls: list[CallRequest] = []
        self.iter_calls: list[CallRequest] = []

    def call(self, req: CallRequest) -> str:
        self.calls.append(req)
        return "answer"

    def iter_call(self, req: CallRequest):
        self.iter_calls.append(req)
        yield "streamed"


class _StreamingRawLLM:
    """Mock raw LLM: iter_chat streams several chunks, chat() returns a single
    batched string. Mirrors how a real llama-server backs MemoryAwareLLMClient,
    so the tools->non-stream fallback path is actually exercised."""

    n_ctx = 4096
    model = "fake"
    _chat_host = "127.0.0.1"
    _chat_port = 8080

    CHUNKS = ["c1 ", "c2 ", "c3 ", "c4 ", "c5"]

    def __init__(self) -> None:
        self.iter_chat_called = False
        self.chat_called = False

    def chat(self, _messages, **_kwargs) -> str:
        self.chat_called = True
        return "".join(self.CHUNKS)

    def iter_chat(self, _messages, **_kwargs):
        self.iter_chat_called = True
        for chunk in self.CHUNKS:
            yield chunk

    def ask(self, *_args, **_kwargs) -> str:
        return "summary"

    def tokenize_count(self, _text: str, *, timeout_sec: float = 0.5) -> int:
        return 1


class ChatMemoryWiringTests(unittest.TestCase):
    def test_non_stream_final_answer_uses_call_request(self) -> None:
        # The agent no longer keeps a parallel chat_history list; conversation
        # turns are recorded inside MemoryAwareLLMClient.call via prepare/commit.
        # This test asserts the CallRequest shape — the contract the memory
        # wrapper consumes — without seeding any legacy history.
        llm = _MemoryLLM()
        agent = ChatAgent(llm=llm, rag_service=None, tool_registry=None)

        answer = agent._answer_from_current_turn(
            question="current question",
            tool_outputs=[],
            stream=False,
        )

        self.assertEqual(answer, "answer")
        self.assertEqual(len(llm.calls), 1)
        req = llm.calls[0]
        self.assertEqual(req.method_hint, "chat_final")
        self.assertTrue(req.use_history)
        # Chat answers must NOT enable self-edit tools: they force iter_call onto
        # the non-stream tool path and break streaming.
        self.assertFalse(req.enable_memory_tools)
        self.assertEqual(req.record_content, "current question")
        self.assertIn("current question", req.user_content)

    def test_stream_final_answer_uses_iter_call_request(self) -> None:
        llm = _MemoryLLM()
        agent = ChatAgent(llm=llm, rag_service=None, tool_registry=None)

        chunks = list(
            agent._stream_final_answer(
                question="current question",
                tool_outputs=[],
            )
        )

        self.assertEqual(chunks, ["streamed"])
        self.assertEqual(len(llm.iter_calls), 1)
        req = llm.iter_calls[0]
        self.assertEqual(req.method_hint, "chat_final")
        self.assertTrue(req.use_history)
        self.assertFalse(req.enable_memory_tools)
        self.assertEqual(req.record_content, "current question")
        self.assertIn("current question", req.user_content)

    def test_stream_final_answer_streams_chunks_through_real_wrapper(self) -> None:
        # Regression guard for the streaming regression: with the real
        # MemoryAwareLLMClient, the chat path must stream multiple chunks via
        # iter_chat, NOT collapse into one batched chunk via chat(stream=False).
        with tempfile.TemporaryDirectory() as tmp:
            raw = _StreamingRawLLM()
            runtime = MemoryRuntime(
                raw_llm=raw,
                workspace_root=Path(tmp),
                max_context_tokens=8192,
            )
            wrapper = MemoryAwareLLMClient(raw_llm=raw, memory_runtime=runtime)
            agent = ChatAgent(llm=wrapper, rag_service=None, tool_registry=None)

            chunks = list(
                agent._stream_final_answer(
                    question="hi",
                    tool_outputs=[],
                )
            )

            self.assertEqual(chunks, _StreamingRawLLM.CHUNKS)
            self.assertTrue(raw.iter_chat_called)
            self.assertFalse(raw.chat_called)
            runtime.close()


if __name__ == "__main__":
    unittest.main()
