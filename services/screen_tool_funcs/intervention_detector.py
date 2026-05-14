from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from .models import FilteredScreenContext, InterventionDecision, WindowContext
from .scenario_scheduler import ScenarioScheduler, ScenarioWeights
from .scenarios import (
    IdleAfterWritingScenario,
    ScenarioContext,
    ScenarioEvaluation,
    ScenarioType,
    WholeDocumentReviewScenario,
)


class InterventionDetector:
    """Common gates + scenario fan-out + CFS selection.

    Flow:
        1. Build snapshot + history slice.
        2. Run common gates (editing_app, dwell, stable_paragraph) sequentially.
           Any failure short-circuits to a non-intervention decision.
        3. Fan-out scenario evaluations. Every scenario is evaluated so that
           all readiness signals are recorded for telemetry, even when CFS
           ultimately picks only one.
        4. CFS scheduler selects exactly one scenario from the ready set.
        5. Build InterventionDecision with intervention_type set to the
           selected scenario (or "none" if no scenario is ready).
    """

    EDITING_APP_TYPES = {"document", "presentation", "spreadsheet", "code_editor"}

    def __init__(
        self,
        *,
        history_window: int = 10,
        min_history_count: int = 5,
        dwell_threshold: float = 0.5,
        min_paragraph_chars: int = 20,
        min_ocr_paragraph_chars: int = 40,
        scenarios: list[ScenarioType] | None = None,
        scheduler: ScenarioScheduler | None = None,
    ) -> None:
        self.history_window = history_window
        self.min_history_count = min_history_count
        self.dwell_threshold = dwell_threshold
        self.min_paragraph_chars = min_paragraph_chars
        self.min_ocr_paragraph_chars = min_ocr_paragraph_chars

        self.scenarios: list[ScenarioType] = scenarios or [
            IdleAfterWritingScenario(min_paragraph_chars=min_paragraph_chars),
            WholeDocumentReviewScenario(),
        ]
        self.scheduler = scheduler

    @property
    def scenario_weights(self) -> dict[str, ScenarioWeights]:
        return {
            scenario.name: ScenarioWeights(
                initial_vruntime=scenario.initial_vruntime,
                vruntime_increment=scenario.vruntime_increment,
            )
            for scenario in self.scenarios
        }

    # decide()는 캡처 시점의 window/filtered/history_events를 바탕으로 intervention 여부와 시나리오 선택을 결정한다.
    # schedule=True인 경우 시나리오 스코어링 후 CFS 스케줄링까지 수행, False인 경우 common gate와 시나리오 게이트 결과만 반환 (CFS 스케줄링은 하지 않음)
    def decide(
        self,
        *,
        window: WindowContext,
        filtered: FilteredScreenContext,
        history_events: list[dict[str, Any]] | None = None,
        schedule: bool = True,
    ) -> InterventionDecision:
        history_events = history_events or []
        current_snapshot = self._snapshot(window=window, filtered=filtered)
        # recent = 과거 리스너 이벤트에 이번 캡쳐를 붙이고, history_window 크기만큼 자른다. (최대 history_window 개의 이벤트를 고려한다.)
        recent = (history_events + [current_snapshot])[-self.history_window:]
        same_document_events = [
            event for event in recent if self._document_key(event) == current_snapshot["document_key"]
        ]

        common_metadata = {
            "history_window": self.history_window,
            "history_count": len(recent),
            "same_document_count": len(same_document_events),
            "dwell_ratio": round(len(same_document_events) / max(len(recent), 1), 3),
            "document_key": current_snapshot["document_key"],
            "paragraph_fingerprint": current_snapshot["paragraph_fingerprint"],
        }

        editing_app = self._is_editing_app(filtered)
        dwell_satisfied = self._has_sufficient_dwell(common_metadata)
        stable_paragraph = self._has_stable_paragraph(filtered)

        common_checks: dict[str, dict[str, Any]] = {
            "editing_app": {
                "passed": editing_app,
                "reason": "editing_app_active" if editing_app else "not_editing_app",
                "active_app_type": filtered.active_app_type,
            },
            "dwell": {
                "passed": dwell_satisfied,
                "reason": "editing_app_dwell_satisfied" if dwell_satisfied else "insufficient_dwell",
                "history_count": common_metadata["history_count"],
                "min_history_count": self.min_history_count,
                "dwell_ratio": common_metadata["dwell_ratio"],
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
        }

        common_blockers = [name for name, check in common_checks.items() if not check["passed"]]
        if common_blockers:
            return InterventionDecision(
                should_consider_llm=False,
                intervention_type="none",
                score=0.0,
                priority="low",
                reason_codes=[common_checks[name]["reason"] for name in common_blockers],
                metadata={
                    **common_metadata,
                    "common_checks": common_checks,
                    "scenarios": {},
                    "blockers": common_blockers,
                    "selected": None,
                    "scheduler": None,
                },
            )

        """
           dwell() -> recent 기반 document key 일치 비율이 충분한가? [탐색 개수 : history_window, 탐색 대상 : same_document_events ]
           stable_paragraph() -> 현재 단락이 충분히 길거나 OCR confidence가 충분히 높은가? [current_paragraph_text, current_paragraph_source, confidence]
           typing_pause() -> 최근 입력 이벤트 후 충분한 무활동 시간이 지났는가? [최근 입력 이벤트 timestamp, 현재 timestamp, min_typing_pause_seconds]
            
        """

        # `now`를 한 번 만들어 get_state/select_and_charge/snapshot에 공유.
        # last_fired_at/last_fired_doc_chars는 fan-out 전에 읽어 cooldown 게이트에 전달.
        now = time.time()
        last_fired_at: dict[str, float] = {}
        last_fired_doc_chars: dict[str, int] = {}
        if self.scheduler is not None:
            state = self.scheduler.get_state(current_snapshot["document_key"], now=now)
            last_fired_at = dict(state.last_fired_at)
            last_fired_doc_chars = dict(state.last_fired_doc_chars)

        scenario_ctx = ScenarioContext(
            window=window,
            filtered=filtered,
            history_events=history_events,  # RAM에 유지, 잘리지 않은 원본 history
            same_document_events=same_document_events, # recent 에서 document_key가 같은 이벤트들
            document_key=current_snapshot["document_key"],
            paragraph_fingerprint=current_snapshot["paragraph_fingerprint"],
            last_fired_at=last_fired_at,
            last_fired_doc_chars=last_fired_doc_chars,
        )
        scenario_results: dict[str, ScenarioEvaluation] = {}
        for scenario in self.scenarios:
            scenario_results[scenario.name] = scenario.evaluate(scenario_ctx)

        ready_names = [name for name, ev in scenario_results.items() if ev.ready]
        scheduler_snapshot: dict[str, Any] | None = None
        selected_name: str | None = None
        if schedule and ready_names and self.scheduler is not None:
            # 발동 시점의 정규화 문서 길이 — 글자수 기반 cooldown 판정용
            doc_chars = len(" ".join((filtered.active_editor_text or "").split()))
            selected_name = self.scheduler.select_and_charge(
                current_snapshot["document_key"],
                ready_names,
                now=now,
                doc_chars=doc_chars,
            )
            scheduler_snapshot = self.scheduler.snapshot(
                current_snapshot["document_key"], now=now
            )
        elif schedule and ready_names:
            selected_name = ready_names[0]

        scenarios_meta = {
            name: {
                "ready": ev.ready,
                "score": round(ev.score, 3),
                "priority": ev.priority,
                "reasons": list(ev.reasons),
                "blockers": list(ev.blockers),
                "gate_results": ev.gate_results,
                "metadata": ev.metadata,
            }
            for name, ev in scenario_results.items()
        }

        if selected_name is None:
            blockers = [f"scenario:{name}:not_ready" for name in scenario_results if not scenario_results[name].ready]
            return InterventionDecision(
                should_consider_llm=False,
                intervention_type="none",
                score=0.0,
                priority="low",
                reason_codes=blockers,
                metadata={
                    **common_metadata,
                    "common_checks": common_checks,
                    "scenarios": scenarios_meta,
                    "blockers": blockers,
                    "selected": None,
                    "scheduler": scheduler_snapshot,
                },
            )

        selected = scenario_results[selected_name]
        return InterventionDecision(
            should_consider_llm=True,
            intervention_type=selected_name,
            score=round(selected.score, 3),
            priority=selected.priority,
            reason_codes=list(selected.reasons),
            metadata={
                **common_metadata,
                "common_checks": common_checks,
                "scenarios": scenarios_meta,
                "blockers": [],
                "selected": selected_name,
                "scheduler": scheduler_snapshot,
            },
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
        if not source:
            return False
        if source == "ocr_same_as_full_text":
            # OCR confidence is a flat constant, so paragraph length stays the
            # only available trustworthiness proxy for OCR-sourced captures.
            return len(paragraph) >= self.min_ocr_paragraph_chars and filtered.confidence >= 0.55
        # UIA/app-text extraction is precise: a short current paragraph is still
        # a correct extraction, so this common gate no longer applies a length
        # floor here. Per-paragraph length is now a scenario-level gate (see
        # ScenarioType._has_substantial_paragraph) so document-scoped scenarios
        # are not blocked by a short current paragraph.
        return filtered.confidence >= 0.8

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

    def _fingerprint(self, text: str) -> str:
        normalized = " ".join(text.split()).strip().lower()
        if not normalized:
            return ""
        normalized = normalized[:500]
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    def _normalize_key(self, value: str) -> str:
        value = " ".join(value.split()).lower()
        return re.sub(r"\s+", " ", value).strip()
