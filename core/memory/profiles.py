"""\
LLM 호출별 memory 개입 수준.

호출의 strictness(JSON 강제, RAG grounding, ghost streaming 등)에 따라
memory envelope 주입과 recall 기록 여부를 다르게 가져가야 한다. 모든
generation 호출은 MemoryAwareLLMClient를 통과하지만, profile에 따라
prepare/commit의 동작 강도가 달라진다.

`SCREEN_GROUNDED`는 memory_arch.md에는 없는 항목이다 — Veritas의
screen_context 호출이 working_context는 활용해야 하지만 recall context는
prompt 안에 이미 들어가 있는 KB context와 충돌하므로, INTERACTIVE와
STRICT_GROUNDED 사이의 중간 정책으로 신설했다.
"""

from __future__ import annotations

from enum import Enum


class MemoryProfile(str, Enum):
    """Memory 개입 정책.

    | profile         | envelope            | recall append    | 사용처                       |
    | --------------- | ------------------- | ---------------- | ---------------------------- |
    | DISABLED        | X                   | X                | 완전 휘발 (cleanup 등)       |
    | OBSERVE         | X                   | invocation만     | tool JSON / query rewrite    |
    | BUDGETED        | X                   | X                | editor ghost (latency 중요)  |
    | INTERACTIVE     | working+FIFO+recall | O                | 일반 chat                    |
    | SCREEN_GROUNDED | working만           | O                | screen intervention          |
    | STRICT_GROUNDED | X                   | O                | RAG / Draft (KB 오염 금지)   |
    | INTERNAL        | X                   | X                | summarizer 자기호출(우회용)  |
    """

    # Memory 완전 비활성. invocation log조차 남기지 않는다.
    DISABLED = "disabled"
    
    # invocation log만 남기고 prompt/recall은 건드리지 않는다. tool JSON 호출의 기본값.
    OBSERVE = "observe"
    
    # token budget 검사와 logging만 수행. ghost-writing처럼 latency 민감 호출.
    BUDGETED = "budgeted"
    
    # 일반 chat. working + FIFO summary + recall snippet을 envelope에 주입한다.
    INTERACTIVE = "interactive"
    
    # screen intervention. working_context만 주입(KB context는 이미 prompt 내부에 있음).
    SCREEN_GROUNDED = "screen_grounded"
    
    # RAG/Draft. envelope 주입 안 함(grounding 오염 방지), recall 기록만 함.
    STRICT_GROUNDED = "strict_grounded"
    
    # Memory runtime이 스스로 만드는 LLM 호출(summary, working rewrite). 재귀 방지용.
    INTERNAL = "internal"
