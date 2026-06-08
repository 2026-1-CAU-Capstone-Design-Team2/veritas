"""Regression tests for two memory-layer fixes.

1. MemoryAwareLLMClient proxies ``tokenize_count`` / ``stream_summary`` to raw,
   so wrapper-holding tools (document_summarize) get a live context-fit gate.
2. MemoryRuntime.configure_workspace waits for an in-flight bg flush before
   swapping the store, and the flush writes to the workspace it started on.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from core.memory.models import MemoryRole
from llm.memory_aware_llm import MemoryAwareLLMClient
from services.memory_tools_funcs.runtime import MemoryRuntime
from services.memory_tools_funcs.store import MemoryStore
from tools.document_summarize_tool.document_summarize_tool import DocumentSummarizeTool


class _FakeStore:
    def load_request(self) -> str:
        return "test request"


class _ProxyRawLLM:
    n_ctx = 1234
    model = "m"
    stream_summary = True
    max_parallel = 2

    def tokenize_count(self, text: str, *, timeout_sec: float = 0.5) -> int:
        _ = timeout_sec
        return len(text)


class WrapperProxyTests(unittest.TestCase):
    def test_tokenize_count_delegates_to_raw(self) -> None:
        wrapper = MemoryAwareLLMClient(raw_llm=_ProxyRawLLM(), memory_runtime=None)
        self.assertTrue(callable(getattr(wrapper, "tokenize_count", None)))
        self.assertEqual(wrapper.tokenize_count("hello"), 5)
        self.assertEqual(wrapper.tokenize_count("hi", timeout_sec=0.1), 2)

    def test_tokenize_count_none_when_raw_lacks_it(self) -> None:
        class _NoTok:
            n_ctx = 10
            model = "m"

        wrapper = MemoryAwareLLMClient(raw_llm=_NoTok(), memory_runtime=None)
        self.assertIsNone(wrapper.tokenize_count("x"))

    def test_stream_summary_reflects_raw(self) -> None:
        on = MemoryAwareLLMClient(raw_llm=_ProxyRawLLM(), memory_runtime=None)
        self.assertTrue(on.stream_summary)

        class _NoStream:
            n_ctx = 1
            model = "m"

        off = MemoryAwareLLMClient(raw_llm=_NoStream(), memory_runtime=None)
        self.assertFalse(off.stream_summary)


class SummarizeContextGateThroughWrapperTests(unittest.TestCase):
    def test_prompt_fits_context_is_live_through_wrapper(self) -> None:
        class _Raw:
            n_ctx = 2000
            model = "m"
            stream_summary = False

            def tokenize_count(self, text: str, *, timeout_sec: float = 0.5) -> int:
                _ = timeout_sec
                return len(text)

        wrapper = MemoryAwareLLMClient(raw_llm=_Raw(), memory_runtime=None)
        tool = DocumentSummarizeTool(schema={}, llm=wrapper, run_store_service=_FakeStore())

        # The token gate now runs through the wrapper: a small prompt fits, an
        # oversized one does not. Before the proxy fix tokenize_count was absent
        # on the wrapper and this method always returned True.
        self.assertTrue(tool._prompt_fits_context("system", "short"))
        self.assertFalse(tool._prompt_fits_context("system", "x" * 5000))


class _BlockingSummaryLLM:
    """Raw LLM whose summarizer ``ask`` blocks until released."""

    n_ctx = 8192
    model = "fake"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def ask(self, *_args, **_kwargs) -> str:
        self.entered.set()
        self.release.wait(timeout=10)
        return "COMPACTED_SUMMARY"


class FlushWorkspaceSwapTests(unittest.TestCase):
    def test_configure_workspace_waits_for_inflight_flush(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_a = Path(tmp) / "A"
            ws_b = Path(tmp) / "B"
            raw = _BlockingSummaryLLM()
            runtime = MemoryRuntime(
                raw_llm=raw, workspace_root=ws_a, max_context_tokens=8192
            )
            try:
                big = " ".join(["word"] * 300)
                for i in range(10):
                    runtime.queue.append_event(
                        role=MemoryRole.USER if i % 2 == 0 else MemoryRole.ASSISTANT,
                        content=f"row-{i} {big}",
                        source="test",
                    )
                self.assertGreater(runtime.queue.total_fifo_tokens(), 2400)

                runtime._maybe_launch_bg_flush(profile="chat")
                # flush is now blocked inside the summarizer call
                self.assertTrue(raw.entered.wait(timeout=5))
                self.assertTrue(runtime._flush_thread.is_alive())

                def _release_soon() -> None:
                    time.sleep(0.2)
                    raw.release.set()

                releaser = threading.Thread(target=_release_soon)
                releaser.start()

                # must block until the flush completes, then swap to B
                runtime.configure_workspace(ws_b)
                releaser.join()

                self.assertFalse(runtime._flush_thread.is_alive())

                # summary landed in A (the workspace the flush started on)
                store_a = MemoryStore(ws_a)
                self.addCleanup(store_a.close)
                self.assertEqual(store_a.load_latest_summary(), "COMPACTED_SUMMARY")

                # the new workspace B never received the old workspace's summary
                self.assertFalse(runtime.store.load_latest_summary())
            finally:
                raw.release.set()
                runtime.close()


if __name__ == "__main__":
    unittest.main()
