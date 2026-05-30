"""Concrete policy objects for memory eviction and retrieval."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from core.memory.budget import MemoryBudget
from core.memory.models import MemoryItem


@dataclass
class EvictionDecision:
    """Result of selecting FIFO rows for eviction."""

    evict_items: list[MemoryItem] = field(default_factory=list)
    keep_items: list[MemoryItem] = field(default_factory=list)
    reason: str = ""


@dataclass
class RetrievalDecision:
    """Recall/archival retrieval limits for one call."""

    recall_limit: int = 0
    archival_limit: int = 0
    reason: str = ""


class EvictionPolicy(Protocol):
    """Protocol for FIFO eviction decisions."""

    def select_eviction_candidates(
        self,
        items: list[MemoryItem],
        budget: MemoryBudget,
    ) -> EvictionDecision:
        """Split items into evict/keep buckets."""
        ...


class RetrievalPolicy(Protocol):
    """Protocol for recall/archival retrieval limits."""

    def decide_retrieval(
        self,
        query: str,
        budget: MemoryBudget,
    ) -> RetrievalDecision:
        """Return retrieval limits for this call."""
        ...


@dataclass(frozen=True)
class FIFOTailEvictionPolicy:
    """Evict old FIFO prefix while keeping a fixed recent tail."""

    keep_tail: int = 20

    def select_eviction_candidates(
        self,
        items: list[MemoryItem],
        budget: MemoryBudget,
    ) -> EvictionDecision:
        del budget
        keep_tail = max(0, int(self.keep_tail))
        if len(items) <= keep_tail:
            return EvictionDecision(keep_items=list(items), reason="under_tail_limit")
        return EvictionDecision(
            evict_items=list(items[:-keep_tail]) if keep_tail else list(items),
            keep_items=list(items[-keep_tail:]) if keep_tail else [],
            reason=f"fifo_tail_keep_{keep_tail}",
        )


@dataclass(frozen=True)
class ImportanceAwareEvictionPolicy:
    """Keep recent and high-importance/accessed items before evicting FIFO rows."""

    keep_tail: int = 20

    def select_eviction_candidates(
        self,
        items: list[MemoryItem],
        budget: MemoryBudget,
    ) -> EvictionDecision:
        del budget
        keep_count = max(0, int(self.keep_tail))
        if len(items) <= keep_count:
            return EvictionDecision(keep_items=list(items), reason="under_tail_limit")
        scored: list[tuple[float, int, MemoryItem]] = []
        for index, item in enumerate(items):
            recency = index / max(1, len(items) - 1)
            score = recency + float(item.importance_hint or 0.0) + min(3, int(item.access_count or 0)) * 0.1
            scored.append((score, index, item))
        keep_indexes = {
            index
            for _score, index, _item in sorted(scored, key=lambda row: row[0], reverse=True)[
                :keep_count
            ]
        }
        evict_items = [item for index, item in enumerate(items) if index not in keep_indexes]
        keep_items = [item for index, item in enumerate(items) if index in keep_indexes]
        return EvictionDecision(
            evict_items=evict_items,
            keep_items=keep_items,
            reason=f"importance_keep_{keep_count}",
        )


@dataclass(frozen=True)
class FixedKRetrievalPolicy:
    """Deterministic fixed top-k retrieval limits."""

    recall_limit: int = 3
    archival_limit: int = 2
    recall_scan_limit: int = 8
    archival_scan_limit: int = 6

    def decide_retrieval(
        self,
        query: str,
        budget: MemoryBudget,
    ) -> RetrievalDecision:
        del budget
        if not str(query or "").strip():
            return RetrievalDecision(reason="empty_query")
        return RetrievalDecision(
            recall_limit=max(0, int(self.recall_limit)),
            archival_limit=max(0, int(self.archival_limit)),
            reason="fixed_k",
        )


@dataclass(frozen=True)
class ProfilePolicySet:
    """Policy pair selected for a memory profile."""

    eviction: EvictionPolicy
    retrieval: RetrievalPolicy


class ProfilePolicyDispatcher:
    """Select memory policies by profile name."""

    def __init__(self, profiles: dict[str, ProfilePolicySet] | None = None) -> None:
        self._profiles = profiles or self._default_profiles()

    def policies_for(self, profile: str) -> ProfilePolicySet:
        key = str(profile or "chat").strip().lower()
        return self._profiles.get(key) or self._profiles["chat"]

    def eviction_for(self, profile: str) -> EvictionPolicy:
        return self.policies_for(profile).eviction

    def retrieval_for(self, profile: str) -> RetrievalPolicy:
        return self.policies_for(profile).retrieval

    @staticmethod
    def _default_profiles() -> dict[str, ProfilePolicySet]:
        chat = ProfilePolicySet(
            eviction=FIFOTailEvictionPolicy(keep_tail=20),
            retrieval=FixedKRetrievalPolicy(recall_limit=3, archival_limit=2),
        )
        grounded = ProfilePolicySet(
            eviction=FIFOTailEvictionPolicy(keep_tail=20),
            retrieval=FixedKRetrievalPolicy(recall_limit=0, archival_limit=0),
        )
        return {
            "chat": chat,
            "autosurvey": ProfilePolicySet(
                eviction=FIFOTailEvictionPolicy(keep_tail=20),
                retrieval=FixedKRetrievalPolicy(recall_limit=2, archival_limit=1),
            ),
            "editor": ProfilePolicySet(
                eviction=FIFOTailEvictionPolicy(keep_tail=20),
                retrieval=FixedKRetrievalPolicy(recall_limit=1, archival_limit=1),
            ),
            "verify": grounded,
            "rag": grounded,
        }
