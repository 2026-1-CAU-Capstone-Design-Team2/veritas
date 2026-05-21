from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from .content_filter import ContentFilter
from .intervention_detector import InterventionDetector
from .intervention_dispatcher import InterventionDispatcher
from .models import AppTextResult, OcrResult, ScreenContextEvent, UiAutomationResult
from .ocr_engine import OcrEngine
from .powerpoint_com import PowerPointComReader
from .scenario_scheduler import ScenarioScheduler
from .scenario import (
    AcronymIntroducedScenario,
    BlankDocumentStartScenario,
    CitationMissingScenario,
    CodeBlockPresentScenario,
    CopyPasteGrowthScenario,
    FactualClaimMadeScenario,
    HeadingAddedScenario,
    IdleAfterWritingScenario,
    LargeDeletionScenario,
    LongParagraphWrittenScenario,
    LongStaticReviewScenario,
    ManyQuestionMarksScenario,
    NumberedListGrowthScenario,
    OutlinePhaseScenario,
    ParagraphChurnScenario,
    QuoteInsertedScenario,
    RepeatedPhraseInParagraphScenario,
    ScatteredEditsScenario,
    TodoMarkerPresentScenario,
    TransitionWordOveruseScenario,
    UndoCycleDetectedScenario,
    WeakModifierOveruseScenario,
    WholeDocumentReviewScenario,
)
from .screen_capture import ScreenCapture
from .store import ScreenContextStore
from .text_extraction_targets import is_text_extraction_target
from .ui_automation import UiAutomationReader
from .window_context import WindowContextReader


"""
producer: ScreenContextService 폴링 스레드 -> decide() -> dispatcher.dispatch() -> store.enqueue_intervention() (디스크 intervention_queue.json에 append)

consumer: ChatAgent._screen_intervention_loop — 별도 스레드. consume(limit=1) -> answer_screen_intervention()에서 ~30초 블로킹 LLM 호출 -> 반복
"""
class ScreenContextService:
    """OCR/PID를 주기적으로 수집하고 agent용 최종 context를 저장합니다."""
    # consumer 중단으로 인해 큐에 끼인 항목을 점유로 간주하지 않기 위해 LLM timeout 시간보다 큰 경우에만 큐에 남은 항목을 인플라이트로 간주하도록 함
    INTERVENTION_MAX_INFLIGHT_SEC = 300.0

    # 수집기/필터/detector/scheduler/dispatcher 구성, 공유 시나리오 리스트 주입
    def __init__(
        self,
        root: str | Path,
        *,
        interval_sec: float = 3.0,
        ocr_language: str = "ko-KR",
        ocr_scale: float = 2.0,
        crop_left: int = 0,
        crop_top: int = 0,
        crop_right: int = 0,
        crop_bottom: int = 0,
        console_log: bool = False,
    ) -> None:
        self.interval_sec = interval_sec
        self.console_log = console_log
        self.window_reader = WindowContextReader()
        self.screen_capture = ScreenCapture(
            crop_left=crop_left,
            crop_top=crop_top,
            crop_right=crop_right,
            crop_bottom=crop_bottom,
        )
        self.ocr_engine = OcrEngine(language=ocr_language, scale=ocr_scale)
        self.powerpoint_reader = PowerPointComReader()
        self.ui_reader = UiAutomationReader()
        self.content_filter = ContentFilter()
        # console_log(=--screen-debug)면 capture log를 debug/에 기록
        self.store = ScreenContextStore(root, debug=console_log)

        # Build one shared scenario list and inject it into every component
        # that depends on it (detector, scheduler, dispatcher). This replaces
        # the previous sequence where the detector's default scenarios were
        # used to seed the scheduler and the scheduler was then attached back
        # to the detector via a post-construction setter — which would silently
        # drift if anyone replaced detector.scenarios at runtime.
        scenarios = [
            # Phase 1-3 (기존 5개)
            IdleAfterWritingScenario(),
            WholeDocumentReviewScenario(),
            LongStaticReviewScenario(),
            ParagraphChurnScenario(),
            BlankDocumentStartScenario(),
            # Phase 4 — Tier 1 (현재 캡처 payload 기반, 8개)
            OutlinePhaseScenario(),
            AcronymIntroducedScenario(),
            HeadingAddedScenario(),
            LongParagraphWrittenScenario(),
            NumberedListGrowthScenario(),
            TodoMarkerPresentScenario(),
            ManyQuestionMarksScenario(),
            CodeBlockPresentScenario(),
            # Phase 4 — Tier 2-A (텍스트 패턴, 6개)
            QuoteInsertedScenario(),
            CitationMissingScenario(),
            FactualClaimMadeScenario(),
            RepeatedPhraseInParagraphScenario(),
            TransitionWordOveruseScenario(),
            WeakModifierOveruseScenario(),
            # Phase 4 — Tier 2-B (캡처간 diff, 4개)
            ScatteredEditsScenario(),
            LargeDeletionScenario(),
            CopyPasteGrowthScenario(),
            UndoCycleDetectedScenario(),
        ]
        self.scenario_scheduler = ScenarioScheduler(
            self.store,
            scenarios=scenarios,
            console_log=console_log,
        )
        self.intervention_detector = InterventionDetector(
            scenarios=scenarios,
            scheduler=self.scenario_scheduler,
        )
        self.intervention_dispatcher = InterventionDispatcher(
            self.store,
            scenarios={s.name: s for s in scenarios},
            console_log=console_log,
        )

        self._previous_active_text = ""
        self._last_poll_error: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._debug_stats = self._new_debug_stats()

    # 한 번 캡처: 텍스트 수집 -> filtered -> decide -> 이벤트 저장 -> dispatch
    def capture_once(self) -> ScreenContextEvent:
        window = self.window_reader.read_foreground()
        app_text = self._read_app_text_first(window)
        ui_automation = (
            UiAutomationResult(error="skipped: app text extraction succeeded.")
            if self._is_usable_app_text(app_text)
            else self._read_text_first(window)
        )
        if self._is_usable_app_text(app_text):
            ocr = OcrResult(
                language=self.ocr_engine.language,
                error="skipped: app text extraction succeeded.",
            )
        elif self._is_usable_text_source(ui_automation):
            ocr = OcrResult(
                language=self.ocr_engine.language,
                error="skipped: UI Automation text extraction succeeded.",
            )
        elif ui_automation.reject_reason == "empty_text":
            # UIA found and read the focused editor control but it is genuinely
            # empty (reject_reason="empty_text"). Do NOT fall back to OCR: OCR
            # would just read the app's menus/toolbars as noise. Treat it as an
            # empty document so BlankDocumentStartScenario handles it instead.
            ocr = OcrResult(
                language=self.ocr_engine.language,
                error="skipped: UIA read an empty editor (authoritative; no OCR fallback).",
            )
        else:
            image = self.screen_capture.capture_window(window)
            ocr = self.ocr_engine.recognize(image)

        filtered = self.content_filter.build(
            window=window,
            app_text=app_text,
            ocr=ocr,
            ui_automation=ui_automation,
            previous_text=self._previous_active_text,
        )
        history_events = self.store.load_recent(self.intervention_detector.history_window - 1)
        pipeline_busy = self._intervention_pipeline_busy()
        intervention = self.intervention_detector.decide(
            window=window,
            filtered=filtered,
            history_events=history_events,
            schedule=not pipeline_busy,
        )
        diagnostics = self.diagnose_capture(
            window=window,
            app_text=app_text,
            ui_automation=ui_automation,
            ocr=ocr,
            filtered=filtered,
            intervention=intervention,
        )

        self._previous_active_text = filtered.active_editor_text
        event = ScreenContextEvent.new(
            event_id=self._new_event_id(),
            window=window,
            ocr=ocr,
            app_text=app_text,
            ui_automation=ui_automation,
            filtered=filtered,
            intervention=intervention,
            diagnostics=diagnostics,
        )
        self.store.save_event(event)
        self.intervention_dispatcher.dispatch(event)
        self._log_capture_event(event)
        return event

    # 큐에 in-flight 개입이 있으면 True. consumer가 LLM 처리 동안 큐에서 제거하지 않으므로 큐 non-empty = 파이프라인 점유
    # consumer 중단으로 끼인 항목(captured_at age > INTERVENTION_MAX_INFLIGHT_SEC)은 제외
    def _intervention_pipeline_busy(self) -> bool:
        pending = self.store.load_pending_interventions(limit=1)
        if not pending:
            return False
        captured_at = str(pending[0].get("captured_at") or "").strip()
        if not captured_at:
            return True
        try:
            age = (datetime.now() - datetime.fromisoformat(captured_at)).total_seconds()
        except ValueError:
            return True
        return age <= self.INTERVENTION_MAX_INFLIGHT_SEC

    # 저장된 이벤트로부터 진단 dict를 재계산
    def diagnose_event(self, event: ScreenContextEvent) -> dict:
        return self.diagnose_capture(
            window=event.window,
            app_text=event.app_text,
            ui_automation=event.ui_automation,
            ocr=event.ocr,
            filtered=event.filtered,
            intervention=event.intervention,
        )

    # 캡처 구성요소로 진단 dict 생성 (텍스트 유무/소스/confidence/개입 여부)
    def diagnose_capture(
        self,
        *,
        window,
        app_text: AppTextResult,
        ui_automation: UiAutomationResult,
        ocr: OcrResult,
        filtered,
        intervention,
    ) -> dict:
        text = (filtered.active_editor_text or "").strip()
        current_paragraph = (filtered.current_paragraph_text or "").strip()
        text_source = self._diagnose_text_source(
            app_text=app_text,
            ui_automation=ui_automation,
            ocr=ocr,
            filtered=filtered,
        )
        has_foreground_window = bool(window.hwnd and not window.error)
        has_text = bool(text)
        has_current_paragraph = bool(current_paragraph)
        usable_for_llm = bool(has_foreground_window and has_text and filtered.confidence > 0)

        return {
            "has_foreground_window": has_foreground_window,
            "has_text": has_text,
            "has_current_paragraph": has_current_paragraph,
            "text_source": text_source,
            "confidence": filtered.confidence,
            "active_text_chars": len(text),
            "current_paragraph_chars": len(current_paragraph),
            "browser_url": ui_automation.browser_url,
            "usable_for_llm": usable_for_llm,
            "intervention_queued": bool(intervention.should_consider_llm),
            "intervention_blockers": (
                (intervention.metadata or {}).get("blockers")
                if not intervention.should_consider_llm
                else []
            ),
            "errors": {
                "window": window.error,
                "app_text": app_text.error,
                "ui_automation": ui_automation.error,
                "ocr": ocr.error,
            },
        }

    # active_editor_text가 어느 소스(app_text/UIA/OCR)에서 왔는지 판별
    def _diagnose_text_source(
        self,
        *,
        app_text: AppTextResult,
        ui_automation: UiAutomationResult,
        ocr: OcrResult,
        filtered,
    ) -> str:
        active_text = (filtered.active_editor_text or "").strip()
        if not active_text:
            return "none"
        if (app_text.text or "").strip() == active_text:
            return app_text.text_source or "app_text"
        if (ui_automation.text or "").strip() == active_text:
            return ui_automation.text_source or "ui_automation"
        if (ocr.text or "").strip() == active_text:
            return "ocr"
        return "filtered"

    # scenario_scheduler 시작 + 폴링 스레드 기동
    def start_polling(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._debug_stats = self._new_debug_stats()
        self._debug_stats["started_at"] = datetime.now().isoformat(timespec="seconds")
        self.scenario_scheduler.start()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    # 폴링 스레드 정지 + scenario_scheduler 정지(상태 flush)
    def stop_polling(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.interval_sec + 1)
        self.scenario_scheduler.stop()
        # debug 모드면 세션 통계 1줄을 capture log 끝에 append
        if self.console_log:
            self._write_debug_stats_summary()

    # 폴링 스레드가 살아있는지 여부
    def is_polling(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # 마지막 폴링 사이클의 에러 메시지 (없으면 None)
    def last_poll_error(self) -> str | None:
        return self._last_poll_error

    # interval_sec 주기로 capture_once를 반복 실행하는 폴링 루프
    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            started_at = time.monotonic()
            try:
                self.capture_once()
                self._last_poll_error = None
            except Exception as exc:
                self._last_poll_error = f"{type(exc).__name__}: {exc}"
                if self.console_log:
                    print(f"[screen_context][poll][error] {self._last_poll_error}")
            elapsed = time.monotonic() - started_at
            remaining = max(self.interval_sec - elapsed, 0.0)
            self._stop_event.wait(remaining)

    # 캡처 이벤트를 capture log에 기록, console_log면 콘솔에도 출력
    def _log_capture_event(self, event: ScreenContextEvent) -> None:
        diagnostics = event.diagnostics or {}
        blockers = diagnostics.get("intervention_blockers") or []
        errors = diagnostics.get("errors") or {}
        if not isinstance(errors, dict):
            errors = {}

        window = event.window
        title = " ".join((window.window_title or "").split())
        if len(title) > 120:
            title = title[:117] + "..."

        log_payload = {
            "type": "screen_capture",
            "event_id": event.event_id,
            "captured_at": event.captured_at,
            "window": {
                "process_name": window.process_name,
                "window_title": window.window_title,
                "pid": window.pid,
                "hwnd": window.hwnd,
            },
            "browser_url": event.ui_automation.browser_url,
            "diagnostics": diagnostics,
            "filtered": {
                "active_app_type": event.filtered.active_app_type,
                "active_text_chars": len((event.filtered.active_editor_text or "").strip()),
                "current_paragraph_chars": len((event.filtered.current_paragraph_text or "").strip()),
                "changed_text_chars": len((event.filtered.changed_text or "").strip()),
                "current_paragraph_source": event.filtered.current_paragraph_source,
            },
            "intervention": {
                "should_consider_llm": event.intervention.should_consider_llm,
                "intervention_type": event.intervention.intervention_type,
                "score": event.intervention.score,
                "priority": event.intervention.priority,
                "reason_codes": event.intervention.reason_codes,
                "metadata": event.intervention.metadata,
            },
        }
        self.store.append_capture_log(log_payload)

        if not self.console_log:
            return

        self._accumulate_debug_stats(event)

        base = (
            f"[screen_context][capture] event={event.event_id} "
            f"process={window.process_name or '-'} "
            f"title={title!r} "
            f"source={diagnostics.get('text_source', 'unknown')} "
            f"chars={diagnostics.get('active_text_chars', 0)} "
            f"confidence={diagnostics.get('confidence', 0.0)} "
            f"queued={diagnostics.get('intervention_queued', False)} "
            f"type={event.intervention.intervention_type}"
        )
        if blockers:
            base += f" blockers={blockers}"
        print(base)
        if diagnostics.get("has_text"):
            self._log_debug_text(event, diagnostics)
        self._log_debug_decision(event)

        if not diagnostics.get("has_foreground_window"):
            print(
                "[screen_context][capture][warn] "
                f"event={event.event_id} window_error={errors.get('window') or 'unknown'}"
            )
        elif not diagnostics.get("has_text"):
            extractor_errors = {
                key: value
                for key, value in errors.items()
                if key in {"app_text", "ui_automation", "ocr"} and value
            }
            print(
                "[screen_context][capture][warn] "
                f"event={event.event_id} no_readable_text errors={extractor_errors}"
            )

    # debug 모드 세션 통계 누적기 초기 구조
    def _new_debug_stats(self) -> dict:
        return {
            "started_at": None,
            "total_captures": 0,
            "common_passed": 0,
            "common_blocked": 0,
            "block_reasons": {},
            "scenarios": {},
            "interventions_queued": 0,
        }

    # 캡처 1건의 결정 결과를 debug 세션 통계에 누적
    def _accumulate_debug_stats(self, event: ScreenContextEvent) -> None:
        stats = self._debug_stats
        stats["total_captures"] += 1
        meta = event.intervention.metadata or {}
        common_checks = meta.get("common_checks") or {}
        blockers = [
            name
            for name, check in common_checks.items()
            if isinstance(check, dict) and not check.get("passed")
        ]
        if blockers:
            stats["common_blocked"] += 1
            for name in blockers:
                stats["block_reasons"][name] = stats["block_reasons"].get(name, 0) + 1
        else:
            stats["common_passed"] += 1
        for name, scenario in (meta.get("scenarios") or {}).items():
            if not isinstance(scenario, dict):
                continue
            entry = stats["scenarios"].setdefault(name, {"ready": 0, "selected": 0})
            if scenario.get("ready"):
                entry["ready"] += 1
        selected = meta.get("selected")
        if selected:
            entry = stats["scenarios"].setdefault(selected, {"ready": 0, "selected": 0})
            entry["selected"] += 1
        if event.intervention.should_consider_llm:
            stats["interventions_queued"] += 1

    # debug 모드 종료 시 세션 통계 1줄을 capture log 끝에 append
    def _write_debug_stats_summary(self) -> None:
        stats = self._debug_stats
        summary = {
            "type": "session_stats",
            "session_id": self.store.capture_session_id,
            "started_at": stats.get("started_at"),
            "ended_at": datetime.now().isoformat(timespec="seconds"),
            "total_captures": stats["total_captures"],
            "common_gate": {
                "passed": stats["common_passed"],
                "blocked": stats["common_blocked"],
                "block_reasons": stats["block_reasons"],
            },
            "scenarios": stats["scenarios"],
            "interventions_queued": stats["interventions_queued"],
        }
        self.store.append_capture_log(summary)

    # 디버그용: 추출 텍스트 길이·미리보기를 콘솔 출력
    def _log_debug_text(self, event: ScreenContextEvent, diagnostics: dict) -> None:
        filtered = event.filtered
        print(
            "[screen_context][text] "
            f"source={diagnostics.get('text_source', 'unknown')} "
            f"active_chars={len((filtered.active_editor_text or '').strip())} "
            f"paragraph_chars={len((filtered.current_paragraph_text or '').strip())} "
            f"changed_chars={len((filtered.changed_text or '').strip())} "
            f"preview={self._preview_text(filtered.current_paragraph_text or filtered.active_editor_text)!r}"
        )
        if (event.ocr.text or "").strip():
            print(
                "[screen_context][ocr] "
                f"chars={len((event.ocr.text or '').strip())} "
                f"lines={len(event.ocr.lines or [])} "
                f"preview={self._preview_text(event.ocr.text)!r}"
            )

    # 디버그용: 공통 게이트/시나리오 게이트/선택 결과를 콘솔 출력
    def _log_debug_decision(self, event: ScreenContextEvent) -> None:
        metadata = event.intervention.metadata or {}
        common_checks = metadata.get("common_checks") or {}
        if isinstance(common_checks, dict):
            for name in ("editing_app", "dwell", "stable_paragraph"):
                check = common_checks.get(name) or {}
                if not isinstance(check, dict):
                    continue
                status = "PASS" if check.get("passed") else "BLOCK"
                detail = self._format_common_check_detail(name, check)
                print(f"[screen_context][decision] common.{name}={status} {detail}".rstrip())

        scenarios = metadata.get("scenarios") or {}
        if isinstance(scenarios, dict):
            for scenario_name, scenario in scenarios.items():
                if not isinstance(scenario, dict):
                    continue
                status = "READY" if scenario.get("ready") else "WAIT"
                gates = scenario.get("gate_results") or {}
                gate_summary = []
                if isinstance(gates, dict):
                    for gate_name, gate in gates.items():
                        if not isinstance(gate, dict):
                            continue
                        gate_status = "P" if gate.get("passed") else "B"
                        gate_summary.append(f"{gate_name}={gate_status}")
                gate_text = " ".join(gate_summary)
                print(
                    f"[screen_context][decision] scenario.{scenario_name}={status} "
                    f"score={scenario.get('score', 0.0)} {gate_text}".rstrip()
                )

        selected = metadata.get("selected")
        if selected:
            scheduler = metadata.get("scheduler") or {}
            vruntimes = scheduler.get("vruntimes") if isinstance(scheduler, dict) else None
            print(
                f"[screen_context][decision] selected={selected} "
                f"vruntimes={vruntimes}"
            )

    # 공통 게이트별 디버그 출력 문자열 포맷
    def _format_common_check_detail(self, name: str, check: dict) -> str:
        reason = str(check.get("reason") or "")
        if name == "editing_app":
            return f"reason={reason} app_type={check.get('active_app_type') or '-'}"
        if name == "dwell":
            return (
                f"reason={reason} "
                f"history={check.get('history_count')}/{check.get('min_history_count')} "
                f"dwell={check.get('dwell_ratio')}/{check.get('dwell_threshold')}"
            )
        if name == "stable_paragraph":
            return (
                f"reason={reason} "
                f"source={check.get('current_paragraph_source') or '-'} "
                f"chars={check.get('current_paragraph_chars')} "
                f"min={check.get('min_paragraph_chars')} "
                f"ocr_min={check.get('min_ocr_paragraph_chars')} "
                f"confidence={check.get('confidence')}"
            )
        return f"reason={reason}"

    # 텍스트를 공백 정규화 후 limit 길이로 잘라 미리보기 문자열 생성
    def _preview_text(self, text: str, *, limit: int = 220) -> str:
        value = " ".join(str(text or "").split()).strip()
        if len(value) <= limit:
            return value
        return value[: limit - 3] + "..."

    # 현재 시각 기반 이벤트 ID 생성 (YYYYMMDD_HHMMSS_us)
    def _new_event_id(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    # 텍스트 추출 대상 앱이면 UI Automation으로 focused 텍스트를 읽음
    def _read_text_first(self, window) -> UiAutomationResult:
        if not self._is_text_extraction_target(window):
            return UiAutomationResult(error="skipped: foreground app is not a text extraction target.")
        return self.ui_reader.read_focused(window)

    # foreground 앱이 텍스트 추출 대상인지(프로세스명·확장자) 판별
    def _is_text_extraction_target(self, window) -> bool:
        return is_text_extraction_target(window)

    # UI Automation 결과가 쓸 만한지(텍스트 존재 + 품질) 판별
    def _is_usable_text_source(self, result: UiAutomationResult) -> bool:
        return bool(result.text and result.source_quality in {"primary", "usable"})

    # 앱 전용 reader로 텍스트를 읽음 (현재 PowerPoint COM만)
    def _read_app_text_first(self, window) -> AppTextResult:
        if (window.process_name or "").lower() == "powerpnt.exe":
            return self.powerpoint_reader.read_active_slide(window)
        return AppTextResult(error="skipped: no app-specific text reader.")

    # 앱 전용 추출 결과가 쓸 만한지(텍스트 존재 + 품질) 판별
    def _is_usable_app_text(self, result: AppTextResult) -> bool:
        return bool(result.text and result.source_quality in {"primary", "usable"})
