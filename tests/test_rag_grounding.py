"""Regression tests for workspace-scoped RAG grounding.

Guards against the regression where the frontend "RAG" chat mode answered
questions about *other* workspaces' topics from the model's general knowledge
instead of saying the active workspace's indexed materials don't cover them.

The fix routes RAG chat through the strict ``RAGService`` grounding path
(``RAG_SYSTEM_PROMPT``) rather than the permissive rag_search + tool-synthesis
path. These tests assert the wiring and the prompt selection without needing a
live LLM or embedding server.
"""
from __future__ import annotations

import unittest

from agent.chat_agent import ChatAgent
from core.prompts import (
    RAG_EMPTY_CONTEXT_PROMPT_TEMPLATE,
    RAG_SYSTEM_PROMPT,
    RAG_USER_PROMPT_TEMPLATE,
    TOOL_CHAT_SYSTEM_PROMPT,
)
from services.rag_service import RAGService


class FakeLLM:
    """Records the (method, system_prompt, user_prompt) of each generation."""

    def __init__(self, reply: str = "FAKE_ANSWER") -> None:
        self.reply = reply
        self.calls: list[tuple[str, str, str]] = []

    def ask(self, system_prompt, user_prompt, reasoning=False, *, stream=False,
            stream_label="", **_kw) -> str:
        self.calls.append(("ask", system_prompt, user_prompt))
        return self.reply

    def iter_ask(self, system_prompt, user_prompt, reasoning=False, *,
                 stream_label="", **_kw):
        self.calls.append(("iter_ask", system_prompt, user_prompt))
        yield self.reply

    def embed(self, _text):
        return [0.0]

    def embed_batch(self, texts):
        return [[0.0] for _ in texts]


# A retrieved World_Model chunk — substantive, but about a *different* topic than
# a 3D Gaussian Splatting question. Long enough to pass _has_substantive_context.
WORLD_MODEL_DOC = {
    "doc_id": "wm1",
    "content": (
        "World models learn a predictive latent dynamics model of an environment "
        "so an agent can plan by imagining rollouts. This document discusses "
        "reinforcement learning with learned world models and latent imagination."
    ),
    "metadata": {"parent_doc_id": "wm1"},
    "distance": 0.11,
}


class RagServiceGroundingTests(unittest.TestCase):
    def _service(self) -> RAGService:
        # vector_store is unused because retrieve() is stubbed per-test.
        return RAGService(llm=FakeLLM(), vector_store=object())

    def test_system_prompt_forbids_general_knowledge_fallback(self) -> None:
        # The grounding contract must explicitly forbid answering off-topic
        # questions from general model knowledge.
        lowered = RAG_SYSTEM_PROMPT.lower()
        self.assertIn("only", lowered)
        self.assertIn("general", lowered)  # "general model knowledge"
        self.assertTrue(
            "different topic" in lowered or "do not contain" in lowered,
            "RAG_SYSTEM_PROMPT should tell the model to refuse off-topic questions",
        )

    def test_system_prompt_carves_out_meta_conversation_questions(self) -> None:
        # Without this carve-out, asking "여태까지 나랑 나눈 대화를 요약해볼래?"
        # in RAG mode gets the off-corpus refusal because the strict grounding
        # contract treats anything not in the documents as "no material".
        lowered = RAG_SYSTEM_PROMPT.lower()
        self.assertIn("meta-conversation", lowered)
        # Points the model at the actual sources for meta-questions: the chat
        # messages preceding this turn AND the memory blocks in the system
        # instruction (working context / FIFO summary / recall).
        self.assertIn("chat messages", lowered)
        self.assertIn("working context", lowered)
        self.assertIn("recent conversation summary", lowered)
        self.assertIn("retrieved recall context", lowered)
        # The off-topic refusal must be explicitly suppressed for these.
        self.assertTrue(
            "off-topic refusal must not" in lowered
            or "does not apply" in lowered,
            "RAG_SYSTEM_PROMPT should suppress off-corpus refusal for meta-questions",
        )

    def test_empty_retrieval_uses_refusal_prompt(self) -> None:
        rag = self._service()
        rag.retrieve = lambda q, use_history=True: []  # nothing relevant in workspace
        list(rag.iter_answer("3D Gaussian Splatting이 뭐야?"))
        method, system, user = rag.llm.calls[-1]
        self.assertEqual(method, "iter_ask")
        self.assertEqual(system, RAG_SYSTEM_PROMPT)
        # The empty-context template tells the model to say it lacks information.
        self.assertIn("don't have enough information", user)
        self.assertNotIn("World models", user)  # no fabricated context

    def test_offtopic_substantive_docs_still_grounded_not_tool_synthesis(self) -> None:
        # Even when nearest-neighbor docs are returned (off-topic World_Model
        # chunks for a GS question), the strict RAG system prompt is used so the
        # model judges relevance and refuses — never the permissive TOOL_CHAT one.
        rag = self._service()
        rag.retrieve = lambda q, use_history=True: [WORLD_MODEL_DOC]
        rag.answer("3D Gaussian Splatting의 렌더링 원리는?")
        method, system, user = rag.llm.calls[-1]
        self.assertEqual(system, RAG_SYSTEM_PROMPT)
        self.assertNotEqual(system, TOOL_CHAT_SYSTEM_PROMPT)
        self.assertIn("DOCUMENTS:", user)  # RAG_USER_PROMPT_TEMPLATE shape
        self.assertIn("World models", user)  # retrieved context is shown for judgment

    def test_doc_context_is_subordinate_not_evidence(self) -> None:
        rag = self._service()
        rag.retrieve = lambda q, use_history=True: []
        prompt = rag._grounded_user_prompt(
            "질문", use_history=False, doc_context="사용자가 쓰던 초안 본문"
        )
        self.assertIn("사용자가 쓰던 초안 본문", prompt)
        self.assertIn("context only", prompt)  # framed as non-evidence


class RecordingRag:
    """Records which RAGService entry point the chat agent used."""

    def __init__(self) -> None:
        self.iter_called = 0
        self.answer_called = 0

    def iter_answer(self, question, *, use_history=True, doc_context=""):
        self.iter_called += 1
        yield "GROUNDED"

    def answer(self, question, stream=False, use_history=True, *, doc_context=""):
        self.answer_called += 1
        return "GROUNDED"

    def get_document_count(self) -> int:
        return 5


class RecordingRegistry:
    """Fails the test loudly if the permissive rag_search tool path is taken."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def has(self, _name: str) -> bool:
        return False

    def call(self, name: str, **_kw):
        self.calls.append(name)
        raise AssertionError(f"unexpected tool call: {name}")


class ChatAgentRagRoutingTests(unittest.TestCase):
    def _agent(self) -> tuple[ChatAgent, RecordingRag, RecordingRegistry]:
        rag = RecordingRag()
        registry = RecordingRegistry()
        agent = ChatAgent(llm=FakeLLM(), rag_service=rag, tool_registry=registry)
        return agent, rag, registry

    def test_ask_rag_iter_uses_strict_iter_answer(self) -> None:
        agent, rag, registry = self._agent()
        chunks = list(agent.ask_rag_iter("질문"))
        self.assertEqual(chunks, ["GROUNDED"])
        self.assertEqual(rag.iter_called, 1)
        self.assertEqual(registry.calls, [])  # no rag_search tool synthesis

    def test_slash_rag_command_routes_to_strict_path(self) -> None:
        agent, rag, registry = self._agent()
        chunks = list(agent.ask_auto_iter("/rag 3D Gaussian Splatting이 뭐야?"))
        self.assertEqual(chunks, ["GROUNDED"])
        self.assertEqual(rag.iter_called, 1)
        self.assertEqual(registry.calls, [])  # permissive path not taken

    def test_ask_rag_invokes_rag_service_once(self) -> None:
        # The agent no longer keeps a parallel chat_history list — turn
        # recording is owned by the memory runtime via RAGService.iter_answer /
        # .answer (which go through MemoryAwareLLMClient.call/iter_call). This
        # test just guards the single-invocation contract on the strict path.
        agent, rag, _ = self._agent()
        agent.ask_rag("질문")
        self.assertEqual(rag.answer_called, 1)


if __name__ == "__main__":
    unittest.main()
