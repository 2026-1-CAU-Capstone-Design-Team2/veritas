"""
LLM 호출의 prompt token budget 정의.

MemoryRuntime.prepare()가 호출마다 한 번 생성하는 value object다.
max_context_tokens는 호출 시점의 raw_llm.n_ctx에서 받는다 — 모델
스왑 후의 새 n_ctx도 다음 prepare에서 자연스럽게 반영된다(frozen=True
이지만 호출별로 새로 만들기 때문에 mutate가 필요하지 않다).
"""

from __future__ import annotations

from dataclasses import dataclass

"""
    max_context_tokens : 모델이 지닐 수 있는 context window 크기.

"""


@dataclass(frozen=True)
class MemoryBudget:
    """
    단일 LLM 호출의 token 회계.

    Working/FIFO/Recall/Archival의 *목표* 할당량을 정해두고, 실제 prompt
    조립은 ContextBuilder가 이 한도 안에서 잘라 넣는다. 
    warning_ratio / flush_ratio는 누적 FIFO token에 대한 임계값 비율이다.
    """

    # 모델의 컨텍스트 윈도 크기(raw_llm.n_ctx). 모델 스왑 시 다음 prepare에서 새 값으로 들어옴.
    max_context_tokens: int
    # 응답 생성 reserve. 입력 budget = max_context_tokens - reserve_output_tokens.
    reserve_output_tokens: int = 1024

    # 이 호출의 system_prompt token 추정값. prepare()가 측정해서 채운다.
    system_tokens: int = 0
    # 이 호출의 user_prompt token 추정값(envelope 주입 전 원본 기준).
    current_request_tokens: int = 0

    # working_context envelope에 할당된 목표 token. ContextBuilder가 한도 안에서 자른다.
    working_context_tokens: int = 1200
    # FIFO summary envelope에 할당된 목표 token.
    fifo_tokens: int = 1800
    # recall_context envelope에 할당된 목표 token.
    recall_tokens: int = 1200
    # archival_context envelope에 할당된 목표 token.
    archival_tokens: int = 1200

    # FIFO 누적 token이 usable * warning_ratio를 넘으면 pressure 경고를 띄울 수 있다.
    warning_ratio: float = 0.70
    # FIFO 누적 token이 usable * flush_ratio를 넘으면 강제로 evict + summarize 한다.
    flush_ratio: float = 1.00

    @property
    def usable_prompt_tokens(self) -> int:
        """
        모델 응답 reserve를 뺀, 입력으로 쓸 수 있는 최대 token 수.

        하한은 512 — n_ctx가 비정상적으로 작게 잡혀도 prompt를 0 또는
        음수로 잘라내지 않도록 보호.
        """
        return max(512, self.max_context_tokens - self.reserve_output_tokens)

    @property
    def warning_tokens(self) -> int:
        """
        이 값을 넘으면 memory pressure 경고를 system message로 흘릴 수 있다.

        1차 구현에서는 logging만 하고, 2차에서 MEMORY_PRESSURE_SYSTEM_MESSAGE를
        prompt에 prepend하도록 확장한다.
        """
        return int(self.usable_prompt_tokens * self.warning_ratio)

    @property
    def flush_tokens(self) -> int:
        """
        이 값을 넘으면 QueueManager가 FIFO를 evict + recursive summarize 한다.

        flush_ratio 기본값 1.00은 "context window가 꽉 차면 비운다"는 보수적 설정이다.
        테스트에서는 더 낮은 값으로 강제 트리거할 수 있다.
        """
        return int(self.usable_prompt_tokens * self.flush_ratio)
