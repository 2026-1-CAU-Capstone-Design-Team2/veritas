"""RAG ("채팅") mode memory wiring.

Guards that the document-grounded RAG path is a full memory citizen: it routes
through the MemoryRuntime (call/iter_call), records its turns to FIFO/recall
(no_record=False), and uses profile="rag" so recall rides along as secondary
context (RAG_SYSTEM_PROMPT still keeps documents as primary evidence).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.memory.request import CallRequest
from llm.memory_aware_llm import MemoryAwareLLMClient
from services.memory_tools_funcs.runtime import MemoryRuntime
from services.rag_service import RAGService


class _RawLLM:
    """Minimal raw LLM: supports chat/iter_chat (memory path) + embed."""

    n_ctx = 8192
    model = "fake"
    _chat_host = "127.0.0.1"
    _chat_port = 8080

    CHUNKS = ["grounded ", "answer"]

    def __init__(self) -> None:
        self.chat_called = False
        self.iter_chat_called = False

    def chat(self, _messages, **_kwargs) -> str:
        self.chat_called = True
        return "".join(self.CHUNKS)

    def iter_chat(self, _messages, **_kwargs):
        self.iter_chat_called = True
        for chunk in self.CHUNKS:
            yield chunk

    def ask(self, *_a, **_k) -> str:
        return "summary"

    def tokenize_count(self, text, *, timeout_sec: float = 0.5) -> int:
        return max(1, len(str(text or "")) // 4)

    def embed(self, _text):
        return [0.0]

    def embed_batch(self, texts):
        return [[0.0] for _ in texts]


class _StubVectorStore:
    """Vector store that returns no documents (empty-context grounded path)."""

    def get_document_count(self) -> int:
        return 0

    def query(self, **_kwargs):
        return []

    def clear(self) -> None:
        pass

    def close(self) -> None:
        pass


class _CapturingMemoryLLM:
    """Captures the CallRequest routed through call/iter_call."""

    def __init__(self) -> None:
        self.calls: list[CallRequest] = []
        self.iter_calls: list[CallRequest] = []
        self.memory_runtime = None  # no runtime -> rewrite/history fall back safely

    def call(self, req: CallRequest) -> str:
        self.calls.append(req)
        return "answer"

    def iter_call(self, req: CallRequest):
        self.iter_calls.append(req)
        yield "answer"

    def embed(self, _text):
        return [0.0]

    def embed_batch(self, texts):
        return [[0.0] for _ in texts]


class RagCallRequestShapeTests(unittest.TestCase):
    def _rag(self) -> tuple[RAGService, _CapturingMemoryLLM]:
        llm = _CapturingMemoryLLM()
        rag = RAGService(llm=llm, vector_store=_StubVectorStore())
        return rag, llm

    def test_answer_routes_through_call_with_rag_profile(self) -> None:
        rag, llm = self._rag()
        out = rag.answer("alpha 질문", stream=False)
        self.assertEqual(out, "answer")
        self.assertEqual(len(llm.calls), 1)
        req = llm.calls[0]
        self.assertEqual(req.method_hint, "rag")
        self.assertEqual(req.profile, "rag")
        self.assertEqual(req.record_content, "alpha 질문")
        self.assertFalse(req.constraints.no_record)
        self.assertTrue(req.constraints.inject_memory_context)
        self.assertFalse(req.enable_memory_tools)

    def test_iter_answer_routes_through_iter_call(self) -> None:
        rag, llm = self._rag()
        chunks = list(rag.iter_answer("beta 질문"))
        self.assertEqual(chunks, ["answer"])
        self.assertEqual(len(llm.iter_calls), 1)
        self.assertEqual(llm.iter_calls[0].profile, "rag")
        self.assertEqual(llm.iter_calls[0].record_content, "beta 질문")


class RagMemoryEndToEndTests(unittest.TestCase):
    def test_rag_answer_records_turn_and_creates_memory_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            runtime = MemoryRuntime(
                raw_llm=_RawLLM(), workspace_root=ws, max_context_tokens=8192
            )
            wrapper = MemoryAwareLLMClient(raw_llm=_RawLLM(), memory_runtime=runtime)
            rag = RAGService(llm=wrapper, vector_store=_StubVectorStore())

            # Empty index -> grounded refusal path, but still a recorded memory turn.
            answer = rag.answer("문서에 없는 질문", stream=False)
            self.assertTrue(answer)

            db = ws / "memory" / "memory.sqlite3"
            self.assertTrue(db.exists())
            # USER + ASSISTANT recorded (no_record=False).
            self.assertEqual(runtime.queue.fifo.count(), 2)
            self.assertEqual(len(runtime.recall.tail(limit=10)), 2)
            runtime.close()

    def test_rag_iter_answer_streams_chunks_through_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = _RawLLM()
            runtime = MemoryRuntime(
                raw_llm=raw, workspace_root=Path(tmp) / "ws", max_context_tokens=8192
            )
            wrapper = MemoryAwareLLMClient(raw_llm=raw, memory_runtime=runtime)
            rag = RAGService(llm=wrapper, vector_store=_StubVectorStore())

            stream_raw = wrapper.raw  # same instance used inside iter_call
            chunks = list(rag.iter_answer("스트리밍 질문"))

            self.assertEqual(chunks, _RawLLM.CHUNKS)
            self.assertTrue(stream_raw.iter_chat_called)
            self.assertFalse(stream_raw.chat_called)
            runtime.close()


if __name__ == "__main__":
    unittest.main()
