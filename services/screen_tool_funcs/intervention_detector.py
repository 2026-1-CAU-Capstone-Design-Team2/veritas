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
        min_ocr_paragraph_chars: int = 80,
        min_changed_chars: int = 10,
    ) -> None:
        self.history_window = history_window
        self.min_history_count = min_history_count
        self.dwell_threshold = dwell_threshold
        self.cooldown_events = cooldown_events
        self.min_paragraph_chars = min_paragraph_chars
        self.min_ocr_paragraph_chars = min_ocr_paragraph_chars
        self.min_changed_chars = min_changed_chars

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

        score = 0.0
        reasons: list[str] = []
        blockers: list[str] = []

        if not self._is_editing_app(filtered):
            blockers.append("not_editing_app")
        else:
            score += 0.2
            reasons.append("editing_app_active")

        if not self._has_sufficient_dwell(metadata):
            blockers.append("insufficient_dwell")
        else:
            score += 0.25
            reasons.append("editing_app_dwell_satisfied")

        if not self._has_stable_paragraph(filtered):
            blockers.append("unstable_current_paragraph")
        else:
            score += 0.2
            reasons.append("current_paragraph_stable")

        if not self._is_continuing_edit(filtered, same_document_events[:-1]):
            blockers.append("not_continuing_paragraph_edit")
        else:
            score += 0.25
            reasons.append("paragraph_edit_continuing")

        if not self._passes_cooldown(current_snapshot, history_events):
            blockers.append("cooldown_or_duplicate")
        else:
            score += 0.1
            reasons.append("cooldown_dedupe_passed")

        if self._is_sensitive_or_unsupported(window):
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

    def _is_continuing_edit(
        self,
        filtered: FilteredScreenContext,
        previous_same_document_events: list[dict[str, Any]],
    ) -> bool:
        if len(filtered.changed_text or "") >= self.min_changed_chars:
            return True

        previous = self._latest_event_with_paragraph(previous_same_document_events)
        if previous is None:
            return bool((filtered.current_paragraph_text or "").strip())

        previous_filtered = previous.get("filtered") or {}
        previous_paragraph = str(previous_filtered.get("current_paragraph_text") or "")
        current_paragraph = filtered.current_paragraph_text or ""
        if not previous_paragraph or not current_paragraph:
            return False

        similarity = difflib.SequenceMatcher(None, previous_paragraph, current_paragraph).ratio()
        previous_full_text = str(previous_filtered.get("active_editor_text") or "")
        if previous_full_text != (filtered.active_editor_text or ""):
            return similarity >= 0.5
        return similarity >= 0.98 and bool(current_paragraph.strip())

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
