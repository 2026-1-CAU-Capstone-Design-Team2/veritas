"""CaretContinuationEngine — native-ghostwrite 모델의 외부 앱 버전.

24-시나리오 + dwell + CFS + idle-gate를 대체한다. native editor의 이어쓰기처럼:

    캡처마다 "지금 쓰는 곳"(cursor_scope_text)을 읽고, 그 위치가 N회 폴링 동안
    바뀌지 않으면(= 사용자가 멈춤) **즉시** 이어쓰기 제안을 발화한다.

native editor가 Qt textChanged + 적응형 debounce로 하던 것을, 외부 앱에서는
UIA/diff 기반 cursor_scope 폴링으로 한다. caret을 신뢰있게 잡았는지는
``filtered.cursor_located``가 알려준다(UIA caret 또는 diff). 못 잡으면 발화하지
않는다 — 커서 모르면 제안 없음.

스팸 방지:
- **spot-dedup**: 같은 위치(scope fingerprint)에 이미 제안했으면 재발화 안 함.
  커서가 움직이거나 텍스트가 바뀌어야(=새 fingerprint) 다음 제안.
- **retry**: 사용자가 "다시"를 누르면 그 위치의 dedup을 풀고 **즉시** 재발화하되
  직전 제안을 ``avoid_text``로 넘겨 다른 문장을 만들게 한다(native reject ladder와
  동일한 의도). idle-gate처럼 "새 편집이 있어야"를 요구하지 않는다 — 이게 기존
  파이프라인에서 retry가 안 되던 근본 이유였다.
- 호출자(ScreenContextService)가 LLM 생성 중(busy)이거나 미해결 카드가 떠 있으면
  발화를 막는다(한 번에 카드 하나).
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Optional

from ..core.models import FilteredScreenContext, InterventionDecision


# 발화 전 cursor_scope가 안정돼야 하는 연속 폴링 횟수. 1초 폴링 × 2 ≈ 멈춘 뒤 ~2초.
DEFAULT_STABLE_POLLS = 2
# 이어쓰기를 하기에 충분한 최소 prefix(커서 앞 텍스트) 길이.
DEFAULT_MIN_PREFIX_CHARS = 20


@dataclass
class _DocState:
    scope_fp: str = ""
    stable_count: int = 0
    last_fired_fp: str = ""
    retry_avoid: Optional[str] = None  # set면 다음 관찰에서 즉시 재발화(avoid)
    retry_event_id: str = ""  # retry 시 재사용할 원래 카드 id(같은 카드 갱신용)


@dataclass
class FireDecision:
    """엔진 관찰 결과."""

    fire: bool = False
    intervention: Optional[InterventionDecision] = None
    reason: str = ""


class CaretContinuationEngine:
    def __init__(
        self,
        *,
        stable_polls: int = DEFAULT_STABLE_POLLS,
        min_prefix_chars: int = DEFAULT_MIN_PREFIX_CHARS,
    ) -> None:
        self.stable_polls = max(1, int(stable_polls))
        self.min_prefix_chars = max(1, int(min_prefix_chars))
        self._lock = threading.Lock()
        self._docs: dict[str, _DocState] = {}

    @staticmethod
    def _fingerprint(scope_text: str) -> str:
        norm = " ".join((scope_text or "").split())
        return hashlib.sha1(norm.encode("utf-8")).hexdigest()

    def request_retry(
        self, document_key: str, *, avoid_text: str = "", target_event_id: str = ""
    ) -> None:
        """사용자 "다시": 그 문서의 현재 위치 dedup을 풀고 다음 관찰에서 즉시
        재발화하도록 예약한다. ``avoid_text``(직전 제안)는 새 문장이 그걸 피하게
        생성기로 전달된다. ``target_event_id``(원래 카드 id)가 주어지면 재발화가
        새 카드를 만들지 않고 그 카드를 **갱신**하도록 흘려보낸다."""
        key = document_key or "_default_"
        with self._lock:
            st = self._docs.setdefault(key, _DocState())
            st.retry_avoid = str(avoid_text or "")
            st.retry_event_id = str(target_event_id or "")
            st.last_fired_fp = ""  # 같은 위치라도 다시 발화 허용

    def reset(self, document_key: Optional[str] = None) -> None:
        with self._lock:
            if document_key is None:
                self._docs.clear()
            else:
                self._docs.pop(document_key or "_default_", None)

    def observe(
        self,
        *,
        document_key: str,
        filtered: FilteredScreenContext,
        busy: bool,
        card_active: bool,
        suppressed: bool = False,
    ) -> FireDecision:
        """한 캡처 관찰. 발화 결정 반환.

        ``suppressed=True``(예: 진입 직후 startup grace)면 안정 카운트는 그대로
        누적하되 **발화하지 않는다** — 사용자가 커서를 자리잡을 1~2초를 준 뒤,
        grace가 풀리면 (여전히 안정이면) 곧바로 발화한다. last_fired_fp를 건드리지
        않으므로 grace 후 dedup에 걸리지 않는다."""
        key = document_key or "_default_"
        scope = (getattr(filtered, "cursor_scope_text", "") or "").strip()
        located = bool(getattr(filtered, "cursor_located", False))

        with self._lock:
            st = self._docs.setdefault(key, _DocState())

            # 커서 미확정 → 위치 모름. 안정 카운트 리셋, 발화 없음.
            if not located or len(scope) < self.min_prefix_chars:
                st.scope_fp = ""
                st.stable_count = 0
                return FireDecision(reason="not_located_or_short")

            fp = self._fingerprint(scope)
            if fp == st.scope_fp:
                st.stable_count += 1
            else:
                st.scope_fp = fp
                st.stable_count = 1

            # startup grace: 안정만 누적하고 발화 보류(커서 자리잡을 시간).
            if suppressed:
                return FireDecision(reason="startup_grace")

            # retry 예약: 즉시 재발화(busy만 막음 — 카드는 retry로 이미 해제됨).
            if st.retry_avoid is not None and not busy:
                avoid = st.retry_avoid
                target_id = st.retry_event_id
                st.retry_avoid = None
                st.retry_event_id = ""
                st.last_fired_fp = fp
                return FireDecision(
                    fire=True,
                    intervention=self._build_decision(
                        filtered, avoid_text=avoid, reason="retry", retry_event_id=target_id
                    ),
                    reason="retry",
                )

            # 생성 중이거나 미해결 카드 → 발화 보류.
            if busy or card_active:
                return FireDecision(reason="busy_or_card_active")

            # 같은 위치에 이미 제안 → dedup(커서/텍스트가 바뀌어야 재발화).
            if fp == st.last_fired_fp:
                return FireDecision(reason="already_suggested_here")

            # 안정 폴링 충족 → 발화.
            if st.stable_count >= self.stable_polls:
                st.last_fired_fp = fp
                return FireDecision(
                    fire=True,
                    intervention=self._build_decision(
                        filtered, avoid_text="", reason="caret_stable"
                    ),
                    reason="caret_stable",
                )

            return FireDecision(reason="waiting_for_stable")

    def _build_decision(
        self,
        filtered: FilteredScreenContext,
        *,
        avoid_text: str,
        reason: str,
        retry_event_id: str = "",
    ) -> InterventionDecision:
        # intervention_type은 이어쓰기 시나리오를 재사용한다 — dispatcher의
        # writing_context override + 프롬프트 guidance(continuation)를 그대로 탄다.
        metadata = {"engine": "caret_continuation", "trigger": reason}
        if avoid_text:
            metadata["avoid_text"] = str(avoid_text)
        if retry_event_id:
            metadata["retry_event_id"] = str(retry_event_id)
        return InterventionDecision(
            should_consider_llm=True,
            intervention_type="idle_after_writing",
            score=1.0,
            priority="high",
            reason_codes=[f"caret_continuation:{reason}"],
            metadata=metadata,
        )


__all__ = [
    "CaretContinuationEngine",
    "FireDecision",
    "DEFAULT_STABLE_POLLS",
    "DEFAULT_MIN_PREFIX_CHARS",
]
