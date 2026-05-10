from __future__ import annotations

import re

from .models import ScreenContextEvent
from .store import ScreenContextStore


class InterventionDispatcher:
    """Persist approved intervention candidates for downstream consumers."""

    def __init__(self, store: ScreenContextStore, *, console_log: bool = False) -> None:
        self.store = store
        self.console_log = console_log

    def dispatch(self, event: ScreenContextEvent) -> dict | None:
        if not event.intervention.should_consider_llm:
            return None

        payload = self._build_payload(event)
        self.store.enqueue_intervention(payload)
        if self.console_log:
            print(
                "[screen_context][intervention] "
                f"queued event={event.event_id} "
                f"priority={event.intervention.priority} "
                f"score={event.intervention.score}"
            )
        return payload

    def _build_payload(self, event: ScreenContextEvent) -> dict:
        filtered = event.filtered
        window = event.window
        metadata = event.intervention.metadata or {}
        activity_context = self._activity_context(metadata)
        focused_sentence = self._focused_sentence(
            paragraph=filtered.current_paragraph_text,
            changed_text=filtered.changed_text,
        )
        intervention_flag = self._intervention_flag(event)
        tool_routing_hint = self._tool_routing_hint(event, focused_sentence=focused_sentence)

        return {
            "type": "screen_intervention",
            "event_id": event.event_id,
            "captured_at": event.captured_at,
            "app_context": {
                "process": window.process_name,
                "title": window.window_title,
                "pid": window.pid,
                "hwnd": window.hwnd,
                "app_type": filtered.active_app_type,
                "document_key": activity_context.get("document_key", ""),
            },
            "app": {
                "process": window.process_name,
                "title": window.window_title,
                "pid": window.pid,
                "hwnd": window.hwnd,
            },
            "writing_context": {
                "full_text": filtered.active_editor_text,
                "current_paragraph": filtered.current_paragraph_text,
                "focused_sentence": focused_sentence,
                "paragraph_source": filtered.current_paragraph_source,
                "paragraph_rect": (
                    {
                        "x": filtered.current_paragraph_rect.x,
                        "y": filtered.current_paragraph_rect.y,
                        "width": filtered.current_paragraph_rect.width,
                        "height": filtered.current_paragraph_rect.height,
                    }
                    if filtered.current_paragraph_rect
                    else None
                ),
                "changed_text": filtered.changed_text,
                "confidence": filtered.confidence,
            },
            "activity_context": activity_context,
            "intervention_flag": intervention_flag,
            "tool_routing_hint": tool_routing_hint,
            "intervention": {
                "score": event.intervention.score,
                "priority": event.intervention.priority,
                "reason_codes": event.intervention.reason_codes,
                "metadata": event.intervention.metadata,
            },
        }

    def _activity_context(self, metadata: dict) -> dict:
        return {
            "history_window": metadata.get("history_window", 0),
            "history_count": metadata.get("history_count", 0),
            "same_document_count": metadata.get("same_document_count", 0),
            "dwell_ratio": metadata.get("dwell_ratio", 0.0),
            "document_key": metadata.get("document_key", ""),
            "paragraph_fingerprint": metadata.get("paragraph_fingerprint", ""),
        }

    def _intervention_flag(self, event: ScreenContextEvent) -> dict:
        reasons = set(event.intervention.reason_codes or [])
        blockers = set((event.intervention.metadata or {}).get("blockers") or [])
        return {
            "should_consider_llm": event.intervention.should_consider_llm,
            "priority": event.intervention.priority,
            "score": event.intervention.score,
            "reason_codes": event.intervention.reason_codes,
            "blockers": sorted(blockers),
            "flags": {
                "editing_app": "editing_app_active" in reasons,
                "dwell_satisfied": "editing_app_dwell_satisfied" in reasons,
                "paragraph_stable": "current_paragraph_stable" in reasons,
                "continuing_edit": "paragraph_edit_continuing" in reasons,
                "cooldown_dedupe_passed": "cooldown_dedupe_passed" in reasons,
            },
        }

    def _tool_routing_hint(self, event: ScreenContextEvent, *, focused_sentence: str) -> dict:
        current_paragraph = event.filtered.current_paragraph_text or ""
        changed_text = event.filtered.changed_text or ""
        needs_research = self._looks_research_needy(current_paragraph)
        preferred_action = "provide_supporting_material" if needs_research else "continue_writing"
        if len(current_paragraph.strip()) < 20 and not focused_sentence:
            preferred_action = "no_action"

        return {
            "allowed_actions": [
                "continue_writing",
                "provide_supporting_material",
                "search_sources",
                "revise_current_paragraph",
                "no_action",
            ],
            "preferred_action": preferred_action,
            "signals": {
                "research_needed": needs_research,
                "has_recent_change": bool(changed_text.strip()),
                "has_focused_sentence": bool(focused_sentence.strip()),
            },
        }

    def _focused_sentence(self, *, paragraph: str, changed_text: str) -> str:
        paragraph = (paragraph or "").strip()
        changed_text = (changed_text or "").strip()
        if not paragraph:
            return ""

        sentences = [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+|\n+", paragraph) if part.strip()]
        if not sentences:
            return paragraph

        if changed_text:
            changed_head = changed_text[:80].strip()
            for sentence in reversed(sentences):
                if changed_head and changed_head in sentence:
                    return sentence
            for sentence in reversed(sentences):
                if changed_text[:20].strip() and changed_text[:20].strip() in sentence:
                    return sentence

        return sentences[-1]

    def _looks_research_needy(self, text: str) -> bool:
        normalized = (text or "").lower()
        markers = (
            "근거",
            "자료",
            "출처",
            "통계",
            "연구",
            "사례",
            "according to",
            "evidence",
            "source",
            "statistics",
            "research",
        )
        return any(marker in normalized for marker in markers)
