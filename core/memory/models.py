

"""
Memory layer의 공통 데이터 모델.

Memory 시스템의 모든 계층(core 계약 / services 런타임 / llm wrapper)이
공유하는 dependency-free dataclass와 enum을 정의한다. 
!!!!이 모듈은 LLM 호출, 파일 IO, ChromaDB 접근, screen/action policy를 일절 포함하지 않음 !!! 별개임
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MemoryTier(str, Enum):
    """
    Memory item이 속한 저장 계층.

    MemGPT의 main/external context 구조에 대응한다:
    - WORKING/FIFO: main context (LLM window안, 항상 prompt에 포함)
    - RECALL/ARCHIVAL: external context (window 밖, 명시적 인입 필요)
    - SUMMARY: FIFO flush 시 생성되는 recursive summary
    """

    WORKING = "working"
    FIFO = "fifo"
    RECALL = "recall"
    ARCHIVAL = "archival"
    SUMMARY = "summary"


class MemoryRole(str, Enum):
    """
    Memory item을 만든 화자/근원의 역할 식별자.

    SYSTEM/USER/ASSISTANT/TOOL은 OpenAI 챗 컨벤션을 따른다.
    EVENT는 screen capture, document save 등 비대화 이벤트를 recall에
    함께 적재할 때 쓰는 라벨이다.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    EVENT = "event"


@dataclass(frozen=True)
class MemoryScope:
    """
    Memory item이 묶이는 격리 범위.

    workspace_id가 1차 격리 단위(`runs/<workspace>/memory/`)이며,
    session/agent/surface는 같은 workspace 내에서 origin을 식별하는
    보조 라벨이다. surface는 chat/rag/editor/draft/screen 등 generic
    label만 들어가야 한다(도메인 정책을 섞지 않는다).
    """

    workspace_id: str
    session_id: str = ""
    agent_id: str = ""
    surface: str = ""


@dataclass
class MemoryItem:
    """
    단일 memory record.

    Tier마다 같은 dataclass를 재사용한다. tier 필드로 어느 저장소에
    속하는지를 구분하고, role/source/metadata로 origin을 추적한다.
    Storage 계층은 이 객체를 JSONL로 직접 직렬화한다.
    """

    # 안정적 UUID — 같은 turn이 FIFO와 recall에 동시 적재될 때 두 record를 같은 id로 연결.
    id: str
    # 어느 저장 계층에 속하는지(WORKING/FIFO/RECALL/ARCHIVAL/SUMMARY).
    tier: MemoryTier
    # 화자/근원 역할(SYSTEM/USER/ASSISTANT/TOOL/EVENT).
    role: MemoryRole
    # 본문. recall에는 raw text, summary tier에는 압축된 요약문이 들어간다.
    content: str
    # origin 라벨. 보통 stream_label("chat:final", "screen_context", "rag" 등).
    source: str
    # 생성 시각(ISO-8601 UTC).
    created_at: str
    # 본문의 추정 token 수. TokenCounter가 추정해서 채운다.
    token_count: int = 0
    # 마지막으로 prompt에 끌어올린 시각. retrieval policy의 hint.
    last_accessed_at: str | None = None
    # 누적 retrieval 횟수. archival promote 정책의 hint.
    access_count: int = 0
    # 0~1 사이의 중요도 hint. 사용자가 명시한 경우 1.0, 자동 분류 시 추정값.
    importance_hint: float = 0.0
    # 사실/관찰의 확신도. tool 결과는 1.0, 모델 추론은 < 1.0으로 낮춰 기록.
    confidence: float = 1.0
    # 자유 태그(예: ["preference", "deadline"]).
    tags: list[str] = field(default_factory=list)
    # 호출별 부가정보(invocation_id, method, tool_name 등).
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptPackage:
    """
    ContextBuilder가 envelope을 주입한 뒤의 최종 prompt.

    raw LLM 호출 직전에 MemoryRuntime -> MemoryAwareLLMClient 사이를 오가는 운반 객체. 
    profile/invocation_id 등 부가정보는 metadata에 담아 commit 단계에서 다시 꺼낼 수 있게 한다.
    """

    # envelope 적용 후 LLM에 그대로 전달될 system prompt.
    system_prompt: str
    # envelope 적용 후 LLM에 그대로 전달될 user prompt.
    user_prompt: str
    # profile, envelope_chars 등 commit/로깅에 쓰일 부가정보.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryInvocation:
    """
    한 번의 LLM 호출에 대한 metadata(=invocations.jsonl 1줄).

    실제 prompt 본문은 여기 저장하지 않는다 — recall_storage에 user/assistant turn으로 따로 적재된다.
     이 record는 budget 회계,
    profile 추적, 호출 빈도 분석에 쓴다.
    """

    # 호출 식별자. 같은 invocation_id가 USER/ASSISTANT record에 metadata로 따라간다.
    invocation_id: str
    # workspace/session/agent/surface 격리 범위.
    scope: MemoryScope
    # 적용된 MemoryProfile의 value 문자열("interactive", "strict_grounded" 등).
    profile: str
    # 호출 메서드("ask" | "iter_ask" | "ask_json" | "collect_tool_outputs").
    method: str
    # call site label(stream_label). profile 추론과 추적의 기본 키.
    stream_label: str = ""
    # 호출 시각(ISO-8601 UTC).
    created_at: str = ""
    # system_tokens / request_tokens / flush_triggered 등 호출별 부가정보.
    metadata: dict[str, Any] = field(default_factory=dict)
