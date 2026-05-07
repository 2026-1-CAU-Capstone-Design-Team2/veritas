from __future__ import annotations

import re

from .models import AppTextResult, FilteredScreenContext, OcrResult, UiAutomationResult, WindowContext


class ContentFilter:
    def build(
        self,
        *,
        window: WindowContext,
        ocr: OcrResult,
        app_text: AppTextResult | None = None,
        ui_automation: UiAutomationResult | None = None,
        previous_text: str = "",
    ) -> FilteredScreenContext:
        app_text_value = self._normalize(app_text.text) if app_text else ""
        ui_is_usable = self._is_usable_ui_source(ui_automation)
        ui_text = self._normalize(ui_automation.text) if ui_is_usable and ui_automation else ""
        ui_current_paragraph = (
            self._normalize(ui_automation.current_paragraph_text)
            if ui_is_usable and ui_automation
            else ""
        )
        ocr_text = self._normalize(ocr.text)

        active_text = app_text_value or ui_text or ocr_text
        current_paragraph = self._resolve_current_paragraph(
            app_text=app_text_value,
            ui_text=ui_text,
            ui_current_paragraph=ui_current_paragraph,
            ocr_text=ocr_text,
        )
        current_paragraph_source = self._resolve_current_paragraph_source(
            app_text=app_text_value,
            ui_text=ui_text,
            ui_current_paragraph=ui_current_paragraph,
            ocr_text=ocr_text,
            ui_automation=ui_automation,
        )
        changed_text = self._diff_suffix(previous_text, active_text)

        return FilteredScreenContext(
            active_app_type=self._guess_app_type(window),
            active_editor_text=active_text,
            current_paragraph_text=current_paragraph,
            current_paragraph_source=current_paragraph_source,
            current_paragraph_rect=(
                ui_automation.current_paragraph_rect
                if ui_is_usable and ui_automation and current_paragraph_source.startswith("uia_")
                else None
            ),
            visible_context=active_text,
            changed_text=changed_text,
            confidence=self._score_confidence(
                app_text=app_text_value,
                ui_text=ui_text,
                ocr_text=ocr_text,
            ),
            noise_removed=[],
        )

    def _normalize(self, text: str) -> str:
        text = text.replace("\r\n", "\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _diff_suffix(self, previous: str, current: str) -> str:
        if not current:
            return ""
        if not previous or not current.startswith(previous):
            return current
        return current[len(previous):].strip()

    def _guess_app_type(self, window: WindowContext) -> str:
        name = window.process_name.lower()
        title = window.window_title.lower()

        if name in {"winword.exe", "hwp.exe"} or ".doc" in title:
            return "document"
        if name in {"powerpnt.exe"} or ".ppt" in title:
            return "presentation"
        if name in {"excel.exe"} or ".xls" in title:
            return "spreadsheet"
        if name in {"chrome.exe", "msedge.exe", "firefox.exe"}:
            return "browser"
        if name in {"code.exe", "devenv.exe", "pycharm64.exe"}:
            return "code_editor"
        return "unknown"

    def _score_confidence(self, *, app_text: str, ui_text: str, ocr_text: str) -> float:
        if app_text:
            return 0.95
        if ui_text:
            return 0.9
        if ocr_text:
            return 0.55
        return 0.0

    def _is_usable_ui_source(self, ui_automation: UiAutomationResult | None) -> bool:
        if ui_automation is None:
            return False
        return bool(
            ui_automation.source_quality in {"primary", "usable"}
            and not ui_automation.error
        )

    def _resolve_current_paragraph(
        self,
        *,
        app_text: str,
        ui_text: str,
        ui_current_paragraph: str,
        ocr_text: str,
    ) -> str:
        if ui_current_paragraph:
            return ui_current_paragraph
        if ocr_text and not app_text and not ui_text:
            return ocr_text
        if app_text and not ui_text:
            return app_text
        return ""

    def _resolve_current_paragraph_source(
        self,
        *,
        app_text: str,
        ui_text: str,
        ui_current_paragraph: str,
        ocr_text: str,
        ui_automation: UiAutomationResult | None,
    ) -> str:
        if ui_current_paragraph and ui_automation:
            source = ui_automation.current_paragraph_source or "text_range"
            return f"uia_{source}"
        if ocr_text and not app_text and not ui_text:
            return "ocr_same_as_full_text"
        if app_text and not ui_text:
            return "app_text_same_as_full_text"
        return ""
