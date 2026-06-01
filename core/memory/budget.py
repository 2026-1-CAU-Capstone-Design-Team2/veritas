"""LLM 호출의 prompt token budget."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryBudget:
    """단일 호출의 token 회계."""

    max_context_tokens: int
    reserve_output_tokens: int = 1024

    system_tokens: int = 0
    current_request_tokens: int = 0

    working_context_tokens: int = 1200
    fifo_tokens: int = 1800
    recall_tokens: int = 1200

    warning_ratio: float = 0.60
    flush_ratio: float = 0.80

    @property
    def usable_prompt_tokens(self) -> int:
        """응답 reserve를 뺀 입력 가능 token 수."""
        return max(512, self.max_context_tokens - self.reserve_output_tokens)

    @property
    def warning_tokens(self) -> int:
        """memory pressure 경고 임계값."""
        return int(self.usable_prompt_tokens * self.warning_ratio)

    @property
    def flush_tokens(self) -> int:
        """FIFO evict + summarize 임계값."""
        return int(self.usable_prompt_tokens * self.flush_ratio)
