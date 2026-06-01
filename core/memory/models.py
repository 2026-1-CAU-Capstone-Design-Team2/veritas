"""Memory의 공통 dataclass와 enum."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MemoryTier(str, Enum):
    """저장 계층 식별자."""

    WORKING = "working"
    FIFO = "fifo"
    RECALL = "recall"
    SUMMARY = "summary"


class MemoryRole(str, Enum):
    """item을 만든 화자/근원."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    EVENT = "event"


@dataclass(frozen=True)
class MemoryScope:
    """item이 묶이는 격리 범위."""

    workspace_id: str
    session_id: str = ""
    agent_id: str = ""
    surface: str = ""


@dataclass
class MemoryItem:
    """단일 memory record."""

    id: str
    tier: MemoryTier
    role: MemoryRole
    content: str
    source: str
    created_at: str
    token_count: int = 0
    last_accessed_at: str | None = None
    access_count: int = 0
    importance_hint: float = 0.0
    confidence: float = 1.0
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryInvocation:
    """LLM 호출 한 건의 metadata."""

    invocation_id: str
    scope: MemoryScope
    profile: str
    method: str
    stream_label: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
