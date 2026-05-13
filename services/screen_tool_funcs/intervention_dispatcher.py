from __future__ import annotations

import re

from .models import ScreenContextEvent
from .scenarios import ScenarioType
from .store import ScreenContextStore


class InterventionDispatcher:
    """Persist approved intervention candidates for downstream consumers.

    Scenario-specific payload shaping is delegated to ScenarioType.* hooks
    (`writing_context_overrides`, `tool_routing_hint_overrides`). The
    dispatcher only builds the common base shape and merges overrides for the
    selected scenario.
    """

    def __init__(
        self,
        store: ScreenContextStore,
        *,
        scenarios: dict[str, ScenarioType] | None = None,
        console_log: bool = False,
    ) -> None:
        self.store = store
        self.scenarios: dict[str, ScenarioType] = dict(scenarios or {})
        self.console_log = console_log

    def dispatch(self, event: ScreenContextEvent) -> dict | None:
        if not event.intervention.should_consider_llm:
            return None

        payload = self._build_payload(event)
        self.store.enqueue_intervention(payload)
        if self.console_log:
            pending_count = len(self.store.load_pending_interventions())
            print(
                "[screen_context][intervention] "
                f"queued event={event.event_id} "
                f"type={event.intervention.intervention_type} "
                f"priority={event.intervention.priority} "
                f"score={event.intervention.score} "
                f"pending={pending_count}"
            )
        return payload

    def _build_payload(self, event: ScreenContextEvent) -> dict:
        filtered = event.filtered
        window = event.window
        metadata = event.intervention.metadata or {}
        intervention_type = event.intervention.intervention_type or "none"
        activity_context = self._activity_context(metadata)
        recent_sentences = self._recent_sentences(
            filtered.current_paragraph_text or filtered.active_editor_text,
            limit=2,
        )
        focused_sentence = self._focused_sentence(
            paragraph=filtered.current_paragraph_text,
            changed_text=filtered.changed_text,
        )
        if not focused_sentence:
            focused_sentence = self._recent_sentences(recent_sentences, limit=1)
        writing_context = self._writing_context_for_type(
            intervention_type=intervention_type,
            filtered=filtered,
            recent_sentences=recent_sentences,
            focused_sentence=focused_sentence,
        )
        intervention_flag = self._intervention_flag(event)
        tool_routing_hint = self._tool_routing_hint(
            event,
            focused_sentence=focused_sentence,
            intervention_type=intervention_type,
        )

        return {
            "type": "screen_intervention",
            "intervention_type": intervention_type,
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
            "writing_context": writing_context,
            "activity_context": activity_context,
            "intervention_flag": intervention_flag,
            "tool_routing_hint": tool_routing_hint,
            "intervention": {
                "intervention_type": intervention_type,
                "score": event.intervention.score,
                "priority": event.intervention.priority,
                "reason_codes": event.intervention.reason_codes,
                "metadata": event.intervention.metadata,
            },
        }

    def _writing_context_for_type(
        self,
        *,
        intervention_type: str,
        filtered,
        recent_sentences: str,
        focused_sentence: str,
    ) -> dict:
        base = {
            "full_text": filtered.active_editor_text,
            "full_text_chars": len(filtered.active_editor_text or ""),
            "current_paragraph": filtered.current_paragraph_text,
            "recent_sentences": recent_sentences,
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
            "focus_scope": "recent_writing",
        }
        scenario = self.scenarios.get(intervention_type)
        if scenario is not None:
            overrides = scenario.writing_context_overrides(filtered=filtered, base=base) or {}
            base.update(overrides)
        return base

    def _activity_context(self, metadata: dict) -> dict:
        scenarios = metadata.get("scenarios") or {}
        idle_meta = (
            (scenarios.get("idle_after_writing") or {}).get("metadata") or {}
            if isinstance(scenarios, dict)
            else {}
        )
        # Fallback to legacy top-level typing_pause for events from pre-refactor.
        typing_pause = idle_meta.get("typing_pause") or metadata.get("typing_pause") or {}
        selected = metadata.get("selected")
        selected_metadata = (
            (scenarios.get(selected) or {}).get("metadata") or {}
            if isinstance(scenarios, dict) and selected
            else {}
        )
        return {
            "history_window": metadata.get("history_window", 0),
            "history_count": metadata.get("history_count", 0),
            "same_document_count": metadata.get("same_document_count", 0),
            "dwell_ratio": metadata.get("dwell_ratio", 0.0),
            "document_key": metadata.get("document_key", ""),
            "paragraph_fingerprint": metadata.get("paragraph_fingerprint", ""),
            "typing_pause": typing_pause,
            "selected_scenario": selected,
            "selected_scenario_metadata": selected_metadata,
        }

    def _intervention_flag(self, event: ScreenContextEvent) -> dict:
        metadata = event.intervention.metadata or {}
        common_checks = metadata.get("common_checks") or {}
        scenarios = metadata.get("scenarios") or {}
        selected = metadata.get("selected")

        def _passed(name: str) -> bool:
            check = common_checks.get(name) if isinstance(common_checks, dict) else None
            return bool(check.get("passed")) if isinstance(check, dict) else False

        selected_scenario = scenarios.get(selected) if isinstance(scenarios, dict) and selected else None
        selected_reasons = (
            set(selected_scenario.get("reasons") or [])
            if isinstance(selected_scenario, dict)
            else set()
        )
        blockers = set(metadata.get("blockers") or [])

        return {
            "should_consider_llm": event.intervention.should_consider_llm,
            "intervention_type": event.intervention.intervention_type,
            "selected_scenario": selected,
            "priority": event.intervention.priority,
            "score": event.intervention.score,
            "reason_codes": event.intervention.reason_codes,
            "blockers": sorted(blockers),
            "flags": {
                "editing_app": _passed("editing_app"),
                "dwell_satisfied": _passed("dwell"),
                "paragraph_stable": _passed("stable_paragraph"),
                "typing_pause_satisfied": "typing_pause_satisfied" in selected_reasons,
                "cooldown_dedupe_passed": "cooldown_dedupe_passed" in selected_reasons,
                "sustained_writing_observed": "sustained_writing_observed" in selected_reasons,
                "idle_after_sustained_writing": "idle_after_sustained_writing" in selected_reasons,
                "document_cooldown_passed": "document_cooldown_passed" in selected_reasons,
            },
        }

    def _tool_routing_hint(
        self,
        event: ScreenContextEvent,
        *,
        focused_sentence: str,
        intervention_type: str,
    ) -> dict:
        current_paragraph = event.filtered.current_paragraph_text or ""
        changed_text = event.filtered.changed_text or ""
        needs_research = self._looks_research_needy(current_paragraph)

        base: dict = {
            "intervention_type": intervention_type,
            "tone": "neutral",
            "allowed_actions": [
                "continue_writing",
                "provide_supporting_material",
                "search_sources",
                "revise_current_paragraph",
                "review_whole_document",
                "no_action",
            ],
            "preferred_action": "no_action",
            "signals": {
                "research_needed": needs_research,
                "has_recent_change": bool(changed_text.strip()),
                "has_focused_sentence": bool(focused_sentence.strip()),
            },
        }
        scenario = self.scenarios.get(intervention_type)
        if scenario is not None:
            overrides = (
                scenario.tool_routing_hint_overrides(
                    event=event,
                    base=base,
                    focused_sentence=focused_sentence,
                )
                or {}
            )
            for key, value in overrides.items():
                if key == "signals" and isinstance(value, dict):
                    base["signals"] = {**base["signals"], **value}
                else:
                    base[key] = value
        return base

    def _focused_sentence(self, *, paragraph: str, changed_text: str) -> str:
        paragraph = (paragraph or "").strip()
        changed_text = (changed_text or "").strip()
        if not paragraph:
            return ""

        sentences = self._split_sentences(paragraph)
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

    def _recent_sentences(self, text: str, *, limit: int = 2) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        sentences = self._split_sentences(text)
        if not sentences:
            return text[-800:].strip()
        return " ".join(sentences[-max(limit, 1) :])[-800:].strip()

    def _split_sentences(self, text: str) -> list[str]:
        normalized = re.sub(r"\n+", "\n", (text or "").strip())
        if not normalized:
            return []

        parts = [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+|\n+", normalized) if part.strip()]
        if parts:
            return parts
        return [normalized]

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
