"""Memory policy 인터페이스와 결정 객체."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from core.memory.budget import MemoryBudget
from core.memory.models import MemoryItem


@dataclass
class EvictionDecision:
    """FIFO eviction 결과."""

    evict_items: list[MemoryItem] = field(default_factory=list)
    keep_items: list[MemoryItem] = field(default_factory=list)
    reason: str = ""


@dataclass
class RetrievalDecision:
    """recall/archival 인입 한도."""

    recall_limit: int = 0
    archival_limit: int = 0
    reason: str = ""


class EvictionPolicy(Protocol):
    """FIFO eviction 결정 규약."""

    def select_eviction_candidates(
        self,
        items: list[MemoryItem],
        budget: MemoryBudget,
    ) -> EvictionDecision:
        """evict/keep을 가른다."""
        ...


class RetrievalPolicy(Protocol):
    """recall/archival 인입 한도 결정 규약."""

    def decide_retrieval(
        self,
        query: str,
        budget: MemoryBudget,
    ) -> RetrievalDecision:
        """이번 호출의 인입 limit를 정한다."""
        ...
