from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from ..core.models import FilteredScreenContext, InterventionDecision, WindowContext
from ..trace import screen_trace
from .llm_router import ScenarioRouter
from .scenario_scheduler import ScenarioScheduler, ScenarioWeights
from ..scenario import (
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
    # Below this router confidence, decline rather than surface a weak intervention.
    ROUTER_MIN_CONFIDENCE = 0.5

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
        router: ScenarioRouter | None = None,
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
        self.router = router

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

        # Blank-document escape: stable_paragraph(+ dwell)가 유일하게 막힌 게이트이고
        # 현재 캡처가 정말로 빈 문서면 fan-out 을 허용한다. BlankDocumentStartScenario
        # 처럼 "텍스트가 없다"는 상태 자체를 트리거로 쓰는 시나리오는
        #   - stable_paragraph 게이트: source="" → False
        #   - dwell 게이트: 새 문서로 막 들어오면 history_count<min_history_count
        # 두 게이트에서 동시에 막혀 평가조차 안 됐다. editing_app 이 막힌 경우는
        # editor 자체가 아니므로 escape 대상 아님.
        #
        # dwell 까지 풀어도 너무 일찍 발화하지 않는 이유:
        # BlankDocumentStartScenario._near_empty_status 가 자체적으로
        # min_blank_captures(=3) 만큼 연속 빈 캡처를 요구한다. 다른 시나리오들은
        # 어차피 빈 텍스트에서 ready 조건을 충족하지 못한다.
        blank_document_allowed = False
        escapable_gates = {"stable_paragraph", "dwell"}
        if common_blockers and set(common_blockers).issubset(escapable_gates):
            active_text = " ".join((filtered.active_editor_text or "").split())
            paragraph_text = " ".join((filtered.current_paragraph_text or "").split())
            no_source = not (filtered.current_paragraph_source or "")
            # 30 = BlankDocumentStartScenario.max_document_chars default
            if no_source and len(active_text) <= 30 and len(paragraph_text) == 0:
                blank_document_allowed = True
                for gate_name in common_blockers:
                    check = common_checks[gate_name]
                    check["passed"] = True
                    check["reason"] = "blank_document_allowed"
                    check["blank_document_allowed"] = True
                common_blockers = []

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
                    "scheduler_trace": None,
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
        scheduler_trace: dict[str, Any] | None = None
        selected_name: str | None = None
        if schedule and ready_names and self.scheduler is not None:
            # 발동 시점의 정규화 문서 길이 — 글자수 기반 cooldown 판정용
            doc_chars = len(" ".join((filtered.active_editor_text or "").split()))
            scheduler_trace = {}
            if self.router is not None and ScenarioRouter.enabled():
                # LLM router owns selection; CFS keeps only its cheap reflexes
                # (global throttle + recency) around it.
                selected_name = self._route_with_llm(
                    document_key=current_snapshot["document_key"],
                    ready_names=ready_names,
                    scenario_results=scenario_results,
                    filtered=filtered,
                    last_fired_at=last_fired_at,
                    now=now,
                    doc_chars=doc_chars,
                    trace_out=scheduler_trace,
                )
            else:
                selected_name = self.scheduler.select_and_charge(
                    current_snapshot["document_key"],
                    ready_names,
                    now=now,
                    doc_chars=doc_chars,
                    trace_out=scheduler_trace,
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

        # Decision-level trace (only when a scenario actually became a candidate,
        # so idle captures stay silent). Shows the ready set with scores and the
        # selection outcome — selected scenario or why nothing fired.
        if ready_names:
            candidates = ", ".join(
                f"{name}({scenario_results[name].score:.2f})" for name in ready_names
            )
            if selected_name is not None:
                screen_trace(f"candidates=[{candidates}] -> selected={selected_name}")
            else:
                reason = (scheduler_trace or {}).get("rejected_reason") or "not_selected"
                screen_trace(f"candidates=[{candidates}] -> none ({reason})")

        if selected_name is None:
            blockers = [f"scenario:{name}:not_ready" for name in scenario_results if not scenario_results[name].ready]
            # ready 후보가 있었는데 throttle로 막힌 경우, 전역 blocker도 함께 표시
            if scheduler_trace and scheduler_trace.get("rejected_reason") == "global_throttle":
                blockers.append("global_throttle_active")
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
                    "scheduler_trace": scheduler_trace,
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
                "scheduler_trace": scheduler_trace,
            },
        )

    def _route_with_llm(
        self,
        *,
        document_key: str,
        ready_names: list[str],
        scenario_results: dict[str, ScenarioEvaluation],
        filtered: FilteredScreenContext,
        last_fired_at: dict[str, float],
        now: float,
        doc_chars: int,
        trace_out: dict[str, Any],
    ) -> str | None:
        """Pick one ready scenario via the LLM router (or decline). The global
        throttle still gates (anti-spam); a fire is recorded for recency."""
        if self.scheduler is not None and self.scheduler.is_globally_throttled(document_key, now=now):
            trace_out["rejected_reason"] = "global_throttle"
            trace_out["router"] = None
            screen_trace("router: skipped (global throttle)")
            return None
        candidates = [(name, list(scenario_results[name].reasons)) for name in ready_names]
        recent_text = " ".join(
            (filtered.current_paragraph_text or filtered.active_editor_text or "").split()
        )[:1500]
        focused_text = " ".join(
            (filtered.changed_text or filtered.current_paragraph_text or "").split()
        )[:400]
        decision = self.router.route(
            document_type="the user's working document",
            recent_text=recent_text,
            focused_text=focused_text,
            candidates=candidates,
            recent_fired=dict(last_fired_at),
            now=now,
        )
        trace_out["router"] = {
            "scenario": decision.scenario,
            "confidence": round(decision.confidence, 3),
            "reason": decision.reason,
        }
        screen_trace(
            f"router: pick={decision.scenario or 'none'} "
            f"conf={decision.confidence:.2f} ({decision.reason})"
        )
        if decision.scenario and decision.confidence >= self.ROUTER_MIN_CONFIDENCE:
            if self.scheduler is not None:
                self.scheduler.record_fire(document_key, decision.scenario, now=now, doc_chars=doc_chars)
            return decision.scenario
        trace_out.setdefault("rejected_reason", "router_declined")
        return None

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
