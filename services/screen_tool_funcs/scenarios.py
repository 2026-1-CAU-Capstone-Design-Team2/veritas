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

    name: str = ""
    priority: str = "medium"
    initial_vruntime: float = 0.0
    vruntime_increment: float = 1.0

    def __init__(
        self,
        *,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
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
        cooldown_events: int = 5,
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

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)

        typing_pause = self._typing_pause_status(context.same_document_events)
        typing_pause_passed = bool(typing_pause.get("ready"))
        cooldown_passed = self._passes_paragraph_cooldown(
            history_events=context.history_events,
            document_key=context.document_key,
            paragraph_fingerprint=context.paragraph_fingerprint,
        )
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

        # 점수 없는 순수 prerequisite: blocker에만 기여
        if substantial_paragraph_passed:
            evaluation.reasons.append("substantial_paragraph")
        else:
            evaluation.blockers.append("paragraph_too_short")

        evaluation.ready = not evaluation.blockers
        evaluation.priority = "high" if evaluation.ready and evaluation.score >= 0.7 else "medium"
        evaluation.metadata = {"typing_pause": typing_pause}
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
        cooldown_min_seconds: float = 600.0,
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
        cooldown = self._review_cooldown_status(context.last_fired_at)
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

    def _review_cooldown_status(self, last_fired_at: dict[str, float]) -> dict[str, Any]:
        """`last_fired_at[self.name]`(직전 발동 시각)과 현재 시각을 비교해,
        경과가 `cooldown_min_seconds` 이상이면 통과시키는 시간 기반 cooldown.
        """
        last_at = last_fired_at.get(self.name)
        if last_at is None:
            return {
                "passed": True,
                "reason": "no_prior_review",
                "min_seconds": self.cooldown_min_seconds,
            }

        elapsed_seconds = max(time.time() - last_at, 0.0)
        passed = elapsed_seconds >= self.cooldown_min_seconds
        return {
            "passed": passed,
            "reason": "ok" if passed else "cooldown_active",
            "elapsed_seconds": round(elapsed_seconds, 1),
            "min_seconds": self.cooldown_min_seconds,
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
