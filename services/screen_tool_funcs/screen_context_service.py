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
    ) -> None:
        self.interval_sec = interval_sec
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
        self.intervention_detector = InterventionDetector()
        self.store = ScreenContextStore(root)
        self.intervention_dispatcher = InterventionDispatcher(self.store)

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

        self._previous_active_text = filtered.active_editor_text
        event = ScreenContextEvent.new(
            event_id=self._new_event_id(),
            window=window,
            ocr=ocr,
            app_text=app_text,
            ui_automation=ui_automation,
            filtered=filtered,
            intervention=intervention,
        )
        self.store.save_event(event)
        self.intervention_dispatcher.dispatch(event)
        return event

    def start_polling(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop_polling(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.interval_sec + 1)

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
            elapsed = time.monotonic() - started_at
            remaining = max(self.interval_sec - elapsed, 0.0)
            self._stop_event.wait(remaining)

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
