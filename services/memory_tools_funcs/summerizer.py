"""FIFO flush 시 evicted turn을 recursive summary로 압축."""

from __future__ import annotations

from core.prompts.memory import MEMORY_SUMMARY_PROMPT


class MemorySummarizer:
    """raw_llm을 직접 호출하여 evicted turn을 압축한다."""

    def __init__(self, raw_llm) -> None:
        self.raw_llm = raw_llm

    def summarize_evicted(
        self,
        *,
        previous_summary: str,
        evicted_messages: str,
    ) -> str:
        """evicted turn과 이전 summary를 합쳐 새 summary를 만든다."""
        prompt = MEMORY_SUMMARY_PROMPT.format(
            previous_summary=previous_summary or "(none)",
            evicted_messages=evicted_messages or "(none)",
        )
        text = self.raw_llm.ask(
            "You are a memory compression module. Return only the compact summary.",
            prompt,
            reasoning=False,
            sampling_params={
                "temperature": 0.0,
                "top_p": 0.2,
                "presence_penalty": 0.0,
                "max_tokens": 512,
            },
            extra_sampling_params={"repeat_penalty": 1.0},
            stream_label="memory:summary",
        )
        return str(text or "").strip()
