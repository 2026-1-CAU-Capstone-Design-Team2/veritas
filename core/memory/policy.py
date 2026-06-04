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
    """Recall retrieval limits for one call."""

    recall_limit: int = 0
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
    """Protocol for recall retrieval limits."""

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


def _protect_recent_tokens(max_context_tokens: int) -> int:
    """PC 환경(=llama-server가 결정한 n_ctx)별 최소 보호 토큰. 직전 1~2 turn은 항상 keep."""
    if max_context_tokens <= 4096:
        return 384
    if max_context_tokens <= 8192:
        return 768
    if max_context_tokens <= 16384:
        return 1280
    if max_context_tokens <= 32768:
        return 2048
    return 3072


def _target_fifo_tokens(max_context_tokens: int) -> int:
    """PC 환경(=n_ctx)별 FIFO 점유 목표 토큰. evict 후 FIFO가 이 이하로 수렴."""
    if max_context_tokens <= 4096:
        return 1200
    if max_context_tokens <= 8192:
        return 2400
    if max_context_tokens <= 16384:
        return 4096
    if max_context_tokens <= 32768:
        return 8192
    return 16384


@dataclass(frozen=True)
class TokenBudgetFIFOEvictionPolicy:
    """Evict oldest FIFO rows so the surviving tail fits the token target.

    최신부터 token을 누적해서 protect_recent_tokens까지는 무조건 keep,
    그 뒤로는 target_fifo_tokens 이하인 동안만 keep, 초과 시 그 이전(오래된)
    row는 모두 evict.
    """

    def select_eviction_candidates(
        self,
        items: list[MemoryItem],
        budget: MemoryBudget,
    ) -> EvictionDecision:
        if not items:
            return EvictionDecision(reason="empty")

        max_ctx = int(getattr(budget, "max_context_tokens", 0) or 0)
        protect_recent = _protect_recent_tokens(max_ctx)
        target_fifo = _target_fifo_tokens(max_ctx)

        keep_indexes: set[int] = set()
        accumulated = 0
        for idx in range(len(items) - 1, -1, -1):
            item = items[idx]
            tokens = int(item.token_count or 0)
            if accumulated < protect_recent:
                # 안전대: 직전 turn은 token target과 무관하게 항상 보존
                keep_indexes.add(idx)
                accumulated += tokens
                continue
            if accumulated + tokens <= target_fifo:
                keep_indexes.add(idx)
                accumulated += tokens
                continue
            break  # 이 이후 오래된 row는 모두 evict

        evict_items = [item for idx, item in enumerate(items) if idx not in keep_indexes]
        keep_items = [item for idx, item in enumerate(items) if idx in keep_indexes]

        if not evict_items:
            return EvictionDecision(keep_items=keep_items, reason="under_target")

        return EvictionDecision(
            evict_items=evict_items,
            keep_items=keep_items,
            reason=f"token_budget_target_{target_fifo}_protect_{protect_recent}",
        )


@dataclass(frozen=True)
class FixedKRetrievalPolicy:
    """Deterministic fixed top-k retrieval limits."""

    recall_limit: int = 3

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
            eviction=TokenBudgetFIFOEvictionPolicy(),
            retrieval=FixedKRetrievalPolicy(recall_limit=3),
        )
        grounded = ProfilePolicySet(
            eviction=FIFOTailEvictionPolicy(keep_tail=20),
            retrieval=FixedKRetrievalPolicy(recall_limit=0),
        )
        return {
            "chat": chat,
            "autosurvey": ProfilePolicySet(
                eviction=FIFOTailEvictionPolicy(keep_tail=20),
                retrieval=FixedKRetrievalPolicy(recall_limit=2),
            ),
            "editor": ProfilePolicySet(
                eviction=FIFOTailEvictionPolicy(keep_tail=20),
                retrieval=FixedKRetrievalPolicy(recall_limit=1),
            ),
            "verify": grounded,
            # RAG answers are document-grounded, but recall is allowed as a
            # small secondary context (e.g. "what did I ask earlier"). The
            # RAG_SYSTEM_PROMPT still enforces "answer only from documents".
            "rag": ProfilePolicySet(
                eviction=FIFOTailEvictionPolicy(keep_tail=20),
                retrieval=FixedKRetrievalPolicy(recall_limit=2),
            ),
        }
