from __future__ import annotations

from typing import Any

from ..core.models import BoundingBox, UiAutomationResult, WindowContext
from .text_extraction_targets import is_text_extraction_target


class UiAutomationReader:
    """Read focused text controls and derive the current paragraph via TextRange."""

    def read_focused(self, window: WindowContext) -> UiAutomationResult:
        try:
            import uiautomation as auto

            focused = auto.GetFocusedControl()
            if focused is None:
                return UiAutomationResult(
                    reject_reason="no_focused_control",
                    error="No focused UI Automation control.",
                )

            bounding_rect = self._control_bounding_box(focused)
            if bounding_rect and not self._is_inside_window(bounding_rect, window):
                return UiAutomationResult(
                    bounding_rect=bounding_rect,
                    reject_reason="outside_foreground_window",
                    error="Focused control is outside foreground window.",
                )

            text_pattern = self._get_pattern(focused, "GetTextPattern")
            value_text = self._read_value_text(focused)
            document_text = self._read_document_text(text_pattern)
            text = document_text or value_text or (focused.Name or "")
            text_source = "text_pattern" if document_text else "value_pattern" if value_text else "control_name"

            selection = self._read_selection_paragraph(auto, text_pattern)
            hover = self._read_hover_paragraph(auto, window)
            browser_url = self._read_browser_url(auto, window)
            current_text = selection["paragraph_text"] or hover["paragraph_text"]
            current_source = selection["source"] or hover["source"]
            current_rect = selection["paragraph_rect"] or hover["paragraph_rect"]

            result = UiAutomationResult(
                focused_name=focused.Name or "",
                control_type=str(focused.ControlTypeName or ""),
                automation_id=focused.AutomationId or "",
                class_name=focused.ClassName or "",
                bounding_rect=bounding_rect,
                text=text,
                text_source=text_source,
                selection_text=selection["selection_text"],
                current_paragraph_text=current_text,
                current_paragraph_source=current_source,
                current_paragraph_rect=current_rect,
                hover_text=hover["paragraph_text"],
                hover_rect=hover["paragraph_rect"],
                mouse_position=hover["mouse_position"],
                browser_url=browser_url,
            )
            if self._is_control_name_only_empty_editor(result, window):
                result.text = ""
                result.text_source = ""

            if not result.text.strip() and not current_text.strip():
                result.reject_reason = "empty_text"
                result.error = "Focused control has no readable text."
                return result

            quality, reject_reason = self._judge_source_quality(result, window)
            result.source_quality = quality
            result.reject_reason = reject_reason
            if quality in {"rejected", "weak"}:
                result.error = f"UI Automation text source is {quality}: {reject_reason}"
            return result
        except Exception as exc:
            return UiAutomationResult(reject_reason="exception", error=str(exc))

    def _read_browser_url(self, auto: Any, window: WindowContext) -> str:
        if (window.process_name or "").lower() not in {"chrome.exe", "msedge.exe", "firefox.exe"}:
            return ""

        root = None
        try:
            root = auto.ControlFromHandle(window.hwnd) if window.hwnd else None
        except Exception:
            root = None
        if root is None:
            try:
                root = auto.GetFocusedControl()
                while root is not None and getattr(root, "GetParentControl", None):
                    parent = root.GetParentControl()
                    if parent is None:
                        break
                    root = parent
            except Exception:
                return ""

        candidates = self._browser_url_candidates(auto, root)
        for candidate in candidates:
            text = self._read_value_text(candidate) or str(getattr(candidate, "Name", "") or "")
            url = self._normalize_browser_url(text)
            if url:
                return url
        return ""

    def _browser_url_candidates(self, auto: Any, root: Any) -> list[Any]:
        candidates: list[Any] = []
        try:
            edit_controls = root.GetChildren() or []
        except Exception:
            edit_controls = []

        stack = list(edit_controls)
        visited = 0
        while stack and visited < 300:
            visited += 1
            control = stack.pop(0)
            try:
                control_type = str(control.ControlTypeName or "").lower()
                name = str(control.Name or "").lower()
                automation_id = str(control.AutomationId or "").lower()
                class_name = str(control.ClassName or "").lower()
            except Exception:
                continue

            if (
                "edit" in control_type
                and (
                    "address" in name
                    or "주소" in name
                    or "url" in name
                    or "omnibox" in name
                    or "address" in automation_id
                    or "omnibox" in class_name
                )
            ):
                candidates.append(control)

            try:
                stack.extend(control.GetChildren() or [])
            except Exception:
                continue

        return candidates

    def _normalize_browser_url(self, text: str) -> str:
        value = " ".join(str(text or "").split()).strip()
        if not value:
            return ""
        lower = value.lower()
        if lower.startswith(("http://", "https://")):
            return value
        if lower.startswith(("docs.google.com/", "drive.google.com/", "hancomdocs.com/", "docs.hancom.com/")):
            return f"https://{value}"
        return ""

    def _get_pattern(self, control: Any, name: str) -> Any | None:
        try:
            getter = getattr(control, name, None)
            return getter() if getter else None
        except Exception:
            return None

    def _read_value_text(self, control: Any) -> str:
        pattern = self._get_pattern(control, "GetValuePattern")
        try:
            return str(pattern.Value or "") if pattern is not None else ""
        except Exception:
            return ""

    def _read_document_text(self, text_pattern: Any | None) -> str:
        try:
            if text_pattern is None or not hasattr(text_pattern, "DocumentRange"):
                return ""
            return str(text_pattern.DocumentRange.GetText(-1) or "")
        except Exception:
            return ""

    def _read_selection_paragraph(self, auto: Any, text_pattern: Any | None) -> dict[str, Any]:
        empty = self._empty_range_result("selection_paragraph")
        if text_pattern is None:
            return empty

        try:
            ranges = text_pattern.GetSelection() or []
        except Exception:
            return empty

        for text_range in ranges:
            result = self._range_to_paragraph(auto, text_range, source="selection_paragraph")
            if result["paragraph_text"]:
                return result
        return empty

    def _read_hover_paragraph(self, auto: Any, window: WindowContext) -> dict[str, Any]:
        empty = self._empty_range_result("hover_paragraph")
        position = self._cursor_position(auto)
        empty["mouse_position"] = position
        if position is None or not self._point_inside_window(position[0], position[1], window):
            return empty

        try:
            control = auto.ControlFromPoint(position[0], position[1])
        except Exception:
            return empty
        if control is None:
            return empty

        text_pattern = self._get_pattern(control, "GetTextPattern")
        if text_pattern is None:
            return empty

        try:
            text_range = text_pattern.RangeFromPoint(position[0], position[1])
        except Exception:
            return empty
        if text_range is None:
            return empty

        result = self._range_to_paragraph(auto, text_range, source="hover_paragraph")
        result["mouse_position"] = position
        return result

    def _range_to_paragraph(self, auto: Any, text_range: Any, *, source: str) -> dict[str, Any]:
        result = self._empty_range_result(source)
        try:
            result["selection_text"] = str(text_range.GetText(-1) or "").strip()
        except Exception:
            result["selection_text"] = ""

        try:
            paragraph_range = text_range.Clone()
            paragraph_range.ExpandToEnclosingUnit(auto.TextUnit.Paragraph)
            result["paragraph_text"] = str(paragraph_range.GetText(-1) or "").strip()
            result["paragraph_rect"] = self._range_bounding_box(paragraph_range)
        except Exception:
            return result
        return result

    def _empty_range_result(self, source: str) -> dict[str, Any]:
        return {
            "source": source,
            "selection_text": "",
            "paragraph_text": "",
            "paragraph_rect": None,
            "mouse_position": None,
        }

    def _control_bounding_box(self, control: Any) -> BoundingBox | None:
        try:
            rect = control.BoundingRectangle
            return BoundingBox(
                x=int(rect.left),
                y=int(rect.top),
                width=int(rect.right - rect.left),
                height=int(rect.bottom - rect.top),
            )
        except Exception:
            return None

    def _range_bounding_box(self, text_range: Any) -> BoundingBox | None:
        try:
            rects = text_range.GetBoundingRectangles() or []
        except Exception:
            return None
        boxes = []
        for rect in rects:
            try:
                width = int(rect.right - rect.left)
                height = int(rect.bottom - rect.top)
                if width > 0 and height > 0:
                    boxes.append(
                        BoundingBox(
                            x=int(rect.left),
                            y=int(rect.top),
                            width=width,
                            height=height,
                        )
                    )
            except Exception:
                continue
        if not boxes:
            return None

        left = min(box.x for box in boxes)
        top = min(box.y for box in boxes)
        right = max(box.x + box.width for box in boxes)
        bottom = max(box.y + box.height for box in boxes)
        return BoundingBox(x=left, y=top, width=right - left, height=bottom - top)

    def _cursor_position(self, auto: Any) -> list[int] | None:
        try:
            point = auto.GetCursorPos()
        except Exception:
            return None
        try:
            return [int(point[0]), int(point[1])]
        except Exception:
            pass
        try:
            return [int(point.x), int(point.y)]
        except Exception:
            return None

    def _is_inside_window(self, rect: BoundingBox, window: WindowContext) -> bool:
        if rect.width <= 0 or rect.height <= 0:
            return False
        center_x = rect.x + rect.width // 2
        center_y = rect.y + rect.height // 2
        return self._point_inside_window(center_x, center_y, window)

    def _point_inside_window(self, x: int, y: int, window: WindowContext) -> bool:
        if window.rect is None:
            return False
        window_right = window.rect.x + window.rect.width
        window_bottom = window.rect.y + window.rect.height
        return window.rect.x <= x <= window_right and window.rect.y <= y <= window_bottom

    def _judge_source_quality(
        self,
        result: UiAutomationResult,
        window: WindowContext,
    ) -> tuple[str, str | None]:
        process_name = (window.process_name or "").lower()
        text = " ".join((result.text or result.current_paragraph_text or "").split())
        text_lower = text.lower()
        class_name = (result.class_name or "").lower()
        control_type = (result.control_type or "").lower()
        focused_name = (result.focused_name or "").lower()

        if class_name == "xterm-helper-textarea":
            return "rejected", "vscode_terminal_helper"

        non_text_controls = (
            "button",
            "menu",
            "menuitem",
            "tabitem",
            "toolbar",
            "listitem",
            "treeitem",
            "hyperlink",
        )
        if any(token in control_type for token in non_text_controls):
            return "rejected", "non_text_control"

        if self._is_code_editor_non_editor_focus(process_name, focused_name):
            return "rejected", "code_editor_non_editor_focus"

        # Trusted editors with real structured text are authoritative — decide
        # before the terminal/accessibility heuristic below, which only exists
        # to guess on *unknown* windows and false-positives on ordinary prose
        # (e.g. "경제적 접근성" matching the "접근성" helper-text marker).
        if process_name == "winword.exe" and class_name == "_wwg":
            return "primary", None

        if is_text_extraction_target(window) and self._has_structured_text_source(result):
            # Registered editing apps are authoritative when UIA exposes real
            # text/value patterns, even for short text. OCR should stay a
            # fallback for cases where structured text is unavailable.
            return "primary", None

        terminal_markers = (
            "terminal",
            "터미널",
            "screen reader",
            "화면 읽기",
            "accessibility",
            "접근성",
            "alt+f1",
        )
        if any(marker in text_lower for marker in terminal_markers):
            return "rejected", "accessibility_or_terminal_helper_text"

        if result.current_paragraph_text and len(text) >= 20:
            return "usable", None

        if len(text) < 40:
            return "weak", "too_short_for_document_context"

        sentence_markers = sum(text.count(marker) for marker in (".", "?", "!", "다.", "\n"))
        if sentence_markers >= 2 or len(text) >= 120:
            return "usable", None

        return "weak", "not_enough_document_like_text"

    def _has_structured_text_source(self, result: UiAutomationResult) -> bool:
        if (result.text_source or "").lower() in {"text_pattern", "value_pattern"}:
            return bool((result.text or result.current_paragraph_text or "").strip())
        return False

    def _is_control_name_only_empty_editor(
        self,
        result: UiAutomationResult,
        window: WindowContext,
    ) -> bool:
        if not is_text_extraction_target(window):
            return False
        if (result.text_source or "").lower() != "control_name":
            return False
        if (result.current_paragraph_text or "").strip():
            return False

        focused_name = " ".join((result.focused_name or "").lower().split())
        text = " ".join((result.text or "").lower().split())
        if text != focused_name:
            return False
        if focused_name not in {
            "text editor",
            "editor",
            "document",
            "텍스트 편집기",
            "편집기",
            "문서",
        }:
            return False

        return self._is_editor_control_shape(result)

    def _is_editor_control_shape(self, result: UiAutomationResult) -> bool:
        control_type = (result.control_type or "").lower()
        class_name = (result.class_name or "").lower()
        focused_name = (result.focused_name or "").lower()
        if "edit" in control_type or "document" in control_type:
            return True
        if any(
            token in class_name
            for token in ("richedit", "scintilla", "textview", "editor")
        ):
            return True
        return focused_name in {"text editor", "텍스트 편집기"}

    def _is_code_editor_non_editor_focus(
        self,
        process_name: str,
        focused_name: str,
    ) -> bool:
        if process_name not in {"code.exe", "devenv.exe", "pycharm64.exe"}:
            return False
        return any(
            token in focused_name
            for token in ("terminal", "debug console", "python console")
        )
