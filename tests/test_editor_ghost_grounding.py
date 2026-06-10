"""Editor ghostwriting: document-structure injection + relative relevance gate.

Locks the quality changes to the inline continuation:
- the section heading the cursor sits under is injected as ``[현재 섹션 제목]`` so
  a long-document continuation stays on the section's topic;
- grounding retrieval keeps only the tight cluster around the best hit (relative
  gate), dropping the loosely-related tail that a small model can't ignore.
"""

from __future__ import annotations

import unittest

from agent.chat_agent import ChatAgent


class _CaptureLLM:
    """Minimal LLM stub: records (system, user) prompts and streams one reply."""

    def __init__(self, reply: str = "이어지는 내용입니다.") -> None:
        self.reply = reply
        self.captured: list[tuple[str, str]] = []
        self.n_ctx = 8192

    def iter_ask(self, system_prompt, user_prompt, **_kwargs):
        self.captured.append((system_prompt, user_prompt))
        yield self.reply


def _doc(distance: float) -> dict:
    return {"doc_id": f"d{distance}", "content": "x", "distance": distance}


class RelativeDistanceGateTests(unittest.TestCase):
    filt = staticmethod(ChatAgent._filter_by_relative_distance)

    def test_drops_the_loosely_related_tail(self) -> None:
        docs = [_doc(0.10), _doc(0.12), _doc(0.30), _doc(0.50)]
        kept = self.filt(docs, top_k=3, margin=0.08)
        self.assertEqual([d["distance"] for d in kept], [0.10, 0.12])

    def test_caps_at_top_k_even_when_all_close(self) -> None:
        docs = [_doc(0.10), _doc(0.11), _doc(0.12), _doc(0.13)]
        kept = self.filt(docs, top_k=3, margin=0.08)
        self.assertEqual(len(kept), 3)

    def test_missing_distance_returns_top_k_unchanged_order(self) -> None:
        docs = [{"content": "a"}, {"content": "b"}, {"content": "c"}, {"content": "d"}]
        kept = self.filt(docs, top_k=2, margin=0.08)
        self.assertEqual(kept, docs[:2])

    def test_empty(self) -> None:
        self.assertEqual(self.filt([], top_k=3, margin=0.08), [])


class StructureHelperTests(unittest.TestCase):
    def test_nearest_heading_picks_the_last_one(self) -> None:
        text = "# 서론\n내용\n\n## 본론\n여기서 쓰는 중"
        self.assertEqual(ChatAgent._nearest_heading(text), "본론")

    def test_nearest_heading_none(self) -> None:
        self.assertEqual(ChatAgent._nearest_heading("제목 없는 본문\n계속"), "")

    def test_current_paragraph_is_text_after_last_blank_line(self) -> None:
        prefix = "앞 문단입니다.\n\n현재 문단의 첫 문장."
        self.assertEqual(ChatAgent._current_paragraph(prefix), "현재 문단의 첫 문장.")

    def test_current_paragraph_strips_heading_line(self) -> None:
        prefix = "## 본론\n현재 문단 내용"
        self.assertEqual(ChatAgent._current_paragraph(prefix), "현재 문단 내용")


class GhostSectionInjectionTests(unittest.TestCase):
    def _agent(self) -> tuple[ChatAgent, _CaptureLLM]:
        llm = _CaptureLLM()
        # rag_service=None → no grounding, so we isolate the section-block change.
        agent = ChatAgent(llm=llm, rag_service=None, tool_registry=None)
        return agent, llm

    def test_caller_section_heading_is_injected(self) -> None:
        agent, llm = self._agent()
        list(
            agent.iter_ghostwrite(
                "이것은 충분히 긴 도입 문장입니다.",
                "",
                use_workspace=False,
                section_heading="서론 배경",
            )
        )
        _system, user = llm.captured[-1]
        self.assertIn("[현재 섹션 제목]", user)
        self.assertIn("서론 배경", user)

    def test_heading_falls_back_to_prefix_when_not_supplied(self) -> None:
        agent, llm = self._agent()
        list(
            agent.iter_ghostwrite(
                "# 결론\n\n이 절에서는 충분히 긴 내용을 다룬다.",
                "",
                use_workspace=False,
            )
        )
        _system, user = llm.captured[-1]
        self.assertIn("[현재 섹션 제목]", user)
        self.assertIn("결론", user)

    def test_no_section_block_when_no_heading(self) -> None:
        agent, llm = self._agent()
        list(
            agent.iter_ghostwrite(
                "제목 없이 충분히 긴 본문을 이어서 작성하는 중입니다.",
                "",
                use_workspace=False,
            )
        )
        _system, user = llm.captured[-1]
        self.assertNotIn("[현재 섹션 제목]", user)


if __name__ == "__main__":
    unittest.main()
