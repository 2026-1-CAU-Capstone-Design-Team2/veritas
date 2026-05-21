"""시나리오 베이스 — 추상 클래스 + 데이터 구조 + 공통 헬퍼 메서드.

모든 시나리오 클래스가 상속하는 ScenarioType, 그리고 detector ↔ scenario 사이
주고받는 데이터 컨테이너(ScenarioContext, ScenarioEvaluation)를 정의.

내용:
- ScenarioContext: 한 캡처 사이클 동안 모든 시나리오가 공유하는 입력 스냅샷
- ScenarioEvaluation: 시나리오별 평가 결과의 통일 포맷
- ScenarioType: 추상 베이스 클래스
  - priority → vruntime default 자동 도출 (`_PRIORITY_VRUNTIME_DEFAULTS`)
  - `_gate_result`: 게이트 결과 표준화
  - `_has_substantial_paragraph`: 문단 길이 prereq 헬퍼
  - `_time_cooldown_status`: 시간 기반 cooldown 헬퍼 (4 시나리오 공유)
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..core.models import FilteredScreenContext, WindowContext


@dataclass
class ScenarioContext:
    """한 캡처 사이클 동안 모든 시나리오가 공유하는 입력 스냅샷."""

    window: WindowContext
    filtered: FilteredScreenContext
    history_events: list[dict[str, Any]]
    same_document_events: list[dict[str, Any]]
    document_key: str
    paragraph_fingerprint: str
    # 문서 단위 {시나리오명: 마지막 발동 unix_ts}. detector가 scheduler 상태에서
    # 읽어 채움. 시간 기반 cooldown 게이트가 사용.
    last_fired_at: dict[str, float] = field(default_factory=dict)
    # 문서 단위 {시나리오명: 마지막 발동 시점의 정규화 문서 길이}.
    # whole_document_review의 "리뷰 이후 추가된 글자 수" 판정에 사용.
    last_fired_doc_chars: dict[str, int] = field(default_factory=dict)


@dataclass
class ScenarioEvaluation:
    """시나리오별 평가 결과를 담는 통일 포맷."""

    name: str
    ready: bool = False
    score: float = 0.0
    priority: str = "low"
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    gate_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class ScenarioType(ABC):
    """모든 개입 시나리오의 추상 베이스.

    각 시나리오는 자신의 게이트 함수, 우선순위, CFS 파라미터를 보유.
    `evaluate()`로 통일된 ScenarioEvaluation을 반환해 detector가 비교 가능하게 한다.
    """

    # priority → (initial_vruntime, vruntime_increment) 기본 매핑.
    # 서브클래스가 클래스 attribute로 명시하지 않은 경우 __init__에서 적용.
    _PRIORITY_VRUNTIME_DEFAULTS: dict[str, tuple[float, float]] = {
        "high":   (-5.0, 3.0),
        "medium": ( 0.0, 2.0),
        "low":    ( 5.0, 2.0),
    }

    name: str = ""
    priority: str = "medium"
    initial_vruntime: float = 0.0
    vruntime_increment: float = 1.0

    @classmethod
    def _default_vruntime_for_priority(cls, priority: str) -> tuple[float, float]:
        """priority 문자열에서 (initial_vruntime, vruntime_increment) default 반환.
        모르는 priority면 medium 값으로 fallback.
        """
        return cls._PRIORITY_VRUNTIME_DEFAULTS.get(
            priority, cls._PRIORITY_VRUNTIME_DEFAULTS["medium"]
        )

    def __init__(
        self,
        *,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        # 서브클래스가 자체 클래스 namespace에 명시하지 않은 vruntime 값은 priority 기반 default로 채움.
        # vars(cls)에 키가 없으면 명시 안 한 것 (베이스 ScenarioType에서 상속만 받은 상태).
        cls_vars = vars(type(self))
        derived_initial, derived_increment = self._default_vruntime_for_priority(self.priority)
        if "initial_vruntime" not in cls_vars:
            self.initial_vruntime = derived_initial
        if "vruntime_increment" not in cls_vars:
            self.vruntime_increment = derived_increment
        # ctor 인자가 주어지면 instance-level override.
        if initial_vruntime is not None:
            self.initial_vruntime = initial_vruntime
        if vruntime_increment is not None:
            self.vruntime_increment = vruntime_increment

    @abstractmethod
    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        """시나리오별 게이트를 실행하고 통일된 `ScenarioEvaluation`을 반환."""

    def writing_context_overrides(
        self,
        *,
        filtered: FilteredScreenContext,
        base: dict[str, Any],
    ) -> dict[str, Any]:
        """이 시나리오가 발동될 때 `writing_context`에 병합할 부분 필드 반환.
        기본은 빈 dict(오버라이드 없음). 서브클래스가 `focus_scope`, `recent_sentences` 등을 채움.
        """
        return {}

    def tool_routing_hint_overrides(
        self,
        *,
        event: Any,
        base: dict[str, Any],
        focused_sentence: str,
    ) -> dict[str, Any]:
        """이 시나리오가 발동될 때 `tool_routing_hint`에 병합할 부분 필드 반환.
        기본은 빈 dict. 서브클래스가 `tone`, `preferred_action` 등을 설정.
        """
        return {}

    def _gate_result(
        self,
        passed: bool,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """게이트 결과를 `{passed, reason, ...extra}` 형태로 표준화."""
        result: dict[str, Any] = {"passed": passed, "reason": reason}
        if extra:
            for key, value in extra.items():
                if key in ("passed", "reason"):
                    continue
                result[key] = value
        return result

    def _has_substantial_paragraph(
        self,
        filtered: FilteredScreenContext,
        *,
        min_chars: int = 20,
    ) -> bool:
        """현재 문단이 개입 대상으로 쓸 만큼 충분한 길이인지 판정하는 공유 헬퍼.
        문단 단위 시나리오만 자기 게이트로 호출. 공통 `stable_paragraph` 게이트는
        문단 길이를 검사하지 않으며 그 책임이 이 헬퍼로 옮겨졌음.
        """
        paragraph = " ".join((filtered.current_paragraph_text or "").split())
        return len(paragraph) >= min_chars

    def _time_cooldown_status(
        self,
        last_fired_at: dict[str, float],
        *,
        min_seconds: float | None = None,
    ) -> dict[str, Any]:
        """공유 시간 기반 cooldown 헬퍼.
        last_fired_at[self.name] 경과 시간이 min_seconds 이상이면 통과.
        min_seconds=None이면 self.cooldown_min_seconds 사용.
        4 시나리오(idle/long_static/churn/blank_doc) + Tier 1/2 시나리오 다수가 호출.
        scheduler 상태에서 읽으므로 발동 기록이 회전으로 잊히지 않아 긴 cooldown도 강제됨.
        """
        threshold = (
            min_seconds
            if min_seconds is not None
            else float(getattr(self, "cooldown_min_seconds", 0.0))
        )
        last_at = last_fired_at.get(self.name)
        if last_at is None:
            return {
                "passed": True,
                "reason": "no_prior_fire",
                "min_seconds": threshold,
            }
        elapsed_seconds = max(time.time() - last_at, 0.0)
        passed = elapsed_seconds >= threshold
        return {
            "passed": passed,
            "reason": "ok" if passed else "cooldown_active",
            "elapsed_seconds": round(elapsed_seconds, 1),
            "min_seconds": threshold,
        }
