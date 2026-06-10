"""CaretContinuationEngine — native-style 외부 앱 이어쓰기 엔진.

검증:
- cursor_scope가 N폴 안정 + cursor_located이면 즉시 발화(시나리오/dwell/CFS 없이).
- 같은 위치 dedup(재발화 안 함), 위치 바뀌면 새 발화.
- cursor_located=False면 발화 안 함(커서 모르면 제안 없음).
- busy / card_active면 보류.
- retry: 즉시 재발화 + avoid_text 전달(새 편집 없어도 — idle-gate 미사용).
"""
from __future__ import annotations

import unittest

from services.screen_tool_funcs.core.models import FilteredScreenContext
from services.screen_tool_funcs.intervention.caret_continuation import (
    CaretContinuationEngine,
)


PREFIX_A = "따라서 다음과 같은 조사 세션에 대한 전체 결론을 도출할 수 있습니다."
PREFIX_B = "또한 추가 분석을 통해 새로운 시사점을 얻을 수 있었습니다."


def _filtered(scope: str, *, located: bool = True) -> FilteredScreenContext:
    return FilteredScreenContext(
        active_editor_text=scope,
        cursor_scope_text=scope,
        cursor_located=located,
    )


class CaretContinuationEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.eng = CaretContinuationEngine(stable_polls=2, min_prefix_chars=20)

    def _obs(self, scope, *, located=True, busy=False, card=False, doc="doc", suppressed=False):
        return self.eng.observe(
            document_key=doc,
            filtered=_filtered(scope, located=located),
            busy=busy,
            card_active=card,
            suppressed=suppressed,
        )

    def test_fires_after_n_stable_polls(self) -> None:
        self.assertFalse(self._obs(PREFIX_A).fire)  # poll 1 — stable_count=1
        d = self._obs(PREFIX_A)  # poll 2 — stable_count=2 → fire
        self.assertTrue(d.fire)
        self.assertEqual(d.intervention.intervention_type, "idle_after_writing")
        self.assertEqual(d.reason, "caret_stable")

    def test_dedup_same_spot_no_refire(self) -> None:
        self._obs(PREFIX_A)
        self.assertTrue(self._obs(PREFIX_A).fire)
        # 같은 위치 계속 안정 → 재발화 안 함.
        self.assertFalse(self._obs(PREFIX_A).fire)
        self.assertFalse(self._obs(PREFIX_A).fire)

    def test_cursor_move_fires_again(self) -> None:
        self._obs(PREFIX_A)
        self.assertTrue(self._obs(PREFIX_A).fire)
        # 위치 변경(scope 다름) → 새 안정 누적 후 발화.
        self.assertFalse(self._obs(PREFIX_B).fire)  # count=1 at new spot
        self.assertTrue(self._obs(PREFIX_B).fire)  # count=2 → fire

    def test_not_located_never_fires(self) -> None:
        for _ in range(5):
            self.assertFalse(self._obs(PREFIX_A, located=False).fire)

    def test_short_prefix_never_fires(self) -> None:
        for _ in range(5):
            self.assertFalse(self._obs("짧음").fire)

    def test_busy_or_card_holds(self) -> None:
        self._obs(PREFIX_A)  # count=1
        self.assertFalse(self._obs(PREFIX_A, busy=True).fire)  # count=2 but busy
        self.assertFalse(self._obs(PREFIX_A, card=True).fire)  # card active
        # 풀리면 발화(이미 안정).
        self.assertTrue(self._obs(PREFIX_A).fire)

    def test_retry_fires_immediately_with_avoid_text(self) -> None:
        self._obs(PREFIX_A)
        self.assertTrue(self._obs(PREFIX_A).fire)  # 첫 제안
        self.assertFalse(self._obs(PREFIX_A).fire)  # dedup
        # "다시": 새 편집 없이도 즉시 재발화 + avoid_text + 원래 카드 id 재사용.
        self.eng.request_retry(
            "doc", avoid_text="이전 제안 문장입니다.", target_event_id="pd_orig"
        )
        d = self._obs(PREFIX_A)
        self.assertTrue(d.fire)
        self.assertEqual(d.reason, "retry")
        self.assertEqual(d.intervention.metadata.get("avoid_text"), "이전 제안 문장입니다.")
        # 원래 카드 id를 흘려보내 같은 카드 갱신(새 카드 X).
        self.assertEqual(d.intervention.metadata.get("retry_event_id"), "pd_orig")

    def test_retry_blocked_while_busy_then_fires(self) -> None:
        self._obs(PREFIX_A)
        self._obs(PREFIX_A)  # fired
        self.eng.request_retry("doc", avoid_text="x" * 10)
        self.assertFalse(self._obs(PREFIX_A, busy=True).fire)  # busy → 보류
        self.assertTrue(self._obs(PREFIX_A).fire)  # busy 풀리면 retry 발화

    def test_suppressed_holds_fire_then_fires_after_grace(self) -> None:
        # grace 동안: 안정 누적하되 발화 보류.
        self.assertFalse(self._obs(PREFIX_A, suppressed=True).fire)
        self.assertFalse(self._obs(PREFIX_A, suppressed=True).fire)  # stable=2지만 보류
        self.assertFalse(self._obs(PREFIX_A, suppressed=True).fire)
        # grace 해제 → 이미 안정이므로 곧바로 발화(dedup에 안 걸림).
        d = self._obs(PREFIX_A)
        self.assertTrue(d.fire)
        self.assertEqual(d.reason, "caret_stable")

    def test_per_document_independent(self) -> None:
        self._obs(PREFIX_A, doc="docA")
        self.assertTrue(self._obs(PREFIX_A, doc="docA").fire)
        # 다른 문서는 독립 — 같은 텍스트라도 자기 안정 누적 필요.
        self.assertFalse(self._obs(PREFIX_A, doc="docB").fire)
        self.assertTrue(self._obs(PREFIX_A, doc="docB").fire)


if __name__ == "__main__":
    unittest.main()
