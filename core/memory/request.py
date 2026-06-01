"""Tool이 Gateway에 넘기는 호출 단위 contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CallConstraints:
    """호출별 memory 개입 제약."""

    # True면 working_context 섹션을 system msg에 넣지 않는다.
    grounded: bool = False
    # True면 working/summary/history 일체 주입하지 않는다.
    json_strict: bool = False
    # True면 retrieval/flush를 건너뛰고 turn 기록도 하지 않는다.
    latency_critical: bool = False
    # True면 invocation log 외 모든 기록을 생략한다.
    no_record: bool = False
    # False면 working/summary/FIFO memory context를 prompt에 넣지 않는다.
    inject_memory_context: bool = True


@dataclass(frozen=True)
class CallRequest:
    """Tool → Gateway 호출 요청."""

    task_instruction: str
    user_content: str
    record_content: str = ""
    constraints: CallConstraints = field(default_factory=CallConstraints)
    use_history: bool = True
    profile: str = "chat"
    stream_label: str = ""
    method_hint: str = "call"
    sampling_params: dict[str, Any] | None = None
    extra_sampling_params: dict[str, Any] | None = None
    timeout_sec: float | None = None

    # True면 wrapper가 raw_llm.chat에 memory self-edit tool schema를 함께 전달한다.
    # LLM이 working_context_append/replace, recall_search를 호출할 수 있게 된다.
    enable_memory_tools: bool = False
