from __future__ import annotations

import difflib
import hashlib
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .models import FilteredScreenContext, WindowContext


def _event_document_key(event: dict[str, Any]) -> str:
    """Derive document_key from any event shape (current snapshot or disk event)."""
    direct = str(event.get("document_key") or "").strip()
    if direct:
        return direct
    intervention = event.get("intervention") or {}
    metadata = intervention.get("metadata") or {}
    nested = str(metadata.get("document_key") or "").strip()
    if nested:
        return nested
    window = event.get("window") or {}
    process_name = str(window.get("process_name") or "").lower()
    title = " ".join(str(window.get("window_title") or "").split()).lower()
    title = re.sub(r"\s+", " ", title).strip()
    return f"{process_name}|{title}"


def _event_paragraph_fingerprint(event: dict[str, Any]) -> str:
    direct = str(event.get("paragraph_fingerprint") or "").strip()
    if direct:
        return direct
    intervention = event.get("intervention") or {}
    metadata = intervention.get("metadata") or {}
    nested = str(metadata.get("paragraph_fingerprint") or "").strip()
    if nested:
        return nested
    filtered = event.get("filtered") or {}
    text = str(filtered.get("current_paragraph_text") or "")
    normalized = " ".join(text.split()).strip().lower()[:500]
    if not normalized:
        return ""
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


@dataclass
class ScenarioContext:
    """Snapshot of inputs shared across scenarios within one capture cycle."""

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
    """Uniform per-scenario evaluation result."""

    name: str
    ready: bool = False
    score: float = 0.0
    priority: str = "low"
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    gate_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class ScenarioType(ABC):
    """Abstract base for intervention scenarios.

    A scenario is a TCB-like object holding its own gate functions, priority,
    and CFS scheduling parameters. evaluate() returns a uniform
    ScenarioEvaluation that the detector can compare against others.
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
        """priority 문자열에서 (initial_vruntime, vruntime_increment) default를 반환.
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
        """Run scenario-specific gates and return a uniform evaluation."""

    def writing_context_overrides(
        self,
        *,
        filtered: FilteredScreenContext,
        base: dict[str, Any],
    ) -> dict[str, Any]:
        """Return partial fields the dispatcher should merge into writing_context.

        Default: no overrides. Subclasses can replace `focus_scope`,
        `recent_sentences`, `focused_sentence`, etc. when this scenario fires.
        """
        return {}

    def tool_routing_hint_overrides(
        self,
        *,
        event: Any,
        base: dict[str, Any],
        focused_sentence: str,
    ) -> dict[str, Any]:
        """Return partial fields the dispatcher should merge into tool_routing_hint.

        Default: no overrides. Subclasses set `tone`, `preferred_action`, or
        merge into `signals` when this scenario fires.
        """
        return {}

    def _gate_result(
        self,
        passed: bool,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
        """현재 문단(`filtered.current_paragraph_text`)이 `min_chars` 이상인지 판정.
        문단 단위 시나리오가 자기 게이트로 호출하는 공유 헬퍼.
        """
        paragraph = " ".join((filtered.current_paragraph_text or "").split())
        return len(paragraph) >= min_chars

    def _time_cooldown_status(
        self,
        last_fired_at: dict[str, float],
        *,
        min_seconds: float | None = None,
    ) -> dict[str, Any]:
        """last_fired_at[self.name] 경과 시간이 min_seconds 이상이면 통과하는 시간 cooldown.
        min_seconds=None이면 self.cooldown_min_seconds를 사용.
        반환: {passed, reason, elapsed_seconds(있을 때), min_seconds}.
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


class IdleAfterWritingScenario(ScenarioType):
    """User wrote, then paused on the same paragraph -> request gentle continuation."""

    name = "idle_after_writing"
    priority = "medium"
    initial_vruntime = 0.0
    vruntime_increment = 1.0

    def __init__(
        self,
        *,
        min_paragraph_chars: int = 20,
        min_changed_chars: int = 10,
        min_idle_captures: int = 2,
        idle_similarity_threshold: float = 0.985,
        cooldown_events: int = 3,
        cooldown_min_seconds: float = 60.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(
            initial_vruntime=initial_vruntime,
            vruntime_increment=vruntime_increment,
        )
        self.min_paragraph_chars = min_paragraph_chars
        self.min_changed_chars = min_changed_chars
        self.min_idle_captures = min_idle_captures
        self.idle_similarity_threshold = idle_similarity_threshold
        self.cooldown_events = cooldown_events
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)

        typing_pause = self._typing_pause_status(context.same_document_events)
        typing_pause_passed = bool(typing_pause.get("ready"))
        cooldown_passed = self._passes_paragraph_cooldown(
            history_events=context.history_events,
            document_key=context.document_key,
            paragraph_fingerprint=context.paragraph_fingerprint,
        )
        time_cooldown = self._time_cooldown_status(context.last_fired_at)
        time_cooldown_passed = bool(time_cooldown.get("passed"))
        # 현재 문단 길이 게이트 — 문단 단위 시나리오가 자기 책임으로 확인
        paragraph_chars = len(
            " ".join((context.filtered.current_paragraph_text or "").split())
        )
        substantial_paragraph_passed = self._has_substantial_paragraph(
            context.filtered, min_chars=self.min_paragraph_chars
        )

        evaluation.gate_results = {
            "typing_pause": self._gate_result(
                typing_pause_passed,
                "typing_pause_satisfied" if typing_pause_passed else "not_paused_after_typing",
                typing_pause,
            ),
            "paragraph_cooldown": self._gate_result(
                cooldown_passed,
                "cooldown_dedupe_passed" if cooldown_passed else "cooldown_or_duplicate",
                {
                    "cooldown_events": self.cooldown_events,
                    "document_key": context.document_key,
                    "paragraph_fingerprint": context.paragraph_fingerprint,
                },
            ),
            "time_cooldown": self._gate_result(
                time_cooldown_passed,
                "time_cooldown_passed" if time_cooldown_passed else "time_cooldown_active",
                time_cooldown,
            ),
            "substantial_paragraph": self._gate_result(
                substantial_paragraph_passed,
                "substantial_paragraph" if substantial_paragraph_passed else "paragraph_too_short",
                {
                    "current_paragraph_chars": paragraph_chars,
                    "min_paragraph_chars": self.min_paragraph_chars,
                },
            ),
        }

        if typing_pause_passed:
            evaluation.score += 0.5
            evaluation.reasons.append("typing_pause_satisfied")
        else:
            evaluation.blockers.append("not_paused_after_typing")

        if cooldown_passed:
            evaluation.score += 0.3
            evaluation.reasons.append("cooldown_dedupe_passed")
        else:
            evaluation.blockers.append("cooldown_or_duplicate")

        # 시간 cooldown은 prereq: 점수 없이 blocker에만 기여
        if time_cooldown_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")

        # 점수 없는 순수 prerequisite: blocker에만 기여
        if substantial_paragraph_passed:
            evaluation.reasons.append("substantial_paragraph")
        else:
            evaluation.blockers.append("paragraph_too_short")

        evaluation.ready = not evaluation.blockers
        evaluation.priority = "high" if evaluation.ready and evaluation.score >= 0.7 else "medium"
        evaluation.metadata = {"typing_pause": typing_pause, "time_cooldown": time_cooldown}
        return evaluation

    def writing_context_overrides(
        self,
        *,
        filtered: FilteredScreenContext,
        base: dict[str, Any],
    ) -> dict[str, Any]:
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(
        self,
        *,
        event: Any,
        base: dict[str, Any],
        focused_sentence: str,
    ) -> dict[str, Any]:
        current_paragraph = (event.filtered.current_paragraph_text or "").strip()
        needs_research = bool((base.get("signals") or {}).get("research_needed"))
        preferred_action = "provide_supporting_material" if needs_research else "continue_writing"
        if len(current_paragraph) < self.min_paragraph_chars and not focused_sentence:
            preferred_action = "no_action"
        return {
            "tone": "gentle_continuation",
            "preferred_action": preferred_action,
        }

    def _typing_pause_status(self, same_document_events: list[dict[str, Any]]) -> dict[str, Any]:
        current_text = self._normalized_active_text(
            same_document_events[-1] if same_document_events else {}
        )
        if len(current_text) < self.min_paragraph_chars:
            return {
                "ready": False,
                "reason": "current_text_too_short",
                "stable_capture_count": 0,
                "min_idle_captures": self.min_idle_captures,
                "current_text_chars": len(current_text),
                "prior_text_chars": 0,
            }

        stable_capture_count = 0
        last_similarity = 1.0
        last_length_delta = 0
        for event in reversed(same_document_events):
            event_text = self._normalized_active_text(event)
            stable, similarity, length_delta = self._is_same_idle_text(event_text, current_text)
            last_similarity = similarity
            last_length_delta = length_delta
            if not stable:
                break
            stable_capture_count += 1

        prior_index = len(same_document_events) - stable_capture_count - 1
        has_prior_text_event = prior_index >= 0
        prior_text = (
            self._normalized_active_text(same_document_events[prior_index])
            if has_prior_text_event
            else ""
        )
        changed_before_pause = (
            has_prior_text_event and self._meaningful_text_change(prior_text, current_text)
        )
        ready = stable_capture_count >= self.min_idle_captures and changed_before_pause
        reason = "ready" if ready else "waiting_for_idle_captures"
        if stable_capture_count >= self.min_idle_captures and not changed_before_pause:
            reason = "no_recent_text_change_before_pause"

        return {
            "ready": ready,
            "reason": reason,
            "stable_capture_count": stable_capture_count,
            "min_idle_captures": self.min_idle_captures,
            "idle_similarity_threshold": self.idle_similarity_threshold,
            "last_similarity": round(last_similarity, 4),
            "last_length_delta": last_length_delta,
            "changed_before_pause": changed_before_pause,
            "current_text_chars": len(current_text),
            "prior_text_chars": len(prior_text),
        }

    def _passes_paragraph_cooldown(
        self,
        *,
        history_events: list[dict[str, Any]],
        document_key: str,
        paragraph_fingerprint: str,
    ) -> bool:
        if not paragraph_fingerprint:
            return False
        for event in reversed(history_events[-self.cooldown_events:]):
            intervention = event.get("intervention") or {}
            if not intervention.get("should_consider_llm"):
                continue
            if _event_document_key(event) != document_key:
                continue
            if _event_paragraph_fingerprint(event) == paragraph_fingerprint:
                return False
        return True

    def _normalized_active_text(self, event: dict[str, Any]) -> str:
        filtered = event.get("filtered") or {}
        text = str(filtered.get("active_editor_text") or "")
        return " ".join(text.split()).strip()

    def _meaningful_text_change(self, previous: str, current: str) -> bool:
        if not current:
            return False
        if not previous:
            return len(current) >= self.min_paragraph_chars
        if current == previous:
            return False
        if current.startswith(previous):
            return len(current) - len(previous) >= self.min_changed_chars
        if abs(len(current) - len(previous)) >= self.min_changed_chars:
            return True
        return difflib.SequenceMatcher(None, previous, current).ratio() < 0.98

    def _is_same_idle_text(self, previous: str, current: str) -> tuple[bool, float, int]:
        if previous == current:
            return True, 1.0, 0
        if not previous or not current:
            return False, 0.0, abs(len(current) - len(previous))
        length_delta = abs(len(current) - len(previous))
        max_noise_chars = max(3, int(len(current) * 0.015))
        if length_delta > max_noise_chars:
            return False, 0.0, length_delta
        similarity = difflib.SequenceMatcher(None, previous, current).ratio()
        return similarity >= self.idle_similarity_threshold, similarity, length_delta


class WholeDocumentReviewScenario(ScenarioType):
    """Sustained heavy writing followed by a pause -> review-the-whole-thing assist.

    Rarer than idle_after_writing, so the scheduler ranks it with a lower
    initial vruntime (picked first when both are ready), but it carries a
    heavier vruntime increment so consecutive triggers are throttled by CFS.
    """

    name = "whole_document_review"
    priority = "high"
    initial_vruntime = -10.0
    vruntime_increment = 5.0
    review_char_limit = 6000

    def __init__(
        self,
        *,
        sustained_window: int = 8,
        sustained_min_added_chars: int = 300,
        sustained_min_active_captures: int = 4,
        idle_after_sustained_captures: int = 2,
        idle_similarity_threshold: float = 0.97,
        cooldown_min_seconds: float = 300.0,
        cooldown_min_added_chars: int = 200,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(
            initial_vruntime=initial_vruntime,
            vruntime_increment=vruntime_increment,
        )
        self.sustained_window = sustained_window
        self.sustained_min_added_chars = sustained_min_added_chars
        self.sustained_min_active_captures = sustained_min_active_captures
        self.idle_after_sustained_captures = idle_after_sustained_captures
        self.idle_similarity_threshold = idle_similarity_threshold
        self.cooldown_min_seconds = cooldown_min_seconds
        self.cooldown_min_added_chars = cooldown_min_added_chars

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)

        sustained = self._sustained_writing_status(context.same_document_events)
        sustained_passed = bool(sustained.get("passed"))
        idle = self._idle_after_sustained_status(context.same_document_events)
        idle_passed = bool(idle.get("passed"))
        cooldown = self._document_cooldown_status(
            last_fired_at=context.last_fired_at,
            last_fired_doc_chars=context.last_fired_doc_chars,
            current_chars=len(
                self._normalized_active_text(
                    context.same_document_events[-1] if context.same_document_events else {}
                )
            ),
        )
        cooldown_passed = bool(cooldown.get("passed"))

        evaluation.gate_results = {
            "sustained_writing": self._gate_result(
                sustained_passed,
                "sustained_writing_observed" if sustained_passed else "insufficient_sustained_writing",
                sustained,
            ),
            "idle_after_sustained": self._gate_result(
                idle_passed,
                "idle_after_sustained_writing" if idle_passed else "not_idle_after_sustained_writing",
                idle,
            ),
            "document_cooldown": self._gate_result(
                cooldown_passed,
                "document_cooldown_passed" if cooldown_passed else "document_cooldown_active",
                cooldown,
            ),
        }

        if sustained_passed:
            evaluation.score += 0.4
            evaluation.reasons.append("sustained_writing_observed")
        else:
            evaluation.blockers.append("insufficient_sustained_writing")

        if idle_passed:
            evaluation.score += 0.3
            evaluation.reasons.append("idle_after_sustained_writing")
        else:
            evaluation.blockers.append("not_idle_after_sustained_writing")

        if cooldown_passed:
            evaluation.score += 0.2
            evaluation.reasons.append("document_cooldown_passed")
        else:
            evaluation.blockers.append("document_cooldown_active")

        evaluation.ready = not evaluation.blockers
        evaluation.priority = "high" if evaluation.ready else "medium"
        evaluation.metadata = {
            "sustained_writing": sustained,
            "idle_after_sustained": idle,
            "document_cooldown": cooldown,
        }
        return evaluation

    def writing_context_overrides(
        self,
        *,
        filtered: FilteredScreenContext,
        base: dict[str, Any],
    ) -> dict[str, Any]:
        review_text = self._build_review_text(filtered.active_editor_text)
        return {
            "focus_scope": "full_document",
            "recent_sentences": review_text,
            "focused_sentence": "",
            "full_document_excerpt": review_text,
        }

    def tool_routing_hint_overrides(
        self,
        *,
        event: Any,
        base: dict[str, Any],
        focused_sentence: str,
    ) -> dict[str, Any]:
        return {
            "tone": "comprehensive_review",
            "preferred_action": "review_whole_document",
        }

    def _build_review_text(self, text: str) -> str:
        normalized = " ".join(str(text or "").split()).strip()
        limit = self.review_char_limit
        if len(normalized) <= limit:
            return (
                "Full document review requested. Review the complete visible "
                f"document below:\n{normalized}"
            )
        head_limit = limit // 2
        tail_limit = limit - head_limit
        return (
            "Full document review requested. The visible document is too long, "
            "so review this beginning/end excerpt and mention that the middle "
            f"was omitted. Full document chars={len(normalized)}.\n"
            f"[BEGINNING]\n{normalized[:head_limit]}\n"
            f"[END]\n{normalized[-tail_limit:]}"
        )

    def _sustained_writing_status(
        self,
        same_document_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        events = same_document_events[-self.sustained_window:]
        if len(events) < self.sustained_min_active_captures:
            return {
                "passed": False,
                "reason": "insufficient_window_history",
                "window_size": len(events),
                "window_capacity": self.sustained_window,
                "added_chars": 0,
                "active_captures": 0,
                "min_added_chars": self.sustained_min_added_chars,
                "min_active_captures": self.sustained_min_active_captures,
            }

        added_chars = 0
        active_captures = 0
        previous_text = ""
        for index, event in enumerate(events):
            text = self._normalized_active_text(event)
            if index == 0:
                previous_text = text
                continue
            if text and text != previous_text:
                if text.startswith(previous_text):
                    delta = len(text) - len(previous_text)
                else:
                    delta = max(len(text) - len(previous_text), 0)
                if delta > 0:
                    added_chars += delta
                    active_captures += 1
            previous_text = text

        passed = (
            added_chars >= self.sustained_min_added_chars
            and active_captures >= self.sustained_min_active_captures
        )
        return {
            "passed": passed,
            "reason": "ok" if passed else "below_thresholds",
            "window_size": len(events),
            "window_capacity": self.sustained_window,
            "added_chars": added_chars,
            "active_captures": active_captures,
            "min_added_chars": self.sustained_min_added_chars,
            "min_active_captures": self.sustained_min_active_captures,
        }

    def _idle_after_sustained_status(
        self,
        same_document_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not same_document_events:
            return {
                "passed": False,
                "reason": "no_history",
                "stable_capture_count": 0,
                "min_captures": self.idle_after_sustained_captures,
            }
        current_text = self._normalized_active_text(same_document_events[-1])
        stable_capture_count = 0
        last_similarity = 1.0
        for event in reversed(same_document_events):
            event_text = self._normalized_active_text(event)
            if not event_text or not current_text:
                break
            if event_text == current_text:
                stable_capture_count += 1
                continue
            similarity = difflib.SequenceMatcher(None, event_text, current_text).ratio()
            last_similarity = similarity
            if similarity < self.idle_similarity_threshold:
                break
            stable_capture_count += 1

        passed = stable_capture_count >= self.idle_after_sustained_captures
        return {
            "passed": passed,
            "reason": "ok" if passed else "still_typing",
            "stable_capture_count": stable_capture_count,
            "min_captures": self.idle_after_sustained_captures,
            "last_similarity": round(last_similarity, 4),
            "current_text_chars": len(current_text),
        }

    def _document_cooldown_status(
        self,
        *,
        last_fired_at: dict[str, float],
        last_fired_doc_chars: dict[str, int],
        current_chars: int,
    ) -> dict[str, Any]:
        """직전 발동 시각(`last_fired_at`)과 그 시점의 문서 길이
        (`last_fired_doc_chars`)를 받아, 경과 시간이 `cooldown_min_seconds`
        이상이고 추가된 글자 수가 `cooldown_min_added_chars` 이상이면 통과.
        """
        last_at = last_fired_at.get(self.name)
        if last_at is None:
            return {
                "passed": True,
                "reason": "no_prior_review",
                "min_seconds": self.cooldown_min_seconds,
                "min_added_chars": self.cooldown_min_added_chars,
            }

        previous_chars = last_fired_doc_chars.get(self.name, 0)
        added_chars = max(current_chars - previous_chars, 0)
        elapsed_seconds = max(time.time() - last_at, 0.0)
        time_ok = elapsed_seconds >= self.cooldown_min_seconds
        chars_ok = added_chars >= self.cooldown_min_added_chars
        passed = time_ok and chars_ok
        return {
            "passed": passed,
            "reason": "ok" if passed else "cooldown_active",
            "elapsed_seconds": round(elapsed_seconds, 1),
            "added_chars_since_last": added_chars,
            "min_seconds": self.cooldown_min_seconds,
            "min_added_chars": self.cooldown_min_added_chars,
            "time_ok": time_ok,
            "chars_ok": chars_ok,
        }

    def _normalized_active_text(self, event: dict[str, Any]) -> str:
        filtered = event.get("filtered") or {}
        text = str(filtered.get("active_editor_text") or "")
        return " ".join(text.split()).strip()


class LongStaticReviewScenario(ScenarioType):
    """Editor left open and unchanged for a long stretch -> proofread + suggest.

    Covers the case where the user has finished writing or is re-reading their
    own text without editing. Unlike idle_after_writing / whole_document_review,
    this scenario fires precisely when *no* writing activity is observed: the
    `prolonged_static` gate requires the document text to stay unchanged across
    several consecutive captures. The other two scenarios block themselves in
    that same situation (no change-before-pause, no chars added), so the ready
    sets do not collide in practice.

    Scheduled with a high initial vruntime so that, on the rare capture where it
    shares the ready set with another scenario, CFS picks it last. Re-firing on
    the same document is throttled by the `review_cooldown` gate, since when
    this scenario is the only ready one it would otherwise be selected on every
    capture.
    """

    name = "long_static_review"
    priority = "low"
    initial_vruntime = 10.0
    vruntime_increment = 3.0
    review_char_limit = 6000

    def __init__(
        self,
        *,
        min_static_captures: int = 3,
        min_document_chars: int = 200,
        idle_similarity_threshold: float = 0.99,
        cooldown_min_seconds: float = 240.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(
            initial_vruntime=initial_vruntime,
            vruntime_increment=vruntime_increment,
        )
        self.min_static_captures = min_static_captures
        self.min_document_chars = min_document_chars
        self.idle_similarity_threshold = idle_similarity_threshold
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)

        static = self._prolonged_static_status(context.same_document_events)
        static_passed = bool(static.get("passed"))
        cooldown = self._time_cooldown_status(context.last_fired_at)
        cooldown_passed = bool(cooldown.get("passed"))

        evaluation.gate_results = {
            "prolonged_static": self._gate_result(
                static_passed,
                "prolonged_static_observed" if static_passed else "not_static_long_enough",
                static,
            ),
            "review_cooldown": self._gate_result(
                cooldown_passed,
                "review_cooldown_passed" if cooldown_passed else "review_cooldown_active",
                cooldown,
            ),
        }

        if static_passed:
            evaluation.score += 0.5
            evaluation.reasons.append("prolonged_static_observed")
        else:
            evaluation.blockers.append("not_static_long_enough")

        if cooldown_passed:
            evaluation.score += 0.3
            evaluation.reasons.append("review_cooldown_passed")
        else:
            evaluation.blockers.append("review_cooldown_active")

        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {
            "prolonged_static": static,
            "review_cooldown": cooldown,
        }
        return evaluation

    def writing_context_overrides(
        self,
        *,
        filtered: FilteredScreenContext,
        base: dict[str, Any],
    ) -> dict[str, Any]:
        review_text = self._build_review_text(filtered.active_editor_text)
        return {
            "focus_scope": "full_document",
            "recent_sentences": review_text,
            "focused_sentence": "",
            "full_document_excerpt": review_text,
        }

    def tool_routing_hint_overrides(
        self,
        *,
        event: Any,
        base: dict[str, Any],
        focused_sentence: str,
    ) -> dict[str, Any]:
        return {
            "tone": "proofreading_review",
            "preferred_action": "review_whole_document",
        }

    def _build_review_text(self, text: str) -> str:
        normalized = " ".join(str(text or "").split()).strip()
        limit = self.review_char_limit
        if len(normalized) <= limit:
            return (
                "The user has kept this document open without editing for a "
                "while and may be re-reading it. Proofread the document below, "
                "point out typos or awkward phrasing, and suggest what to write "
                f"next or what to add:\n{normalized}"
            )
        head_limit = limit // 2
        tail_limit = limit - head_limit
        return (
            "The user has kept this document open without editing for a while "
            "and may be re-reading it. The document is too long, so proofread "
            "this beginning/end excerpt, point out typos or awkward phrasing, "
            "and suggest what to add. Mention that the middle was omitted. "
            f"Full document chars={len(normalized)}.\n"
            f"[BEGINNING]\n{normalized[:head_limit]}\n"
            f"[END]\n{normalized[-tail_limit:]}"
        )

    def _prolonged_static_status(
        self,
        same_document_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not same_document_events:
            return {
                "passed": False,
                "reason": "no_history",
                "static_capture_count": 0,
                "min_static_captures": self.min_static_captures,
            }

        current_text = self._normalized_active_text(same_document_events[-1])
        if len(current_text) < self.min_document_chars:
            return {
                "passed": False,
                "reason": "document_too_short",
                "static_capture_count": 0,
                "min_static_captures": self.min_static_captures,
                "current_text_chars": len(current_text),
                "min_document_chars": self.min_document_chars,
            }

        static_capture_count = 0
        last_similarity = 1.0
        for event in reversed(same_document_events):
            event_text = self._normalized_active_text(event)
            is_static, similarity = self._is_static_text(event_text, current_text)
            last_similarity = similarity
            if not is_static:
                break
            static_capture_count += 1

        passed = static_capture_count >= self.min_static_captures
        return {
            "passed": passed,
            "reason": "ok" if passed else "not_static_long_enough",
            "static_capture_count": static_capture_count,
            "min_static_captures": self.min_static_captures,
            "idle_similarity_threshold": self.idle_similarity_threshold,
            "last_similarity": round(last_similarity, 4),
            "current_text_chars": len(current_text),
            "min_document_chars": self.min_document_chars,
        }


    def _normalized_active_text(self, event: dict[str, Any]) -> str:
        filtered = event.get("filtered") or {}
        text = str(filtered.get("active_editor_text") or "")
        return " ".join(text.split()).strip()

    def _is_static_text(self, previous: str, current: str) -> tuple[bool, float]:
        if previous == current:
            return True, 1.0
        if not previous or not current:
            return False, 0.0
        length_delta = abs(len(current) - len(previous))
        max_noise_chars = max(2, int(len(current) * 0.01))
        if length_delta > max_noise_chars:
            return False, 0.0
        similarity = difflib.SequenceMatcher(None, previous, current).ratio()
        return similarity >= self.idle_similarity_threshold, similarity


class ParagraphChurnScenario(ScenarioType):
    """현재 문단을 작은 편집으로 계속 만지작거리는 '막힘' 상태 -> 대안 표현 제안.

    idle_after_writing(멈춤)·whole_document_review(지속 대량작성)·
    long_static_review(정적) 어디에도 안 걸리는 중간 상태를 노린다 — 텍스트가 매
    캡처 조금씩 바뀌지만 순 진전이 거의 없는 구간. `small_churn` 게이트는 윈도우
    내 변경 캡처가 많고, 캡처당 변화량이 작고, 누적 순변화도 작을 때만 통과한다.
    """

    name = "paragraph_churn"
    priority = "medium"
    initial_vruntime = 3.0
    vruntime_increment = 2.0

    def __init__(
        self,
        *,
        churn_window: int = 6,
        min_changed_captures: int = 3,
        max_capture_delta: int = 15,
        max_net_change: int = 25,
        min_paragraph_chars: int = 20,
        cooldown_min_seconds: float = 150.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(
            initial_vruntime=initial_vruntime,
            vruntime_increment=vruntime_increment,
        )
        self.churn_window = churn_window
        self.min_changed_captures = min_changed_captures
        self.max_capture_delta = max_capture_delta
        self.max_net_change = max_net_change
        self.min_paragraph_chars = min_paragraph_chars
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)

        churn = self._small_churn_status(context.same_document_events)
        churn_passed = bool(churn.get("passed"))
        cooldown = self._time_cooldown_status(context.last_fired_at)
        cooldown_passed = bool(cooldown.get("passed"))
        # 문단 단위 시나리오의 공유 prereq — 현재 문단이 개입할 만큼 충분한 길이인지
        substantial_passed = self._has_substantial_paragraph(
            context.filtered, min_chars=self.min_paragraph_chars
        )

        evaluation.gate_results = {
            "small_churn": self._gate_result(
                churn_passed,
                "small_churn_observed" if churn_passed else "not_churning",
                churn,
            ),
            "churn_cooldown": self._gate_result(
                cooldown_passed,
                "churn_cooldown_passed" if cooldown_passed else "churn_cooldown_active",
                cooldown,
            ),
            "substantial_paragraph": self._gate_result(
                substantial_passed,
                "substantial_paragraph" if substantial_passed else "paragraph_too_short",
                {
                    "current_paragraph_chars": len(
                        " ".join((context.filtered.current_paragraph_text or "").split())
                    ),
                    "min_paragraph_chars": self.min_paragraph_chars,
                },
            ),
        }

        if churn_passed:
            evaluation.score += 0.5
            evaluation.reasons.append("small_churn_observed")
        else:
            evaluation.blockers.append("not_churning")

        if cooldown_passed:
            evaluation.score += 0.3
            evaluation.reasons.append("churn_cooldown_passed")
        else:
            evaluation.blockers.append("churn_cooldown_active")

        # 점수 없는 순수 prerequisite: blocker에만 기여
        if substantial_passed:
            evaluation.reasons.append("substantial_paragraph")
        else:
            evaluation.blockers.append("paragraph_too_short")

        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {
            "small_churn": churn,
            "churn_cooldown": cooldown,
        }
        return evaluation

    def writing_context_overrides(
        self,
        *,
        filtered: FilteredScreenContext,
        base: dict[str, Any],
    ) -> dict[str, Any]:
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(
        self,
        *,
        event: Any,
        base: dict[str, Any],
        focused_sentence: str,
    ) -> dict[str, Any]:
        return {
            "tone": "unstick",
            "preferred_action": "revise_current_paragraph",
        }

    def _small_churn_status(
        self,
        same_document_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        events = same_document_events[-self.churn_window:]
        if len(events) < self.min_changed_captures + 1:
            return {
                "passed": False,
                "reason": "insufficient_window_history",
                "window_size": len(events),
                "min_changed_captures": self.min_changed_captures,
            }

        changed_captures = 0
        max_delta = 0
        previous_text = self._normalized_active_text(events[0])
        first_len = len(previous_text)
        for event in events[1:]:
            text = self._normalized_active_text(event)
            if text != previous_text:
                changed_captures += 1
                max_delta = max(max_delta, abs(len(text) - len(previous_text)))
            previous_text = text
        net_change = abs(len(previous_text) - first_len)

        passed = (
            changed_captures >= self.min_changed_captures
            and max_delta <= self.max_capture_delta
            and net_change <= self.max_net_change
        )
        return {
            "passed": passed,
            "reason": "ok" if passed else "not_churning",
            "window_size": len(events),
            "changed_captures": changed_captures,
            "max_capture_delta": max_delta,
            "net_change": net_change,
            "min_changed_captures": self.min_changed_captures,
            "max_capture_delta_limit": self.max_capture_delta,
            "max_net_change": self.max_net_change,
        }

    def _normalized_active_text(self, event: dict[str, Any]) -> str:
        filtered = event.get("filtered") or {}
        text = str(filtered.get("active_editor_text") or "")
        return " ".join(text.split()).strip()


class BlankDocumentStartScenario(ScenarioType):
    """거의 빈 문서를 열고 머물러 있는 상태 -> 시작 구조/방향 제안.

    `near_empty_document` 게이트는 최근 연속 캡처가 모두 거의 비어 있을 때만
    통과한다. 사용자가 타이핑을 시작하면 더는 '빈 문서'가 아니므로 자연 종료된다.
    다른 시나리오가 없을 때만 마지막으로 선택되도록 높은 initial vruntime을 둔다.
    """

    name = "blank_document_start"
    priority = "low"
    initial_vruntime = 8.0
    vruntime_increment = 2.0

    def __init__(
        self,
        *,
        max_document_chars: int = 30,
        min_blank_captures: int = 3,
        cooldown_min_seconds: float = 600.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(
            initial_vruntime=initial_vruntime,
            vruntime_increment=vruntime_increment,
        )
        self.max_document_chars = max_document_chars
        self.min_blank_captures = min_blank_captures
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)

        blank = self._near_empty_status(context.same_document_events)
        blank_passed = bool(blank.get("passed"))
        cooldown = self._time_cooldown_status(context.last_fired_at)
        cooldown_passed = bool(cooldown.get("passed"))

        evaluation.gate_results = {
            "near_empty_document": self._gate_result(
                blank_passed,
                "near_empty_observed" if blank_passed else "document_not_empty",
                blank,
            ),
            "start_cooldown": self._gate_result(
                cooldown_passed,
                "start_cooldown_passed" if cooldown_passed else "start_cooldown_active",
                cooldown,
            ),
        }

        if blank_passed:
            evaluation.score += 0.5
            evaluation.reasons.append("near_empty_observed")
        else:
            evaluation.blockers.append("document_not_empty")

        if cooldown_passed:
            evaluation.score += 0.3
            evaluation.reasons.append("start_cooldown_passed")
        else:
            evaluation.blockers.append("start_cooldown_active")

        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {
            "near_empty_document": blank,
            "start_cooldown": cooldown,
        }
        return evaluation

    def writing_context_overrides(
        self,
        *,
        filtered: FilteredScreenContext,
        base: dict[str, Any],
    ) -> dict[str, Any]:
        return {"focus_scope": "full_document"}

    def tool_routing_hint_overrides(
        self,
        *,
        event: Any,
        base: dict[str, Any],
        focused_sentence: str,
    ) -> dict[str, Any]:
        return {
            "tone": "kickoff",
            "preferred_action": "continue_writing",
        }

    def _near_empty_status(
        self,
        same_document_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        events = same_document_events[-self.min_blank_captures:]
        if len(events) < self.min_blank_captures:
            return {
                "passed": False,
                "reason": "insufficient_window_history",
                "blank_capture_count": 0,
                "min_blank_captures": self.min_blank_captures,
            }

        blank_capture_count = 0
        last_chars = 0
        for event in events:
            last_chars = len(self._normalized_active_text(event))
            if last_chars <= self.max_document_chars:
                blank_capture_count += 1

        passed = blank_capture_count >= self.min_blank_captures
        return {
            "passed": passed,
            "reason": "ok" if passed else "document_not_empty",
            "blank_capture_count": blank_capture_count,
            "min_blank_captures": self.min_blank_captures,
            "current_text_chars": last_chars,
            "max_document_chars": self.max_document_chars,
        }

    def _normalized_active_text(self, event: dict[str, Any]) -> str:
        filtered = event.get("filtered") or {}
        text = str(filtered.get("active_editor_text") or "")
        return " ".join(text.split()).strip()


# ============================================================
# Phase 4 — Tier 1 시나리오 (현재 캡처 payload만으로 판정)
# ============================================================

# ASCII lookaround로 한국어 조사 뒤에서도 매치 + 3자+ 강제로 OK/PT 같은 false positive 차단
_ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{2,4}(?![A-Za-z0-9])")
_HEADING_RE = re.compile(r"(?:^|\n)\s*(#{1,6}\s+\S|\d+[.)]\s+\S)")
_NUMBERED_ITEM_RE = re.compile(r"(?:^|\n)\s*\d+[.)]\s+\S")
_TODO_MARKER_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b|\[\s*\?\s*\]")
_CODE_FENCE_RE = re.compile(r"(?:^|\n)\s*```")
_BULLET_LINE_RE = re.compile(r"^\s*([-*•]|\d+[.)])\s+")


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
        cooldown_min_seconds: float = 180.0,
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


class AcronymIntroducedScenario(ScenarioType):
    """본문에 대문자 2-5자 약어 등장 → 정의 추가 제안."""

    name = "acronym_introduced"
    priority = "medium"

    def __init__(
        self,
        *,
        max_acronyms: int = 5,
        cooldown_min_seconds: float = 300.0,
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


class HeadingAddedScenario(ScenarioType):
    """본문 시작 부근에 헤딩(Markdown # 또는 번호 헤딩) 존재 → 섹션 시작 도와줌."""

    name = "heading_added"
    priority = "medium"

    def __init__(
        self,
        *,
        max_headings: int = 3,
        cooldown_min_seconds: float = 240.0,
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
        # 헤딩은 보통 별도 문단(보통 본문 위)이므로 문서 전반에서 탐지
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
        cooldown_min_seconds: float = 240.0,
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
        cooldown_min_seconds: float = 180.0,
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


class TodoMarkerPresentScenario(ScenarioType):
    """본문에 TODO/FIXME/[?] 마커 존재 → 미해결 작업 정리 제안."""

    name = "todo_marker_present"
    priority = "low"

    def __init__(
        self,
        *,
        max_markers: int = 10,
        cooldown_min_seconds: float = 600.0,
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
        cooldown_min_seconds: float = 240.0,
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


class CodeBlockPresentScenario(ScenarioType):
    """본문에 코드 블록(``` fence) 존재 → 코드 설명·검증 제안."""

    name = "code_block_present"
    priority = "medium"

    def __init__(
        self,
        *,
        min_fences: int = 1,
        cooldown_min_seconds: float = 300.0,
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


# ============================================================
# Phase 4 — Tier 2 시나리오 (텍스트 패턴 또는 캡처간 diff)
# ============================================================

# --- 2-A 텍스트 패턴용 사전/regex ---

# 한국어 접속어 — transition_word_overuse 사전
KO_TRANSITION_WORDS: tuple[str, ...] = (
    "그러나", "하지만", "또한", "그리고", "그래서", "따라서",
    "한편", "반면", "오히려", "그렇지만", "더불어",
)
# 한국어 약한 강조어 — weak_modifier_overuse 사전
KO_WEAK_MODIFIERS: tuple[str, ...] = (
    "매우", "정말", "아주", "굉장히", "되게", "엄청", "너무", "꽤",
)
# 기술 분야 jargon starter (도메인 맞춰 확장 가능)
KO_TECH_JARGON: tuple[str, ...] = (
    "API", "Kubernetes", "Docker", "백엔드", "프론트엔드", "엔드포인트",
    "쿼리", "스키마", "토큰", "캐싱", "마이크로서비스", "런타임",
    "컨테이너", "오케스트레이션", "디플로이먼트",
)

# 큰따옴표(ASCII/Korean curly/CJK 「」/『』) 안 20자+ 인용
_QUOTE_RE = re.compile(
    r'(?:[\"“”«][^\"“”«»]{20,}[\"“”»])'  # ASCII/curly/guillemet
    r'|(?:「[^」]{20,}」)'                  # CJK 단일 인용부
    r'|(?:『[^』]{20,}』)'                  # CJK 이중 인용부
)
# 통계/년도 패턴 — 숫자+단위(ASCII or 한국어) 또는 19xx/20xx 년도.
# \b가 한국어와 비호환이므로 lookaround로 ASCII 경계만 강제, 한국어 단위는 그냥 매치.
_STATISTIC_RE = re.compile(
    r'(?<![A-Za-z0-9])\d+(?:\.\d+)?(?:%|퍼센트|배|kg|km|만|억|조|개)'
    r'|(?<!\d)(?:19|20)\d{2}년?(?!\d)'
)
# 인용 마커 — [1], [Document X], (저자, 2023)
_CITATION_MARKER_RE = re.compile(r'\[\d+\]|\[Document\s+[^\]]+\]|\([가-힣A-Za-z]+(?:\s*외)?,\s*\d{4}\)')


def _norm_active_text(event: dict[str, Any]) -> str:
    """이벤트의 active_editor_text를 공백 정규화. 캡처간 diff 시나리오의 공유 헬퍼."""
    filtered = event.get("filtered") or {}
    return " ".join(str(filtered.get("active_editor_text") or "").split()).strip()


class QuoteInsertedScenario(ScenarioType):
    """본문에 큰따옴표 인용이 등장 → 출처/귀속 제안."""

    name = "quote_inserted"
    priority = "medium"

    def __init__(
        self,
        *,
        min_quote_chars: int = 20,
        cooldown_min_seconds: float = 300.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.min_quote_chars = min_quote_chars
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        quote = self._quote_status(context.filtered)
        passed = bool(quote.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "quote_present": self._gate_result(
                passed, "quote_present" if passed else "no_quote", quote,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("quote_present")
        else:
            evaluation.blockers.append("no_quote")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"quote_status": quote, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "attribution_check", "preferred_action": "suggest_attribution"}

    def _quote_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = filtered.active_editor_text or ""
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "quotes": 0}
        matches = _QUOTE_RE.findall(text)
        passed = bool(matches)
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_quote",
            "quotes": len(matches),
            "min_quote_chars": self.min_quote_chars,
        }


class CitationMissingScenario(ScenarioType):
    """통계/년도 등 사실 주장이 있는데 인용 마커가 부재 → 근거 보강 제안."""

    name = "citation_missing"
    priority = "medium"

    def __init__(
        self,
        *,
        min_statistics: int = 2,
        cooldown_min_seconds: float = 300.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.min_statistics = min_statistics
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        cite = self._citation_status(context.filtered)
        passed = bool(cite.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "citation_missing": self._gate_result(
                passed, "citation_missing_observed" if passed else "no_citation_gap", cite,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("citation_missing_observed")
        else:
            evaluation.blockers.append("no_citation_gap")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"citation_status": cite, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "full_document"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "evidence_check", "preferred_action": "request_citation"}

    def _citation_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = filtered.active_editor_text or ""
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "statistics": 0, "citations": 0}
        stat_count = len(_STATISTIC_RE.findall(text))
        cite_count = len(_CITATION_MARKER_RE.findall(text))
        passed = stat_count >= self.min_statistics and cite_count == 0
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_citation_gap",
            "statistics": stat_count,
            "citations": cite_count,
            "min_statistics": self.min_statistics,
        }


class FactualClaimMadeScenario(ScenarioType):
    """본문에 통계/년도 등 사실 주장이 등장 → 검증 도와줌."""

    name = "factual_claim_made"
    priority = "medium"

    def __init__(
        self,
        *,
        min_statistics: int = 1,
        cooldown_min_seconds: float = 240.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.min_statistics = min_statistics
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        claim = self._claim_status(context.filtered)
        passed = bool(claim.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "factual_claim": self._gate_result(
                passed, "factual_claim_observed" if passed else "no_factual_claim", claim,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("factual_claim_observed")
        else:
            evaluation.blockers.append("no_factual_claim")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"claim_status": claim, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "verify", "preferred_action": "verify_claim"}

    def _claim_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = (filtered.current_paragraph_text or filtered.active_editor_text or "")
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "statistics": 0}
        stat_count = len(_STATISTIC_RE.findall(text))
        passed = stat_count >= self.min_statistics
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_factual_claim",
            "statistics": stat_count,
            "min_statistics": self.min_statistics,
        }


class RepeatedPhraseInParagraphScenario(ScenarioType):
    """현재 문단에 같은 ngram(2-단어)이 다수 반복 → 표현 다양화 제안."""

    name = "repeated_phrase_in_paragraph"
    priority = "medium"

    def __init__(
        self,
        *,
        ngram: int = 2,
        min_repeats: int = 3,
        min_paragraph_words: int = 20,
        cooldown_min_seconds: float = 180.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.ngram = ngram
        self.min_repeats = min_repeats
        self.min_paragraph_words = min_paragraph_words
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        repeat = self._repeat_status(context.filtered)
        passed = bool(repeat.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "repeated_phrase": self._gate_result(
                passed, "repeated_phrase_observed" if passed else "no_repeated_phrase", repeat,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("repeated_phrase_observed")
        else:
            evaluation.blockers.append("no_repeated_phrase")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"repeat_status": repeat, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "rephrase", "preferred_action": "suggest_alternative_wording"}

    def _repeat_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = filtered.current_paragraph_text or ""
        words = text.split()
        if len(words) < self.min_paragraph_words:
            return {
                "passed": False,
                "reason": "paragraph_too_short",
                "words": len(words),
                "min_paragraph_words": self.min_paragraph_words,
            }
        if len(words) < self.ngram:
            return {"passed": False, "reason": "ngram_unavailable", "top_ngram": None, "top_count": 0}
        from collections import Counter
        ngrams = [" ".join(words[i : i + self.ngram]) for i in range(len(words) - self.ngram + 1)]
        counts = Counter(ngrams)
        top_ngram, top_count = counts.most_common(1)[0] if counts else (None, 0)
        passed = top_count >= self.min_repeats
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_repeated_phrase",
            "top_ngram": top_ngram,
            "top_count": top_count,
            "min_repeats": self.min_repeats,
            "ngram": self.ngram,
        }


class TransitionWordOveruseScenario(ScenarioType):
    """접속어가 짧은 구간에 다수 → 흐름 다듬기 제안."""

    name = "transition_word_overuse"
    priority = "medium"

    def __init__(
        self,
        *,
        min_count: int = 4,
        cooldown_min_seconds: float = 300.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.min_count = min_count
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        transition = self._transition_status(context.filtered)
        passed = bool(transition.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "transition_overuse": self._gate_result(
                passed, "transition_overuse_observed" if passed else "transitions_ok", transition,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("transition_overuse_observed")
        else:
            evaluation.blockers.append("transitions_ok")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"transition_status": transition, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "smooth_flow", "preferred_action": "reduce_transitions"}

    def _transition_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = (filtered.current_paragraph_text or filtered.active_editor_text or "")
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "count": 0}
        count = sum(text.count(word) for word in KO_TRANSITION_WORDS)
        passed = count >= self.min_count
        return {
            "passed": passed,
            "reason": "ok" if passed else "transitions_ok",
            "count": count,
            "min_count": self.min_count,
        }


class WeakModifierOveruseScenario(ScenarioType):
    """약한 강조어가 다수 → 구체 표현으로 다듬기 제안."""

    name = "weak_modifier_overuse"
    priority = "medium"

    def __init__(
        self,
        *,
        min_count: int = 4,
        cooldown_min_seconds: float = 300.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.min_count = min_count
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        modifier = self._modifier_status(context.filtered)
        passed = bool(modifier.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "weak_modifier_overuse": self._gate_result(
                passed, "weak_modifier_overuse_observed" if passed else "modifiers_ok", modifier,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("weak_modifier_overuse_observed")
        else:
            evaluation.blockers.append("modifiers_ok")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"modifier_status": modifier, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "tighten", "preferred_action": "concretize_modifiers"}

    def _modifier_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = (filtered.current_paragraph_text or filtered.active_editor_text or "")
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "count": 0}
        count = sum(text.count(word) for word in KO_WEAK_MODIFIERS)
        passed = count >= self.min_count
        return {
            "passed": passed,
            "reason": "ok" if passed else "modifiers_ok",
            "count": count,
            "min_count": self.min_count,
        }


class JargonDensePassageScenario(ScenarioType):
    """기술 용어 밀도가 높은 구간 → 쉬운 표현 대안 제시."""

    name = "jargon_dense_passage"
    priority = "low"

    def __init__(
        self,
        *,
        min_jargon_count: int = 4,
        min_paragraph_chars: int = 100,
        cooldown_min_seconds: float = 600.0,
        jargon_dict: tuple[str, ...] = KO_TECH_JARGON,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.min_jargon_count = min_jargon_count
        self.min_paragraph_chars = min_paragraph_chars
        self.cooldown_min_seconds = cooldown_min_seconds
        self.jargon_dict = jargon_dict

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        jargon = self._jargon_status(context.filtered)
        passed = bool(jargon.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "jargon_dense": self._gate_result(
                passed, "jargon_dense_observed" if passed else "jargon_ok", jargon,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("jargon_dense_observed")
        else:
            evaluation.blockers.append("jargon_ok")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"jargon_status": jargon, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "accessible", "preferred_action": "simplify_jargon"}

    def _jargon_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = filtered.current_paragraph_text or ""
        if len(text) < self.min_paragraph_chars:
            return {
                "passed": False,
                "reason": "paragraph_too_short",
                "chars": len(text),
                "min_paragraph_chars": self.min_paragraph_chars,
            }
        count = sum(text.count(term) for term in self.jargon_dict)
        passed = count >= self.min_jargon_count
        return {
            "passed": passed,
            "reason": "ok" if passed else "jargon_ok",
            "jargon_count": count,
            "min_jargon_count": self.min_jargon_count,
            "dict_size": len(self.jargon_dict),
        }


# --- 2-B 캡처간 diff 시나리오 ---


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
