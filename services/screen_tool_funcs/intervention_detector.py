from __future__ import annotations

import difflib
import hashlib
import re
from typing import Any

from .models import FilteredScreenContext, InterventionDecision, WindowContext


class InterventionDetector:
    """History-aware rule gate for LLM intervention candidates."""

    EDITING_APP_TYPES = {"document", "presentation", "spreadsheet", "code_editor"}

    def __init__(
        self,
        *,
        history_window: int = 10,
        min_history_count: int = 5,
        dwell_threshold: float = 0.8,
        cooldown_events: int = 5,
        min_paragraph_chars: int = 20,
        min_ocr_paragraph_chars: int = 40,
        min_changed_chars: int = 10,
        min_idle_captures: int = 4,
        idle_similarity_threshold: float = 0.985,
    ) -> None:
        self.history_window = history_window
        self.min_history_count = min_history_count
        self.dwell_threshold = dwell_threshold
        self.cooldown_events = cooldown_events
        self.min_paragraph_chars = min_paragraph_chars
        self.min_ocr_paragraph_chars = min_ocr_paragraph_chars
        self.min_changed_chars = min_changed_chars
        self.min_idle_captures = min_idle_captures
        self.idle_similarity_threshold = idle_similarity_threshold

    def decide(
        self,
        *,
        window: WindowContext,
        filtered: FilteredScreenContext,
        history_events: list[dict[str, Any]] | None = None,
    ) -> InterventionDecision:
        history_events = history_events or []
        current_snapshot = self._snapshot(window=window, filtered=filtered)
        recent = (history_events + [current_snapshot])[-self.history_window:]
        same_document_events = [
            event for event in recent if self._document_key(event) == current_snapshot["document_key"]
        ]

        metadata = {
            "history_window": self.history_window,
            "history_count": len(recent),
            "same_document_count": len(same_document_events),
            "dwell_ratio": round(len(same_document_events) / max(len(recent), 1), 3),
            "document_key": current_snapshot["document_key"],
            "paragraph_fingerprint": current_snapshot["paragraph_fingerprint"],
        }
        typing_pause = self._typing_pause_status(same_document_events)
        metadata["typing_pause"] = typing_pause

        score = 0.0
        reasons: list[str] = []
        blockers: list[str] = []

        editing_app = self._is_editing_app(filtered)
        dwell_satisfied = self._has_sufficient_dwell(metadata)
        stable_paragraph = self._has_stable_paragraph(filtered)
        typing_pause_ready = bool(typing_pause.get("ready"))
        cooldown_passed = self._passes_cooldown(current_snapshot, history_events)
        supported_app = not self._is_sensitive_or_unsupported(window)
        metadata["checks"] = {
            "editing_app": {
                "passed": editing_app,
                "reason": "editing_app_active" if editing_app else "not_editing_app",
                "active_app_type": filtered.active_app_type,
            },
            "dwell": {
                "passed": dwell_satisfied,
                "reason": "editing_app_dwell_satisfied" if dwell_satisfied else "insufficient_dwell",
                "history_count": metadata["history_count"],
                "min_history_count": self.min_history_count,
                "dwell_ratio": metadata["dwell_ratio"],
                "dwell_threshold": self.dwell_threshold,
            },
            "stable_paragraph": {
                "passed": stable_paragraph,
                "reason": (
                    "current_paragraph_stable"
                    if stable_paragraph
                    else "unstable_current_paragraph"
                ),
                "current_paragraph_source": filtered.current_paragraph_source,
                "current_paragraph_chars": len((filtered.current_paragraph_text or "").strip()),
                "min_paragraph_chars": self.min_paragraph_chars,
                "min_ocr_paragraph_chars": self.min_ocr_paragraph_chars,
                "confidence": filtered.confidence,
            },
            "typing_pause": {
                "passed": typing_pause_ready,
                "reason": (
                    "typing_pause_satisfied"
                    if typing_pause_ready
                    else "not_paused_after_typing"
                ),
                **typing_pause,
            },
            "cooldown": {
                "passed": cooldown_passed,
                "reason": "cooldown_dedupe_passed" if cooldown_passed else "cooldown_or_duplicate",
                "cooldown_events": self.cooldown_events,
            },
            "supported_app": {
                "passed": supported_app,
                "reason": "supported_app" if supported_app else "sensitive_or_unsupported_app",
                "process_name": window.process_name,
            },
        }

        if not editing_app:
            blockers.append("not_editing_app")
        else:
            score += 0.2
            reasons.append("editing_app_active")

        if not dwell_satisfied:
            blockers.append("insufficient_dwell")
        else:
            score += 0.25
            reasons.append("editing_app_dwell_satisfied")

        if not stable_paragraph:
            blockers.append("unstable_current_paragraph")
        else:
            score += 0.2
            reasons.append("current_paragraph_stable")

        if not typing_pause_ready:
            blockers.append("not_paused_after_typing")
        else:
            score += 0.25
            reasons.append("typing_pause_satisfied")

        if not cooldown_passed:
            blockers.append("cooldown_or_duplicate")
        else:
            score += 0.1
            reasons.append("cooldown_dedupe_passed")

        if not supported_app:
            blockers = ["sensitive_or_unsupported_app"]
            score = 0.0
            reasons = []

        should_consider = not blockers
        priority = "high" if should_consider and score >= 0.85 else "medium" if should_consider else "low"
        metadata["blockers"] = blockers

        return InterventionDecision(
            should_consider_llm=should_consider,
            score=round(score, 2),
            priority=priority,
            reason_codes=reasons if should_consider else blockers,
            metadata=metadata,
        )

    def _snapshot(self, *, window: WindowContext, filtered: FilteredScreenContext) -> dict[str, Any]:
        paragraph = filtered.current_paragraph_text or ""
        return {
            "window": {
                "process_name": window.process_name,
                "window_title": window.window_title,
            },
            "filtered": {
                "active_app_type": filtered.active_app_type,
                "active_editor_text": filtered.active_editor_text,
                "current_paragraph_text": paragraph,
                "current_paragraph_source": filtered.current_paragraph_source,
                "changed_text": filtered.changed_text,
                "confidence": filtered.confidence,
            },
            "document_key": self._make_document_key(window),
            "paragraph_fingerprint": self._fingerprint(paragraph),
        }

    def _is_editing_app(self, filtered: FilteredScreenContext) -> bool:
        return filtered.active_app_type in self.EDITING_APP_TYPES

    def _has_sufficient_dwell(self, metadata: dict[str, Any]) -> bool:
        return (
            metadata["history_count"] >= self.min_history_count
            and metadata["dwell_ratio"] >= self.dwell_threshold
        )

    def _has_stable_paragraph(self, filtered: FilteredScreenContext) -> bool:
        paragraph = " ".join((filtered.current_paragraph_text or "").split())
        source = filtered.current_paragraph_source or ""
        if source == "ocr_same_as_full_text":
            return len(paragraph) >= self.min_ocr_paragraph_chars and filtered.confidence >= 0.55
        return bool(source and len(paragraph) >= self.min_paragraph_chars and filtered.confidence >= 0.8)

    def _typing_pause_status(self, same_document_events: list[dict[str, Any]]) -> dict[str, Any]:
        current_text = self._normalized_active_text(same_document_events[-1] if same_document_events else {})
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

    def _passes_cooldown(self, current_snapshot: dict[str, Any], history_events: list[dict[str, Any]]) -> bool:
        current_doc = current_snapshot["document_key"]
        current_paragraph = current_snapshot["paragraph_fingerprint"]
        if not current_paragraph:
            return False

        for event in reversed(history_events[-self.cooldown_events:]):
            intervention = event.get("intervention") or {}
            if not intervention.get("should_consider_llm"):
                continue
            if self._document_key(event) != current_doc:
                continue
            paragraph = self._event_paragraph_fingerprint(event)
            if paragraph == current_paragraph:
                return False
        return True

    def _latest_event_with_paragraph(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        for event in reversed(events):
            paragraph = ((event.get("filtered") or {}).get("current_paragraph_text") or "").strip()
            if paragraph:
                return event
        return None

    def _is_sensitive_or_unsupported(self, window: WindowContext) -> bool:
        return (window.process_name or "").lower() in {"lockapp.exe"}

    def _make_document_key(self, window: WindowContext) -> str:
        process_name = (window.process_name or "").lower()
        title = self._normalize_key(window.window_title or "")
        return f"{process_name}|{title}"

    def _document_key(self, event: dict[str, Any]) -> str:
        if event.get("document_key"):
            return str(event["document_key"])
        window = event.get("window") or {}
        process_name = str(window.get("process_name") or "").lower()
        title = self._normalize_key(str(window.get("window_title") or ""))
        return f"{process_name}|{title}"

    def _event_paragraph_fingerprint(self, event: dict[str, Any]) -> str:
        if event.get("paragraph_fingerprint"):
            return str(event["paragraph_fingerprint"])
        filtered = event.get("filtered") or {}
        return self._fingerprint(str(filtered.get("current_paragraph_text") or ""))

    def _fingerprint(self, text: str) -> str:
        normalized = " ".join(text.split()).strip().lower()
        if not normalized:
            return ""
        normalized = normalized[:500]
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    def _normalize_key(self, value: str) -> str:
        value = " ".join(value.split()).lower()
        return re.sub(r"\s+", " ", value).strip()
