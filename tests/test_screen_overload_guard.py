"""Regression tests: 외부 앱 실시간 보조의 카드 폭주 방지 장치.

"사용자가 한 단락을 수정하고 잠시 멈춘 사이 서로 다른 내용의 카드가 5~6개
쌓이는" 문제의 3중 방어선을 가드한다:

1. ScenarioScheduler — 적응형 발화 게이트(floor / base×multiplier / 새 내용
   조기 해제 / 반감기 감쇠) + 단락(fingerprint) 단위 cross-scenario cooldown.
   시나리오별 cooldown은 같은 시나리오의 재발화만 막아서, CFS 공정성이 같은
   단락에서 매번 *다른* 시나리오를 뽑아 발화하는 구멍이 있었다. 페이스는
   고정 벽시계가 아니라 카드 반응(수락↓/거절·무시↑)과 새 편집 유무가 결정.
2. UnresolvedCardGate — 표시된 카드에 사용자가 반응(또는 만료)하기 전에는
   캡처 루프가 새 개입을 스케줄하지 못한다. retry는 브레이크를 풀어 즉시
   재발화를 허용하고, 만료는 '무시' 신호로 페이스를 늦춘다.
3. SuggestionList — 같은 단락 카드는 교체, 전체 표시 상한 MAX_CARDS.
"""
from __future__ import annotations

import os
import tempfile
import time
import types
import unittest

from services.screen_tool_funcs.core.store import ScreenContextStore
from services.screen_tool_funcs.intervention.scenario_scheduler import (
    ScenarioScheduler,
    ScenarioSchedulerState,
    ScenarioWeights,
)
from services.screen_tool_funcs.screen_context_service import (
    ScreenContextService,
    UnresolvedCardGate,
)


def _make_scheduler(
    store: ScreenContextStore,
    *,
    fire_interval_floor_sec: float = 0.0,
    fire_interval_base_sec: float = 0.0,
    fire_interval_ceil_sec: float = 240.0,
    pace_decay_half_life_sec: float = 0.0,
    early_release_min_new_chars: int = 80,
    paragraph_cooldown_sec: float = 180.0,
) -> ScenarioScheduler:
    # floor=0 / base=0 이면 전역 발화 게이트가 항상 통과 — 단락 cooldown 등
    # 다른 게이트를 격리해서 검증할 때 쓰는 기본값.
    return ScenarioScheduler(
        store,
        weights={
            "scenario_a": ScenarioWeights(initial_vruntime=0.0, vruntime_increment=1.0),
            "scenario_b": ScenarioWeights(initial_vruntime=0.0, vruntime_increment=1.0),
        },
        fire_interval_floor_sec=fire_interval_floor_sec,
        fire_interval_base_sec=fire_interval_base_sec,
        fire_interval_ceil_sec=fire_interval_ceil_sec,
        pace_decay_half_life_sec=pace_decay_half_life_sec,
        early_release_min_new_chars=early_release_min_new_chars,
        paragraph_cooldown_sec=paragraph_cooldown_sec,
    )


def _intervention(
    event_id: str = "evt_1",
    *,
    legacy_event_id: str = "",
    document_key: str = "doc_key",
    paragraph_fingerprint: str = "fp_1",
    intervention_type: str = "idle_after_writing",
) -> dict:
    payload = {
        "event_id": event_id,
        "intervention_type": intervention_type,
        "app_context": {"document_key": document_key},
        "activity_context": {
            "document_key": document_key,
            "paragraph_fingerprint": paragraph_fingerprint,
        },
    }
    if legacy_event_id:
        payload["legacy_event_id"] = legacy_event_id
    return payload


class ParagraphCooldownTests(unittest.TestCase):
    """같은 단락에는 시나리오가 달라도 cooldown 동안 재발화하지 않는다."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ScreenContextStore(self._tmp.name)
        self.base = 1_000_000.0

    def test_same_paragraph_blocks_other_scenarios_within_cooldown(self) -> None:
        sched = _make_scheduler(self.store, paragraph_cooldown_sec=180.0)
        first = sched.select_and_charge(
            "doc", ["scenario_a"], now=self.base, paragraph_fingerprint="fp_1"
        )
        self.assertEqual(first, "scenario_a")

        trace: dict = {}
        second = sched.select_and_charge(
            "doc",
            ["scenario_b"],  # 다른 시나리오인데도
            now=self.base + 10.0,
            paragraph_fingerprint="fp_1",  # 같은 단락이면
            trace_out=trace,
        )
        self.assertIsNone(second)
        self.assertEqual(trace["rejected_reason"], "paragraph_cooldown")
        self.assertTrue(trace["paragraph_throttle"]["active"])

    def test_different_paragraph_is_not_blocked(self) -> None:
        sched = _make_scheduler(self.store, paragraph_cooldown_sec=180.0)
        sched.select_and_charge(
            "doc", ["scenario_a"], now=self.base, paragraph_fingerprint="fp_1"
        )
        other = sched.select_and_charge(
            "doc", ["scenario_b"], now=self.base + 10.0, paragraph_fingerprint="fp_2"
        )
        self.assertEqual(other, "scenario_b")

    def test_cooldown_expires(self) -> None:
        sched = _make_scheduler(self.store, paragraph_cooldown_sec=180.0)
        sched.select_and_charge(
            "doc", ["scenario_a"], now=self.base, paragraph_fingerprint="fp_1"
        )
        later = sched.select_and_charge(
            "doc", ["scenario_b"], now=self.base + 181.0, paragraph_fingerprint="fp_1"
        )
        self.assertEqual(later, "scenario_b")

    def test_router_path_record_fire_and_is_paragraph_throttled(self) -> None:
        sched = _make_scheduler(self.store, paragraph_cooldown_sec=180.0)
        sched.record_fire(
            "doc", "scenario_a", now=self.base, paragraph_fingerprint="fp_1"
        )
        self.assertTrue(
            sched.is_paragraph_throttled("doc", "fp_1", now=self.base + 10.0)
        )
        self.assertFalse(
            sched.is_paragraph_throttled("doc", "fp_2", now=self.base + 10.0)
        )
        self.assertFalse(
            sched.is_paragraph_throttled("doc", "fp_1", now=self.base + 181.0)
        )

    def test_allow_immediate_fire_lifts_brakes_for_retry(self) -> None:
        sched = _make_scheduler(
            self.store,
            fire_interval_floor_sec=20.0,
            fire_interval_base_sec=60.0,
            paragraph_cooldown_sec=180.0,
        )
        sched.select_and_charge(
            "doc", ["scenario_a"], now=self.base, paragraph_fingerprint="fp_1"
        )
        # 전역 throttle + 단락 cooldown + 시나리오 자체 기록이 모두 걸린 상태.
        blocked = sched.select_and_charge(
            "doc", ["scenario_a"], now=self.base + 5.0, paragraph_fingerprint="fp_1"
        )
        self.assertIsNone(blocked)

        sched.allow_immediate_fire(
            "doc", scenario_name="scenario_a", paragraph_fingerprint="fp_1"
        )
        retried = sched.select_and_charge(
            "doc", ["scenario_a"], now=self.base + 6.0, paragraph_fingerprint="fp_1"
        )
        self.assertEqual(retried, "scenario_a")

    def test_state_payload_roundtrip_keeps_paragraph_fires(self) -> None:
        state = ScenarioSchedulerState(
            document_key="doc",
            last_fired_paragraphs={"fp_1": self.base},
        )
        restored = ScenarioSchedulerState.from_payload(state.to_payload())
        self.assertEqual(restored.last_fired_paragraphs, {"fp_1": self.base})

    def test_paragraph_fire_history_is_bounded(self) -> None:
        sched = _make_scheduler(self.store, paragraph_cooldown_sec=180.0)
        cap = ScenarioScheduler._PARAGRAPH_FIRE_HISTORY_MAX
        for index in range(cap + 8):
            sched.record_fire(
                "doc",
                "scenario_a",
                now=self.base + index,
                paragraph_fingerprint=f"fp_{index}",
            )
        state = sched.get_state("doc", now=self.base + cap + 8)
        self.assertLessEqual(len(state.last_fired_paragraphs), cap)
        # 가장 최근 기록은 남는다.
        self.assertIn(f"fp_{cap + 7}", state.last_fired_paragraphs)


class AdaptivePacingTests(unittest.TestCase):
    """적응형 발화 게이트: floor / base×multiplier / 새 내용 조기 해제 / 감쇠."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ScreenContextStore(self._tmp.name)
        self.base = 1_000_000.0

    def _sched(self, **overrides) -> ScenarioScheduler:
        params = dict(
            fire_interval_floor_sec=20.0,
            fire_interval_base_sec=60.0,
            fire_interval_ceil_sec=240.0,
            pace_decay_half_life_sec=0.0,  # 감쇠 끔 — 결정적 검증
            early_release_min_new_chars=80,
            paragraph_cooldown_sec=0.0,  # 단락 게이트 격리
        )
        params.update(overrides)
        return _make_scheduler(self.store, **params)

    def _fire(self, sched: ScenarioScheduler, *, now: float, doc_chars: int = 1000) -> None:
        winner = sched.select_and_charge(
            "doc",
            ["scenario_a"],
            now=now,
            doc_chars=doc_chars,
            paragraph_fingerprint="fp_first",
        )
        assert winner == "scenario_a"

    def test_floor_blocks_even_with_new_content(self) -> None:
        sched = self._sched()
        self._fire(sched, now=self.base, doc_chars=1000)
        trace: dict = {}
        blocked = sched.select_and_charge(
            "doc",
            ["scenario_b"],
            now=self.base + 10.0,
            doc_chars=2000,  # 새 내용이 충분해도
            paragraph_fingerprint="fp_other",
            trace_out=trace,
        )
        self.assertIsNone(blocked)
        self.assertEqual(trace["rejected_reason"], "global_throttle")

    def test_adaptive_interval_blocks_without_new_content(self) -> None:
        sched = self._sched()
        self._fire(sched, now=self.base, doc_chars=1000)
        trace: dict = {}
        blocked = sched.select_and_charge(
            "doc",
            ["scenario_b"],
            now=self.base + 30.0,  # floor(20)는 지났지만 base(60) 미달
            doc_chars=1000,  # 새 내용 없음
            paragraph_fingerprint="fp_other",
            trace_out=trace,
        )
        self.assertIsNone(blocked)
        self.assertEqual(trace["rejected_reason"], "adaptive_interval")

    def test_new_content_releases_early(self) -> None:
        sched = self._sched()
        self._fire(sched, now=self.base, doc_chars=1000)
        trace: dict = {}
        winner = sched.select_and_charge(
            "doc",
            ["scenario_b"],
            now=self.base + 30.0,
            doc_chars=1100,  # +100자 ≥ 80 → 조기 해제
            paragraph_fingerprint="fp_other",
            trace_out=trace,
        )
        self.assertEqual(winner, "scenario_b")
        self.assertTrue(trace["global_throttle"]["early_release"])

    def test_interval_elapsed_allows_without_new_content(self) -> None:
        sched = self._sched()
        self._fire(sched, now=self.base, doc_chars=1000)
        winner = sched.select_and_charge(
            "doc",
            ["scenario_b"],
            now=self.base + 61.0,
            doc_chars=1000,
            paragraph_fingerprint="fp_other",
        )
        self.assertEqual(winner, "scenario_b")

    def test_reject_widens_and_accept_narrows_interval(self) -> None:
        sched = self._sched()
        self._fire(sched, now=self.base, doc_chars=1000)
        sched.record_card_outcome("doc", "reject", now=self.base + 1.0)
        # 거절 → multiplier 1.7 → 간격 60×1.7=102초.
        blocked = sched.select_and_charge(
            "doc", ["scenario_b"], now=self.base + 70.0,
            doc_chars=1000, paragraph_fingerprint="fp_other",
        )
        self.assertIsNone(blocked)
        winner = sched.select_and_charge(
            "doc", ["scenario_b"], now=self.base + 103.0,
            doc_chars=1000, paragraph_fingerprint="fp_other",
        )
        self.assertEqual(winner, "scenario_b")

        # 수락 → 1.7×0.6=1.02 → 간격 ~61초.
        sched.record_card_outcome("doc", "accept", now=self.base + 104.0)
        snapshot = sched.snapshot("doc", now=self.base + 104.0)
        self.assertAlmostEqual(
            snapshot["effective_fire_interval_sec"], 60.0 * 1.02, delta=0.5
        )

    def test_ignore_outcome_widens_interval(self) -> None:
        sched = self._sched()
        sched.record_card_outcome("doc", "ignore", now=self.base)
        snapshot = sched.snapshot("doc", now=self.base)
        self.assertAlmostEqual(
            snapshot["effective_fire_interval_sec"], 60.0 * 1.3, delta=0.5
        )

    def test_multiplier_decays_toward_base(self) -> None:
        sched = self._sched(pace_decay_half_life_sec=60.0)
        sched.record_card_outcome("doc", "reject", now=self.base)  # mult 1.7
        # 2 반감기 후: 1 + 0.7×0.25 = 1.175 → 간격 ~70.5초.
        snapshot = sched.snapshot("doc", now=self.base + 120.0)
        self.assertAlmostEqual(snapshot["fire_pace_multiplier"], 1.175, delta=0.01)
        self.assertAlmostEqual(
            snapshot["effective_fire_interval_sec"], 60.0 * 1.175, delta=0.5
        )

    def test_interval_clamped_to_ceil(self) -> None:
        sched = self._sched()
        for _ in range(8):  # 거절 연타 → multiplier가 ceil/base(=4.0)에서 clamp
            sched.record_card_outcome("doc", "reject", now=self.base)
        snapshot = sched.snapshot("doc", now=self.base)
        self.assertAlmostEqual(snapshot["effective_fire_interval_sec"], 240.0, delta=0.5)

    def test_router_path_global_gate_reason(self) -> None:
        sched = self._sched()
        self._fire(sched, now=self.base, doc_chars=1000)
        self.assertEqual(
            sched.global_gate_reason("doc", doc_chars=1000, now=self.base + 10.0),
            "global_throttle",
        )
        self.assertEqual(
            sched.global_gate_reason("doc", doc_chars=1000, now=self.base + 30.0),
            "adaptive_interval",
        )
        self.assertIsNone(
            sched.global_gate_reason("doc", doc_chars=1100, now=self.base + 30.0)
        )
        self.assertIsNone(
            sched.global_gate_reason("doc", doc_chars=1000, now=self.base + 61.0)
        )

    def test_pace_state_survives_payload_roundtrip(self) -> None:
        state = ScenarioSchedulerState(
            document_key="doc",
            fire_pace_multiplier=1.7,
            pace_updated_at=self.base,
            last_global_fire_doc_chars=1234,
            last_global_fire_paragraph_fp="fp_x",
        )
        restored = ScenarioSchedulerState.from_payload(state.to_payload())
        self.assertEqual(restored.fire_pace_multiplier, 1.7)
        self.assertEqual(restored.pace_updated_at, self.base)
        self.assertEqual(restored.last_global_fire_doc_chars, 1234)
        self.assertEqual(restored.last_global_fire_paragraph_fp, "fp_x")


class UnresolvedCardGateTests(unittest.TestCase):
    """표시된 카드가 미해결인 동안 게이트가 잠기고, 반응/만료로 풀린다."""

    def setUp(self) -> None:
        self.clock = [1_000.0]
        self.gate = UnresolvedCardGate(
            resolve_timeout_sec=90.0, now=lambda: self.clock[0]
        )

    def test_inactive_until_marked(self) -> None:
        self.assertFalse(self.gate.active())
        self.gate.mark_shown(_intervention("pd_1", legacy_event_id="evt_1"))
        self.assertTrue(self.gate.active())

    def test_resolve_by_either_id(self) -> None:
        self.gate.mark_shown(_intervention("pd_1", legacy_event_id="evt_1"))
        card = self.gate.resolve("evt_1")  # legacy id로도 해결돼야 함
        self.assertIsNotNone(card)
        self.assertEqual(card["document_key"], "doc_key")
        self.assertEqual(card["paragraph_fingerprint"], "fp_1")
        self.assertFalse(self.gate.active())

        self.gate.mark_shown(_intervention("pd_2", legacy_event_id="evt_2"))
        self.assertIsNotNone(self.gate.resolve("pd_2"))
        self.assertFalse(self.gate.active())

    def test_unknown_id_does_not_resolve(self) -> None:
        self.gate.mark_shown(_intervention("pd_1"))
        self.assertIsNone(self.gate.resolve("pd_other"))
        self.assertTrue(self.gate.active())

    def test_auto_expiry_after_timeout(self) -> None:
        self.gate.mark_shown(_intervention("pd_1"))
        self.clock[0] += 89.0
        self.assertTrue(self.gate.active())
        self.clock[0] += 2.0
        self.assertFalse(self.gate.active())

    def test_streaming_remark_keeps_first_shown_at(self) -> None:
        self.gate.mark_shown(_intervention("pd_1"))
        self.clock[0] += 60.0
        # 같은 카드의 스트리밍 갱신은 shown_at을 연장하지 않는다.
        self.gate.mark_shown(_intervention("pd_1"))
        self.clock[0] += 31.0  # 첫 표시로부터 91초
        self.assertFalse(self.gate.active())

    def test_empty_event_id_is_noop(self) -> None:
        self.gate.mark_shown({"event_id": "", "app_context": {}})
        self.assertFalse(self.gate.active())

    def test_poll_returns_expired_card_exactly_once(self) -> None:
        self.gate.mark_shown(_intervention("pd_1"))
        self.clock[0] += 91.0
        active, expired = self.gate.poll()
        self.assertFalse(active)
        self.assertIsNotNone(expired)
        self.assertEqual(expired["document_key"], "doc_key")
        # 두 번째 poll에서는 더 이상 만료 카드가 안 나온다 (ignore 신호 1회성).
        active, expired = self.gate.poll()
        self.assertFalse(active)
        self.assertIsNone(expired)


class ResolveCardRetryTests(unittest.TestCase):
    """service.resolve_card: retry는 caret-continuation 엔진에 즉시 재발화를
    예약(avoid_text 포함)하고, 다른 반응은 예약하지 않는다."""

    def setUp(self) -> None:
        from services.screen_tool_funcs.intervention.caret_continuation import (
            CaretContinuationEngine,
        )

        self.gate = UnresolvedCardGate(resolve_timeout_sec=90.0)
        self.engine = CaretContinuationEngine(stable_polls=2, min_prefix_chars=10)
        # ScreenContextService 전체를 띄우지 않고 resolve_card가 의존하는 두
        # 협력자만 가진 가짜 self로 unbound 메서드를 검증한다.
        self.service_like = types.SimpleNamespace(
            unresolved_card_gate=self.gate,
            continuation_engine=self.engine,
        )

    def _show_card(self) -> None:
        self.gate.mark_shown(
            _intervention("pd_1", legacy_event_id="evt_1"),
            answer="이전에 제안된 긴 문장입니다.",
        )

    def _filtered(self, scope: str):
        from services.screen_tool_funcs.core.models import FilteredScreenContext

        return FilteredScreenContext(
            active_editor_text=scope, cursor_scope_text=scope, cursor_located=True
        )

    def test_retry_schedules_immediate_refire_with_avoid_text(self) -> None:
        self._show_card()
        resolved = ScreenContextService.resolve_card(
            self.service_like, "pd_1", feedback_action="retry"
        )
        self.assertTrue(resolved)
        self.assertFalse(self.gate.active())
        # 엔진은 새 편집(안정 누적) 없이도 즉시 재발화하며 직전 제안을 avoid로 넘긴다.
        decision = self.engine.observe(
            document_key="doc_key",
            filtered=self._filtered("이 단락의 충분히 긴 커서 앞 텍스트입니다."),
            busy=False,
            card_active=False,
        )
        self.assertTrue(decision.fire)
        self.assertEqual(decision.reason, "retry")
        self.assertEqual(
            decision.intervention.metadata.get("avoid_text"),
            "이전에 제안된 긴 문장입니다.",
        )
        # 원래 카드 id(pd_1)를 재사용해 같은 카드를 갱신한다.
        self.assertEqual(decision.intervention.metadata.get("retry_event_id"), "pd_1")

    def test_reject_does_not_schedule_retry(self) -> None:
        self._show_card()
        resolved = ScreenContextService.resolve_card(
            self.service_like, "evt_1", feedback_action="red_reject"
        )
        self.assertTrue(resolved)
        self.assertFalse(self.gate.active())
        # retry 예약 없음 → 즉시 재발화 안 함(안정 누적 전엔 발화 X).
        decision = self.engine.observe(
            document_key="doc_key",
            filtered=self._filtered("이 단락의 충분히 긴 커서 앞 텍스트입니다."),
            busy=False,
            card_active=False,
        )
        self.assertFalse(decision.fire)

    def test_unknown_card_returns_false(self) -> None:
        resolved = ScreenContextService.resolve_card(
            self.service_like, "pd_missing", feedback_action="retry"
        )
        self.assertFalse(resolved)


# ---------------------------------------------------------------- frontend

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    _APP = QApplication.instance() or QApplication([])
    _QT_OK = True
except Exception:  # pragma: no cover - no Qt / no offscreen platform
    _QT_OK = False


@unittest.skipUnless(_QT_OK, "PySide6 offscreen QApplication unavailable")
class SuggestionListCardPolicyTests(unittest.TestCase):
    """카드 UI 정책: 최신 최상단 이력 누적 / 거절 제거 / 다시 placeholder."""

    def _make_list(self):
        from frontend.ui.windows.document_assist_window import SuggestionList

        return SuggestionList()

    def _ids(self, suggestion_list) -> list[str]:
        # _suggestions는 newest-first(index 0 = 최상단).
        return [str(item.get("id") or "") for item in suggestion_list._suggestions]

    def test_newest_on_top_history_accumulates(self) -> None:
        slist = self._make_list()
        for i in range(4):
            slist.upsert_suggestion(f"pd_{i}", "실시간 보조", f"제안 {i}")
        # 최신(pd_3)이 맨 위, 지난 제안은 아래로 누적(교체 없음).
        self.assertEqual(self._ids(slist), ["pd_3", "pd_2", "pd_1", "pd_0"])
        self.assertEqual(len(slist._cards), 4)

    def test_streaming_update_same_event_id_updates_in_place(self) -> None:
        slist = self._make_list()
        slist.upsert_suggestion("pd_1", "실시간 보조", "부분")
        slist.upsert_suggestion("pd_1", "실시간 보조", "부분 + 더")
        self.assertEqual(self._ids(slist), ["pd_1"])
        self.assertEqual(slist._suggestions[0]["text"], "부분 + 더")

    def test_max_cards_cap_drops_oldest_at_bottom(self) -> None:
        slist = self._make_list()
        total = slist.MAX_CARDS + 3
        for i in range(total):
            slist.upsert_suggestion(f"pd_{i}", "실시간 보조", f"제안 {i}")
        self.assertEqual(len(slist._cards), slist.MAX_CARDS)
        # 최신 MAX_CARDS개만, newest-first. 가장 오래된 pd_0..pd_2는 떨어져 나감.
        expected = [f"pd_{i}" for i in range(total - 1, total - 1 - slist.MAX_CARDS, -1)]
        self.assertEqual(self._ids(slist), expected)

    def test_set_suggestions_newest_first(self) -> None:
        slist = self._make_list()
        # 입력은 시간순(오래된→최신).
        items = [
            {"id": f"pd_{i}", "category": "c", "text": f"t{i}", "tone": "working"}
            for i in range(3)
        ]
        slist.set_suggestions(items)
        self.assertEqual(self._ids(slist), ["pd_2", "pd_1", "pd_0"])

    def test_reject_removes_card(self) -> None:
        slist = self._make_list()
        slist.upsert_suggestion("pd_1", "실시간 보조", "첫")
        slist.upsert_suggestion("pd_2", "실시간 보조", "둘")
        # 거절 피드백 → 해당 카드 제거.
        slist._on_card_feedback("pd_1", "idle_after_writing", "red_reject")
        self.assertEqual(self._ids(slist), ["pd_2"])
        self.assertNotIn("pd_1", slist._cards_by_id)

    def test_retry_shows_regenerating_placeholder(self) -> None:
        slist = self._make_list()
        slist.upsert_suggestion("pd_1", "실시간 보조", "원래 제안 본문입니다.")
        card = slist._cards_by_id["pd_1"]
        card._on_rate("retry")
        self.assertIn("재제안 중", card._body.text())
        self.assertEqual(card._copy_value, "")

    def test_streaming_growth_resyncs_card_height(self) -> None:
        # 스트리밍으로 본문이 길어질 때 카드 높이가 재계산돼 잘리지 않아야 한다.
        slist = self._make_list()
        slist.upsert_suggestion("pd_1", "실시간 보조", "짧은 첫 청크.")
        card = slist._cards_by_id["pd_1"]
        card.set_card_width(300)
        short_h = card.height()
        long_text = "이것은 매우 긴 제안 본문입니다. " * 8
        slist.upsert_suggestion("pd_1", "실시간 보조", long_text)
        self.assertGreater(card.height(), short_h)


if __name__ == "__main__":
    unittest.main()
