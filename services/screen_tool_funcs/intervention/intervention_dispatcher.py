from __future__ import annotations

import re

from ..core.content_filter import CURSOR_SCOPE_CHANGE_MAX_RATIO, resolve_cursor_scope
from ..core.models import ScreenContextEvent
from ..core.store import ScreenContextStore
from ..scenario import ScenarioType


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
        # Resolve where the user is actively writing. Priority: caret paragraph
        # → recent edit region → document tail. NEVER the document head — that
        # is the bug that returned intro-sentence reviews while the user was
        # writing the conclusion.
        anchor_block, focus_hint = self._resolve_anchor(filtered)
        recent_sentences = self._recent_sentences(anchor_block, limit=2)
        focused_sentence = self._focused_sentence(
            paragraph=anchor_block,
            changed_text=focus_hint,
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
            # retry로 재발화될 때 직전 제안 — 생성기가 같은 문장을 피하도록.
            "avoid_text": str(metadata.get("avoid_text") or ""),
            # retry면 원래 카드 id — 새 카드 대신 그 카드를 갱신(upsert)하게 한다.
            "retry_event_id": str(metadata.get("retry_event_id") or ""),
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
            # 커서가 속한 섹션 제목 — 이어쓰기를 그 섹션 주제 범위에 둔다.
            "section_heading": getattr(filtered, "section_heading", "") or "",
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
        """Build the intervention_flag payload with scenario-isolated flags.

        flags structure (post B-3):
            common: { editing_app, dwell_satisfied, paragraph_stable }
            scenarios: { <name>: { ready, score, gates: { <gate>: bool } } }
            selected_scenario: <name> | None

        The earlier shape flattened every scenario's reason flag at the top
        level, so a consumer reading `flags.typing_pause_satisfied` could not
        tell whether the idle scenario actually failed that gate or whether
        another scenario was simply chosen. Scoping the flags per scenario
        removes that ambiguity. Common gate flags also drop the redundant
        `_satisfied`/`_stable` suffixes so all flags read as plain booleans.
        """
        metadata = event.intervention.metadata or {}
        common_checks = metadata.get("common_checks") or {}
        scenarios_meta = metadata.get("scenarios") or {}
        selected = metadata.get("selected")
        blockers = set(metadata.get("blockers") or [])

        common_flags: dict[str, bool] = {}
        if isinstance(common_checks, dict):
            for name, check in common_checks.items():
                common_flags[name] = bool(check.get("passed")) if isinstance(check, dict) else False

        scenario_flags: dict[str, dict] = {}
        if isinstance(scenarios_meta, dict):
            for scenario_name, scenario_data in scenarios_meta.items():
                if not isinstance(scenario_data, dict):
                    continue
                gate_results = scenario_data.get("gate_results") or {}
                gate_flags: dict[str, bool] = {}
                if isinstance(gate_results, dict):
                    for gate_name, gate_data in gate_results.items():
                        if isinstance(gate_data, dict):
                            gate_flags[gate_name] = bool(gate_data.get("passed"))
                scenario_flags[scenario_name] = {
                    "ready": bool(scenario_data.get("ready")),
                    "score": scenario_data.get("score", 0.0),
                    "gates": gate_flags,
                }

        return {
            "should_consider_llm": event.intervention.should_consider_llm,
            "intervention_type": event.intervention.intervention_type,
            "selected_scenario": selected,
            "priority": event.intervention.priority,
            "score": event.intervention.score,
            "reason_codes": event.intervention.reason_codes,
            "blockers": sorted(blockers),
            "flags": {
                "common": common_flags,
                "scenarios": scenario_flags,
                "selected_scenario": selected,
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

    def _resolve_anchor(self, filtered) -> tuple[str, str]:
        """``(anchor_block, focus_hint)`` — 사용자가 지금 글을 쓰고 있는 텍스트
        영역과 그 안에서의 최근 편집 힌트.

        ContentFilter가 이미 ``cursor_scope_text``를 (caret/diff-offset 윈도우/꼬리
        규칙으로) 계산해 두므로 **그 필드를 그대로 anchor로 쓴다** — 시나리오 발화
        스코프와 LLM 앵커가 동일한 단일 소스. 구버전 캡처라 필드가 비면
        ``resolve_cursor_scope``로 재계산(offset 없음 → 꼬리 fallback)한다."""
        scope = (getattr(filtered, "cursor_scope_text", "") or "").strip()
        full = (getattr(filtered, "active_editor_text", "") or "").strip()
        changed = (getattr(filtered, "changed_text", "") or "").strip()
        if not scope:
            return resolve_cursor_scope(
                full=full,
                caret=getattr(filtered, "current_paragraph_text", "") or "",
                changed=changed,
            )
        focus_hint = (
            changed
            if changed and len(changed) < max(1, len(full)) * CURSOR_SCOPE_CHANGE_MAX_RATIO
            else ""
        )
        return scope, focus_hint

    def _focused_sentence(self, *, paragraph: str, changed_text: str) -> str:
        paragraph = (paragraph or "").strip()
        changed_text = (changed_text or "").strip()
        if not paragraph:
            return ""

        sentences = self._split_sentences(paragraph)
        if not sentences:
            return paragraph

        # Anchor to the sentence carrying the MOST RECENT edit. The caret sits at
        # the END of the changed region, so match by its tail (not its head) and
        # scan from the end of the paragraph so the latest occurrence wins. The
        # old head-match would, for a whole-document changed_text, resolve to the
        # intro sentence; ``_resolve_anchor`` already bounds ``paragraph`` and
        # passes only a bounded ``changed_text`` here, and the tail anchor keeps
        # the focus on where the user actually is.
        if changed_text:
            for probe in (changed_text[-80:].strip(), changed_text[-20:].strip()):
                if not probe:
                    continue
                for sentence in reversed(sentences):
                    if probe in sentence:
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
