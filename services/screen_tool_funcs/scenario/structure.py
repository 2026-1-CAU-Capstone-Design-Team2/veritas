"""문서 구조 형성 시나리오 — 헤딩/리스트/긴 문단/코드 블록 등 구조 요소 등장 시 트리거.

포함 시나리오 (Phase 4 Tier 1, 5개):
- OutlinePhaseScenario: 짧은 줄 + bullet 마커 다수 → 개요 항목 풀어쓰기 제안
- HeadingAddedScenario: 헤딩 마커(`#`, `1.`) 존재 → 섹션 내용 시작 도와줌
- LongParagraphWrittenScenario: 현재 문단 ≥500자 → 분리 제안
- NumberedListGrowthScenario: 번호 리스트 항목 ≥3개 → 다음 항목 제안
- CodeBlockPresentScenario: ``` fence 존재 → 코드 설명·검증 제안

모두 현재 캡처의 active_editor_text 또는 current_paragraph_text를 정규식으로
검사하는 단순 패턴 매칭. 캡처간 비교는 안 함.
"""
from __future__ import annotations

from typing import Any

from ..core.models import FilteredScreenContext
from ._shared import (
    _BULLET_LINE_RE,
    _CODE_FENCE_RE,
    _HEADING_RE,
    _NUMBERED_ITEM_RE,
)
from .base import ScenarioContext, ScenarioEvaluation, ScenarioType


class OutlinePhaseScenario(ScenarioType):
    """짧은 줄/줄바꿈이 잦은 개요 작성 단계 → 항목 풀어쓰기 제안."""

    name = "outline_phase"
    priority = "medium"

    def __init__(
        self,
        *,
        max_avg_line_chars: float = 60.0,
        min_lines: int = 5,
        min_short_line_ratio: float = 0.5,
        cooldown_min_seconds: float = 90.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(
            initial_vruntime=initial_vruntime,
            vruntime_increment=vruntime_increment,
        )
        self.max_avg_line_chars = max_avg_line_chars
        self.min_lines = min_lines
        self.min_short_line_ratio = min_short_line_ratio
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        outline = self._outline_status(context.filtered)
        outline_passed = bool(outline.get("passed"))
        time_cooldown = self._time_cooldown_status(context.last_fired_at)
        time_cooldown_passed = bool(time_cooldown.get("passed"))
        evaluation.gate_results = {
            "outline_shape": self._gate_result(
                outline_passed,
                "outline_shape_observed" if outline_passed else "not_outline_shaped",
                outline,
            ),
            "time_cooldown": self._gate_result(
                time_cooldown_passed,
                "time_cooldown_passed" if time_cooldown_passed else "time_cooldown_active",
                time_cooldown,
            ),
        }
        if outline_passed:
            evaluation.score += 0.5
            evaluation.reasons.append("outline_shape_observed")
        else:
            evaluation.blockers.append("not_outline_shaped")
        if time_cooldown_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"outline_status": outline, "time_cooldown": time_cooldown}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "full_document"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "outline_expand", "preferred_action": "expand_outline_item"}

    def _outline_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = (filtered.active_editor_text or "").strip()
        if not text:
            return {"passed": False, "reason": "empty_text", "lines": 0}
        lines = [ln for ln in text.split("\n") if ln.strip()]
        if len(lines) < self.min_lines:
            return {
                "passed": False,
                "reason": "too_few_lines",
                "lines": len(lines),
                "min_lines": self.min_lines,
            }
        avg_len = sum(len(ln) for ln in lines) / len(lines)
        short_lines = sum(1 for ln in lines if len(ln) <= self.max_avg_line_chars)
        short_ratio = short_lines / len(lines)
        bullet_count = sum(1 for ln in lines if _BULLET_LINE_RE.match(ln))
        passed = (avg_len <= self.max_avg_line_chars) and (short_ratio >= self.min_short_line_ratio)
        return {
            "passed": passed,
            "reason": "ok" if passed else "not_outline_shaped",
            "lines": len(lines),
            "avg_line_chars": round(avg_len, 1),
            "max_avg_line_chars": self.max_avg_line_chars,
            "short_line_ratio": round(short_ratio, 3),
            "min_short_line_ratio": self.min_short_line_ratio,
            "bullet_count": bullet_count,
        }


class HeadingAddedScenario(ScenarioType):
    """본문에 헤딩(Markdown # 또는 번호 헤딩) 존재 → 섹션 시작 도와줌."""

    name = "heading_added"
    priority = "medium"

    def __init__(
        self,
        *,
        max_headings: int = 3,
        cooldown_min_seconds: float = 120.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(
            initial_vruntime=initial_vruntime,
            vruntime_increment=vruntime_increment,
        )
        self.max_headings = max_headings
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        heading = self._heading_status(context.filtered)
        heading_passed = bool(heading.get("passed"))
        time_cooldown = self._time_cooldown_status(context.last_fired_at)
        time_cooldown_passed = bool(time_cooldown.get("passed"))
        evaluation.gate_results = {
            "heading_present": self._gate_result(
                heading_passed,
                "heading_present" if heading_passed else "no_heading",
                heading,
            ),
            "time_cooldown": self._gate_result(
                time_cooldown_passed,
                "time_cooldown_passed" if time_cooldown_passed else "time_cooldown_active",
                time_cooldown,
            ),
        }
        if heading_passed:
            evaluation.score += 0.5
            evaluation.reasons.append("heading_present")
        else:
            evaluation.blockers.append("no_heading")
        if time_cooldown_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"heading_status": heading, "time_cooldown": time_cooldown}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "section_kickoff", "preferred_action": "open_section"}

    def _heading_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        # 헤딩은 보통 별도 문단(본문 위)이므로 문서 전반에서 탐지
        text = (filtered.active_editor_text or filtered.current_paragraph_text or "").strip()
        if not text:
            return {"passed": False, "reason": "empty_text", "headings": []}
        matches = _HEADING_RE.findall(text)
        unique = matches[: self.max_headings]
        passed = bool(unique)
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_heading",
            "headings": unique,
            "count": len(unique),
        }


class LongParagraphWrittenScenario(ScenarioType):
    """현재 문단이 임계 이상 길어짐 → 분리 제안."""

    name = "long_paragraph_written"
    priority = "medium"

    def __init__(
        self,
        *,
        min_paragraph_chars: int = 500,
        cooldown_min_seconds: float = 120.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(
            initial_vruntime=initial_vruntime,
            vruntime_increment=vruntime_increment,
        )
        self.min_paragraph_chars = min_paragraph_chars
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        length = self._length_status(context.filtered)
        length_passed = bool(length.get("passed"))
        time_cooldown = self._time_cooldown_status(context.last_fired_at)
        time_cooldown_passed = bool(time_cooldown.get("passed"))
        evaluation.gate_results = {
            "long_paragraph": self._gate_result(
                length_passed,
                "long_paragraph_observed" if length_passed else "paragraph_not_long_enough",
                length,
            ),
            "time_cooldown": self._gate_result(
                time_cooldown_passed,
                "time_cooldown_passed" if time_cooldown_passed else "time_cooldown_active",
                time_cooldown,
            ),
        }
        if length_passed:
            evaluation.score += 0.5
            evaluation.reasons.append("long_paragraph_observed")
        else:
            evaluation.blockers.append("paragraph_not_long_enough")
        if time_cooldown_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"length_status": length, "time_cooldown": time_cooldown}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "structure_split", "preferred_action": "suggest_paragraph_break"}

    def _length_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        paragraph = " ".join((filtered.current_paragraph_text or "").split())
        chars = len(paragraph)
        passed = chars >= self.min_paragraph_chars
        return {
            "passed": passed,
            "reason": "ok" if passed else "paragraph_not_long_enough",
            "current_paragraph_chars": chars,
            "min_paragraph_chars": self.min_paragraph_chars,
        }


class NumberedListGrowthScenario(ScenarioType):
    """본문에 번호 리스트 항목 다수 → 다음 항목 제안."""

    name = "numbered_list_growth"
    priority = "medium"

    def __init__(
        self,
        *,
        min_items: int = 3,
        cooldown_min_seconds: float = 90.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(
            initial_vruntime=initial_vruntime,
            vruntime_increment=vruntime_increment,
        )
        self.min_items = min_items
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        list_status = self._list_status(context.filtered)
        list_passed = bool(list_status.get("passed"))
        time_cooldown = self._time_cooldown_status(context.last_fired_at)
        time_cooldown_passed = bool(time_cooldown.get("passed"))
        evaluation.gate_results = {
            "numbered_list": self._gate_result(
                list_passed,
                "numbered_list_observed" if list_passed else "no_numbered_list",
                list_status,
            ),
            "time_cooldown": self._gate_result(
                time_cooldown_passed,
                "time_cooldown_passed" if time_cooldown_passed else "time_cooldown_active",
                time_cooldown,
            ),
        }
        if list_passed:
            evaluation.score += 0.5
            evaluation.reasons.append("numbered_list_observed")
        else:
            evaluation.blockers.append("no_numbered_list")
        if time_cooldown_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"list_status": list_status, "time_cooldown": time_cooldown}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "full_document"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "list_extend", "preferred_action": "suggest_list_item"}

    def _list_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = filtered.active_editor_text or ""
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "items": 0}
        items = len(_NUMBERED_ITEM_RE.findall(text))
        passed = items >= self.min_items
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_numbered_list",
            "items": items,
            "min_items": self.min_items,
        }


class CodeBlockPresentScenario(ScenarioType):
    """본문에 코드 블록(``` fence) 존재 → 코드 설명·검증 제안."""

    name = "code_block_present"
    priority = "medium"

    def __init__(
        self,
        *,
        min_fences: int = 1,
        cooldown_min_seconds: float = 150.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(
            initial_vruntime=initial_vruntime,
            vruntime_increment=vruntime_increment,
        )
        self.min_fences = min_fences
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        code = self._code_status(context.filtered)
        code_passed = bool(code.get("passed"))
        time_cooldown = self._time_cooldown_status(context.last_fired_at)
        time_cooldown_passed = bool(time_cooldown.get("passed"))
        evaluation.gate_results = {
            "code_block": self._gate_result(
                code_passed,
                "code_block_present" if code_passed else "no_code_block",
                code,
            ),
            "time_cooldown": self._gate_result(
                time_cooldown_passed,
                "time_cooldown_passed" if time_cooldown_passed else "time_cooldown_active",
                time_cooldown,
            ),
        }
        if code_passed:
            evaluation.score += 0.5
            evaluation.reasons.append("code_block_present")
        else:
            evaluation.blockers.append("no_code_block")
        if time_cooldown_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"code_status": code, "time_cooldown": time_cooldown}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "full_document"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "code_review", "preferred_action": "comment_on_code"}

    def _code_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = filtered.active_editor_text or ""
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "fences": 0}
        fences = len(_CODE_FENCE_RE.findall(text))
        passed = fences >= self.min_fences
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_code_block",
            "fences": fences,
            "min_fences": self.min_fences,
        }
