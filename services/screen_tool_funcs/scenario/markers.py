"""마커 및 단순 패턴 시나리오 — 약어/TODO/질문 같은 식별 가능한 단순 마커 등장 시 트리거.

포함 시나리오 (Phase 4 Tier 1, 3개):
- AcronymIntroducedScenario: 대문자 약어 등장 → 정의 추가 제안
- TodoMarkerPresentScenario: TODO/FIXME/[?] 존재 → 미해결 작업 정리
- ManyQuestionMarksScenario: `?` 다수 → 조사할 질문 정리

모두 현재 캡처의 텍스트에 대한 단순 카운트/매칭. 캡처간 비교 없음.
"""
from __future__ import annotations

from typing import Any

from ..core.models import FilteredScreenContext
from ._shared import _ACRONYM_RE, _TODO_MARKER_RE
from .base import ScenarioContext, ScenarioEvaluation, ScenarioType


class AcronymIntroducedScenario(ScenarioType):
    """본문에 대문자 약어(3-5자) 등장 → 정의 추가 제안."""

    name = "acronym_introduced"
    priority = "medium"

    def __init__(
        self,
        *,
        max_acronyms: int = 5,
        cooldown_min_seconds: float = 75.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(
            initial_vruntime=initial_vruntime,
            vruntime_increment=vruntime_increment,
        )
        self.max_acronyms = max_acronyms
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        acronym = self._acronym_status(context.filtered)
        acronym_passed = bool(acronym.get("passed"))
        time_cooldown = self._time_cooldown_status(context.last_fired_at)
        time_cooldown_passed = bool(time_cooldown.get("passed"))
        evaluation.gate_results = {
            "acronym_present": self._gate_result(
                acronym_passed,
                "acronym_present" if acronym_passed else "no_acronym",
                acronym,
            ),
            "time_cooldown": self._gate_result(
                time_cooldown_passed,
                "time_cooldown_passed" if time_cooldown_passed else "time_cooldown_active",
                time_cooldown,
            ),
        }
        if acronym_passed:
            evaluation.score += 0.5
            evaluation.reasons.append("acronym_present")
        else:
            evaluation.blockers.append("no_acronym")
        if time_cooldown_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"acronym_status": acronym, "time_cooldown": time_cooldown}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "clarify", "preferred_action": "suggest_definition"}

    def _acronym_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        # 약어는 문단 단위가 아니라 문서 전반에서 탐지 (다른 문단에 정의돼 있을 수 있음)
        text = (filtered.active_editor_text or filtered.current_paragraph_text or "").strip()
        if not text:
            return {"passed": False, "reason": "empty_text", "acronyms": []}
        matches = _ACRONYM_RE.findall(text)
        unique = sorted(set(matches))[: self.max_acronyms]
        passed = bool(unique)
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_acronym",
            "acronyms": unique,
            "count": len(unique),
        }


class TodoMarkerPresentScenario(ScenarioType):
    """본문에 TODO/FIXME/[?] 마커 존재 → 미해결 작업 정리 제안."""

    name = "todo_marker_present"
    priority = "low"

    def __init__(
        self,
        *,
        max_markers: int = 10,
        cooldown_min_seconds: float = 150.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(
            initial_vruntime=initial_vruntime,
            vruntime_increment=vruntime_increment,
        )
        self.max_markers = max_markers
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        marker = self._marker_status(context.filtered)
        marker_passed = bool(marker.get("passed"))
        time_cooldown = self._time_cooldown_status(context.last_fired_at)
        time_cooldown_passed = bool(time_cooldown.get("passed"))
        evaluation.gate_results = {
            "todo_marker": self._gate_result(
                marker_passed,
                "todo_marker_present" if marker_passed else "no_todo_marker",
                marker,
            ),
            "time_cooldown": self._gate_result(
                time_cooldown_passed,
                "time_cooldown_passed" if time_cooldown_passed else "time_cooldown_active",
                time_cooldown,
            ),
        }
        if marker_passed:
            evaluation.score += 0.5
            evaluation.reasons.append("todo_marker_present")
        else:
            evaluation.blockers.append("no_todo_marker")
        if time_cooldown_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"marker_status": marker, "time_cooldown": time_cooldown}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "full_document"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "task_summary", "preferred_action": "summarize_todos"}

    def _marker_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = filtered.active_editor_text or ""
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "markers": []}
        # findall: 그룹 매치 시 그룹 내용 반환, [?] 분기 매치 시 빈 문자열 — 빈 값 제외
        matches = _TODO_MARKER_RE.findall(text)
        keywords = [m for m in matches if isinstance(m, str) and m]
        # [?] 발생 횟수 추적 — 전체 매치 수에서 키워드 수를 뺌
        bracket_question_count = len(matches) - len(keywords)
        unique_keywords = sorted(set(keywords))[: self.max_markers]
        markers = list(unique_keywords)
        if bracket_question_count > 0:
            markers.append("[?]")
        passed = bool(matches)
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_todo_marker",
            "markers": markers,
            "count": len(matches),
            "bracket_question_count": bracket_question_count,
        }


class ManyQuestionMarksScenario(ScenarioType):
    """현재 문단에 의문문이 다수 → 조사할 질문 정리 제안."""

    name = "many_question_marks"
    priority = "medium"

    def __init__(
        self,
        *,
        min_question_marks: int = 3,
        cooldown_min_seconds: float = 60.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(
            initial_vruntime=initial_vruntime,
            vruntime_increment=vruntime_increment,
        )
        self.min_question_marks = min_question_marks
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        question = self._question_status(context.filtered)
        question_passed = bool(question.get("passed"))
        time_cooldown = self._time_cooldown_status(context.last_fired_at)
        time_cooldown_passed = bool(time_cooldown.get("passed"))
        evaluation.gate_results = {
            "many_questions": self._gate_result(
                question_passed,
                "many_questions_observed" if question_passed else "not_enough_questions",
                question,
            ),
            "time_cooldown": self._gate_result(
                time_cooldown_passed,
                "time_cooldown_passed" if time_cooldown_passed else "time_cooldown_active",
                time_cooldown,
            ),
        }
        if question_passed:
            evaluation.score += 0.5
            evaluation.reasons.append("many_questions_observed")
        else:
            evaluation.blockers.append("not_enough_questions")
        if time_cooldown_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"question_status": question, "time_cooldown": time_cooldown}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "research_focus", "preferred_action": "highlight_key_questions"}

    def _question_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = (filtered.current_paragraph_text or filtered.active_editor_text or "")
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "question_marks": 0}
        count = text.count("?")
        passed = count >= self.min_question_marks
        return {
            "passed": passed,
            "reason": "ok" if passed else "not_enough_questions",
            "question_marks": count,
            "min_question_marks": self.min_question_marks,
        }
