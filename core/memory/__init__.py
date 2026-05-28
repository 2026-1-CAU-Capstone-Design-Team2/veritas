"""Memory layer contract — dependency-free.

services/memory_tools_funcs/와 llm/memory_aware_llm.py가 공유하는 데이터
모델·budget·profile·policy interface를 노출한다. 이 패키지는 LLM 호출,
파일 IO, ChromaDB 접근, workspace path 관리를 일절 하지 않는다.

`from core.memory import MemoryItem, MemoryProfile, ...` 형태로 모든 공개
타입을 한 번에 가져갈 수 있도록 재노출한다.
"""

from core.memory.budget import MemoryBudget
from core.memory.models import (
    MemoryInvocation,
    MemoryItem,
    MemoryRole,
    MemoryScope,
    MemoryTier,
    PromptPackage,
)
from core.memory.policy import (
    EvictionDecision,
    EvictionPolicy,
    RetrievalDecision,
    RetrievalPolicy,
)
from core.memory.profiles import MemoryProfile


__all__ = [
    # budget
    "MemoryBudget",
    # models
    "MemoryInvocation",
    "MemoryItem",
    "MemoryRole",
    "MemoryScope",
    "MemoryTier",
    "PromptPackage",
    # policy
    "EvictionDecision",
    "EvictionPolicy",
    "RetrievalDecision",
    "RetrievalPolicy",
    # profiles
    "MemoryProfile",
]
