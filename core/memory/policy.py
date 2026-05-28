"""
Memory policy 인터페이스와 결정 객체.
budget과 item의 일반적 속성(tier/role/token_count/timestamp 등)으로 결정.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from core.memory.budget import MemoryBudget
from core.memory.models import MemoryItem


@dataclass
class EvictionDecision:
    """
    QueueManager가 FIFO를 어느 항목까지 잘라낼지 정한 결과.

    evict_items는 summarizer로 압축할 대상, keep_items는 FIFO에 그대로
    남길 tail. reason은 추적/디버깅 용도(이 결정이 왜 일어났는지).
    """

    # 이번 flush에서 요약으로 압축할 대상(시간 순서대로).
    evict_items: list[MemoryItem] = field(default_factory=list)
    # 압축하지 않고 FIFO에 그대로 남길 tail(최근 N개).
    keep_items: list[MemoryItem] = field(default_factory=list)
    # 이 결정이 일어난 사유 라벨("flush_pressure" | "manual" | "scope_change" 등).
    reason: str = ""


@dataclass
class RetrievalDecision:
    """
    ContextBuilder가 recall/archival에서 얼마나 끌어올지 정한 한도.

    실제 검색 자체는 RetrievalPolicy 호출자(=ContextBuilder)가 수행하며,
    RetrievalDecisiond은 "몇 개를 budget 안에서 가져올 수 있는가"를 명시함.
    """

    # recall_storage에서 끌어올릴 최대 item 개수.
    recall_limit: int = 0
    # archival_storage에서 끌어올릴 최대 item 개수.
    archival_limit: int = 0
    # 이 결정이 일어난 사유 라벨("interactive_default" | "screen_grounded_only_working" 등).
    reason: str = ""


class EvictionPolicy(Protocol):
    """
    FIFO eviction 결정 규약.

    구현체는 budget 한도 안에서 evict/keep을 가르는 단일 함수만 제공
    """

    def select_eviction_candidates(
        self,
        items: list[MemoryItem],
        budget: MemoryBudget,
    ) -> EvictionDecision:
        """
        주어진 FIFO 항목과 budget으로 evict 대상을 고른다.

        가장 오래된 prefix를 evict하고 최근 N개는 keep으로 남기기
        (MemGPT의 Queue Manager 정책과 동일).
        """
        ...


class RetrievalPolicy(Protocol):
    """
    Recall/Archival 인입 한도 결정 규약. query 키워드와 budget로 한도를 결정한다.
    """

    def decide_retrieval(
        self,
        query: str,
        budget: MemoryBudget,
    ) -> RetrievalDecision:
        """
        이번 호출에서 recall/archival에서 끌어올 최대 개수를 결정한다.
        """
        ...