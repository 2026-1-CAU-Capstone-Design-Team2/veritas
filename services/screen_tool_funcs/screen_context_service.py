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
from .screen_capture import ScreenCapture
from .store import ScreenContextStore
from .ui_automation import UiAutomationReader
from .window_context import WindowContextReader


class ScreenContextService:
    """OCR/PID를 주기적으로 수집하고 agent용 최종 context를 저장합니다."""

    def __init__(
        self,
        root: str | Path,
        *,
        interval_sec: float = 5.0,
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
        self.store = ScreenContextStore(root)
        self.intervention_detector = InterventionDetector()
        self.scenario_scheduler = ScenarioScheduler(
            self.store,
            weights=self.intervention_detector.scenario_weights,
            console_log=console_log,
        )
        self.intervention_detector.scheduler = self.scenario_scheduler
        self.intervention_dispatcher = InterventionDispatcher(
            self.store,
            console_log=console_log,
        )

        self._previous_active_text = ""
        self._last_poll_error: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

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
        intervention = self.intervention_detector.decide(
            window=window,
            filtered=filtered,
            history_events=history_events,
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

    def diagnose_event(self, event: ScreenContextEvent) -> dict:
        return self.diagnose_capture(
            window=event.window,
            app_text=event.app_text,
            ui_automation=event.ui_automation,
            ocr=event.ocr,
            filtered=event.filtered,
            intervention=event.intervention,
        )

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

    def start_polling(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self.scenario_scheduler.start()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop_polling(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.interval_sec + 1)
        self.scenario_scheduler.stop()

    def is_polling(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def last_poll_error(self) -> str | None:
        return self._last_poll_error

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

    def _preview_text(self, text: str, *, limit: int = 220) -> str:
        value = " ".join(str(text or "").split()).strip()
        if len(value) <= limit:
            return value
        return value[: limit - 3] + "..."

    def _new_event_id(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    def _read_text_first(self, window) -> UiAutomationResult:
        if not self._is_text_extraction_target(window):
            return UiAutomationResult(error="skipped: foreground app is not a text extraction target.")
        return self.ui_reader.read_focused(window)

    def _is_text_extraction_target(self, window) -> bool:
        process_name = (window.process_name or "").lower()
        if process_name in {
            "notepad.exe",
            "winword.exe",
            "excel.exe",
            "powerpnt.exe",
            "docs.exe",
            "notepad++.exe",
            "notion.exe",
            "word.exe",
            "hwp.exe",
            "code.exe",
            "devenv.exe",
            "pycharm64.exe",
        }:
            return True

        title = (window.window_title or "").lower()
        return any(title.endswith(ext) or ext in title for ext in (".txt", ".md", ".doc", ".hwp", ".ppt", ".pptx"))

    def _is_usable_text_source(self, result: UiAutomationResult) -> bool:
        return bool(result.text and result.source_quality in {"primary", "usable"})

    def _read_app_text_first(self, window) -> AppTextResult:
        if (window.process_name or "").lower() == "powerpnt.exe":
            return self.powerpoint_reader.read_active_slide(window)
        return AppTextResult(error="skipped: no app-specific text reader.")

    def _is_usable_app_text(self, result: AppTextResult) -> bool:
        return bool(result.text and result.source_quality in {"primary", "usable"})
