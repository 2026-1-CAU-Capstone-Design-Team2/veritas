"""Regression tests for ChatAgent._conversation_lock.

The streaming ask_*_iter generators hold the lock across ``yield``s, and
FastAPI's StreamingResponse drives ``__next__`` on different AnyIO threadpool
threads. The lock must therefore be a plain (non-reentrant) Lock that tolerates
cross-thread release, and re-entrant callers must use the lock-free cores.
"""

from __future__ import annotations

import threading
import unittest

from agent.chat_agent import ChatAgent


class _NoopLLM:
    n_ctx = 4096
    model = "fake"

    def call(self, _req):
        return "answer"

    def iter_call(self, _req):
        yield "x"


class _FakeRagService:
    CHUNKS = ["a", "b", "c"]

    def iter_answer(self, _question, **_kwargs):
        for chunk in self.CHUNKS:
            yield chunk

    def answer(self, _question, **_kwargs):
        return "full answer"


def _agent() -> ChatAgent:
    return ChatAgent(llm=_NoopLLM(), rag_service=_FakeRagService(), tool_registry=None)


class ConversationLockTests(unittest.TestCase):
    def test_lock_is_non_reentrant(self) -> None:
        # An RLock would let the same thread re-acquire; the conversation lock
        # must be a plain Lock so re-entrant callers are forced onto the
        # lock-free cores instead of silently re-acquiring.
        agent = _agent()
        self.assertTrue(agent._conversation_lock.acquire(blocking=False))
        self.assertFalse(agent._conversation_lock.acquire(blocking=False))
        agent._conversation_lock.release()

    def test_ask_rag_iter_survives_cross_thread_iteration(self) -> None:
        # Drive each __next__ on a fresh thread, mimicking AnyIO's threadpool
        # dispatching successive next() calls to different workers. An RLock
        # raises "cannot release un-acquired lock" when the final next() (which
        # exits the `with`) runs on a thread other than the first.
        agent = _agent()
        gen = agent.ask_rag_iter("q")
        chunks: list[str] = []
        errors: list[BaseException] = []
        done = object()

        def pull_one() -> None:
            try:
                chunks.append(next(gen))
            except StopIteration:
                chunks.append(done)  # type: ignore[arg-type]
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        for _ in range(5):
            t = threading.Thread(target=pull_one)
            t.start()
            t.join()
            if chunks and chunks[-1] is done:
                break

        self.assertEqual(errors, [])
        self.assertEqual([c for c in chunks if c is not done], ["a", "b", "c"])
        # The lock must be free afterward — an RLock would have stayed stuck
        # after the failed cross-thread release, freezing every later turn.
        self.assertTrue(agent._conversation_lock.acquire(timeout=1))
        agent._conversation_lock.release()

    def test_ask_auto_rag_command_does_not_deadlock(self) -> None:
        # /rag inside ask_auto holds the lock then enters the rag path; with a
        # non-reentrant Lock it MUST call the lock-free core, not re-acquire.
        agent = _agent()
        result: dict[str, str] = {}

        def run() -> None:
            result["answer"] = agent.ask_auto("/rag what is X", stream=False)

        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "ask_auto /rag self-deadlocked on the conversation lock")
        self.assertEqual(result.get("answer"), "full answer")

    def test_ask_auto_iter_rag_command_does_not_deadlock(self) -> None:
        agent = _agent()
        result: dict[str, list[str]] = {}

        def run() -> None:
            result["chunks"] = list(agent.ask_auto_iter("/rag q"))

        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "ask_auto_iter /rag self-deadlocked on the conversation lock")
        self.assertEqual(result.get("chunks"), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
