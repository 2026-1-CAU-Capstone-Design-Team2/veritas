"""캡처간 편집 변화 시나리오 — same_document_events를 비교해 사용자의 편집 패턴 감지.

포함 시나리오 (Phase 4 Tier 2-B, 4개):
- ScatteredEditsScenario: 여러 위치 작은 편집 (paragraph_churn 보색) → 일관성 점검
- LargeDeletionScenario: 직전 캡처 대비 큰 삭제 (≥100자) → 백업 제안
- CopyPasteGrowthScenario: 직전 캡처 대비 큰 추가 (≥200자, paste 의심) → 정리/통합
- UndoCycleDetectedScenario: A→B→A 3캡처 진동 → 원안 정착 제안

모든 트리거가 ScenarioContext.same_document_events를 비교 분석. 단일 캡처 텍스트만으론
판정 불가능한 시나리오들.
"""
from __future__ import annotations

import difflib
from typing import Any

from ._shared import _event_paragraph_fingerprint, _norm_active_text
from .base import ScenarioContext, ScenarioEvaluation, ScenarioType


class ScatteredEditsScenario(ScenarioType):
    """여러 위치에 분산된 작은 편집 → 일관성 점검 제안 (paragraph_churn 보색)."""

    name = "scattered_edits"
    priority = "medium"

    def __init__(
        self,
        *,
        window: int = 5,
        min_changed_captures: int = 3,
        max_capture_delta: int = 30,
        cooldown_min_seconds: float = 300.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.window = window
        self.min_changed_captures = min_changed_captures
        self.max_capture_delta = max_capture_delta
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        scattered = self._scattered_status(context.same_document_events)
        passed = bool(scattered.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "scattered_edits": self._gate_result(
                passed, "scattered_edits_observed" if passed else "no_scattered_edits", scattered,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("scattered_edits_observed")
        else:
            evaluation.blockers.append("no_scattered_edits")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"scattered_status": scattered, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "full_document"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "consistency", "preferred_action": "consistency_pass"}

    def _scattered_status(self, same_document_events: list[dict[str, Any]]) -> dict[str, Any]:
        events = same_document_events[-self.window:]
        if len(events) < self.min_changed_captures + 1:
            return {
                "passed": False,
                "reason": "insufficient_history",
                "captures": len(events),
            }
        changed_captures = 0
        distinct_fingerprints: set[str] = set()
        prev_text = _norm_active_text(events[0])
        for ev in events[1:]:
            curr_text = _norm_active_text(ev)
            delta = abs(len(curr_text) - len(prev_text))
            if 0 < delta <= self.max_capture_delta:
                changed_captures += 1
                fp = str(_event_paragraph_fingerprint(ev) or "")
                if fp:
                    distinct_fingerprints.add(fp)
            prev_text = curr_text
        # 핵심 조건: changed_captures가 임계 이상 + 여러 다른 paragraph_fingerprint
        passed = (changed_captures >= self.min_changed_captures
                  and len(distinct_fingerprints) >= 2)
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_scattered_edits",
            "captures": len(events),
            "changed_captures": changed_captures,
            "min_changed_captures": self.min_changed_captures,
            "distinct_fingerprints": len(distinct_fingerprints),
            "max_capture_delta": self.max_capture_delta,
        }


class LargeDeletionScenario(ScenarioType):
    """직전 캡처 대비 한 번에 큰 삭제 → 백업/복원 제안."""

    name = "large_deletion"
    priority = "medium"

    def __init__(
        self,
        *,
        min_deletion_chars: int = 100,
        cooldown_min_seconds: float = 180.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.min_deletion_chars = min_deletion_chars
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        deletion = self._deletion_status(context.same_document_events)
        passed = bool(deletion.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "large_deletion": self._gate_result(
                passed, "large_deletion_observed" if passed else "no_large_deletion", deletion,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("large_deletion_observed")
        else:
            evaluation.blockers.append("no_large_deletion")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"deletion_status": deletion, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "backup", "preferred_action": "offer_backup"}

    def _deletion_status(self, same_document_events: list[dict[str, Any]]) -> dict[str, Any]:
        if len(same_document_events) < 2:
            return {"passed": False, "reason": "insufficient_history", "delta": 0}
        prev_chars = len(_norm_active_text(same_document_events[-2]))
        curr_chars = len(_norm_active_text(same_document_events[-1]))
        delta = prev_chars - curr_chars
        passed = delta >= self.min_deletion_chars
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_large_deletion",
            "deleted_chars": delta,
            "prev_chars": prev_chars,
            "curr_chars": curr_chars,
            "min_deletion_chars": self.min_deletion_chars,
        }


class CopyPasteGrowthScenario(ScenarioType):
    """직전 캡처 대비 한 번에 큰 텍스트 추가(paste 의심) → 정리/통합 제안."""

    name = "copy_paste_growth"
    priority = "medium"

    def __init__(
        self,
        *,
        min_growth_chars: int = 200,
        cooldown_min_seconds: float = 240.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.min_growth_chars = min_growth_chars
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        growth = self._growth_status(context.same_document_events)
        passed = bool(growth.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "copy_paste_growth": self._gate_result(
                passed, "copy_paste_growth_observed" if passed else "no_copy_paste_growth", growth,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("copy_paste_growth_observed")
        else:
            evaluation.blockers.append("no_copy_paste_growth")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"growth_status": growth, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "integrate", "preferred_action": "integrate_pasted_content"}

    def _growth_status(self, same_document_events: list[dict[str, Any]]) -> dict[str, Any]:
        if len(same_document_events) < 2:
            return {"passed": False, "reason": "insufficient_history", "delta": 0}
        prev_chars = len(_norm_active_text(same_document_events[-2]))
        curr_chars = len(_norm_active_text(same_document_events[-1]))
        delta = curr_chars - prev_chars
        passed = delta >= self.min_growth_chars
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_copy_paste_growth",
            "added_chars": delta,
            "prev_chars": prev_chars,
            "curr_chars": curr_chars,
            "min_growth_chars": self.min_growth_chars,
        }


class UndoCycleDetectedScenario(ScenarioType):
    """A→B→A 형태의 텍스트 진동 → 원안 정착 제안."""

    name = "undo_cycle_detected"
    priority = "medium"

    def __init__(
        self,
        *,
        similarity_threshold: float = 0.98,
        cooldown_min_seconds: float = 240.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.similarity_threshold = similarity_threshold
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        undo = self._undo_status(context.same_document_events)
        passed = bool(undo.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "undo_cycle": self._gate_result(
                passed, "undo_cycle_observed" if passed else "no_undo_cycle", undo,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("undo_cycle_observed")
        else:
            evaluation.blockers.append("no_undo_cycle")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"undo_status": undo, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "settle", "preferred_action": "resolve_undo_cycle"}

    def _undo_status(self, same_document_events: list[dict[str, Any]]) -> dict[str, Any]:
        if len(same_document_events) < 3:
            return {"passed": False, "reason": "insufficient_history", "captures": len(same_document_events)}
        text_a = _norm_active_text(same_document_events[-3])
        text_b = _norm_active_text(same_document_events[-2])
        text_c = _norm_active_text(same_document_events[-1])
        if not text_a or not text_b or not text_c:
            return {"passed": False, "reason": "empty_capture", "captures": 3}
        sim_ab = difflib.SequenceMatcher(None, text_a, text_b).ratio()
        sim_bc = difflib.SequenceMatcher(None, text_b, text_c).ratio()
        sim_ac = difflib.SequenceMatcher(None, text_a, text_c).ratio()
        # A와 C는 닮음, B는 다름 → A→B→A 진동
        passed = (sim_ac >= self.similarity_threshold
                  and sim_ab < self.similarity_threshold
                  and sim_bc < self.similarity_threshold)
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_undo_cycle",
            "sim_ab": round(sim_ab, 3),
            "sim_bc": round(sim_bc, 3),
            "sim_ac": round(sim_ac, 3),
            "similarity_threshold": self.similarity_threshold,
        }
