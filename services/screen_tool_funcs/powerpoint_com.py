from __future__ import annotations

from typing import Any

from .models import AppTextResult, WindowContext


class PowerPointComReader:
    """Read the active PowerPoint slide text through the Office COM object model."""

    def read_active_slide(self, window: WindowContext) -> AppTextResult:
        if (window.process_name or "").lower() != "powerpnt.exe":
            return AppTextResult(error="skipped: foreground app is not PowerPoint.")

        try:
            import win32com.client
        except ImportError:
            return AppTextResult(error="pywin32 is not installed.")

        try:
            app = win32com.client.GetActiveObject("PowerPoint.Application")
            active_window = app.ActiveWindow
            if active_window is None:
                return AppTextResult(error="PowerPoint has no active window.")

            slide = active_window.View.Slide
            if slide is None:
                return AppTextResult(error="PowerPoint has no active slide.")

            parts: list[str] = []
            shape_count = 0
            text_shape_count = 0
            for shape in slide.Shapes:
                shape_count += 1
                text = self._read_shape_text(shape)
                if not text:
                    continue
                text_shape_count += 1
                parts.append(text)

            notes_text = self._read_notes_text(slide)
            if notes_text:
                parts.append(f"[Notes]\n{notes_text}")

            text = "\n\n".join(parts).strip()
            if not text:
                return AppTextResult(
                    text_source="powerpoint_com",
                    source_quality="rejected",
                    metadata=self._metadata(active_window, slide, shape_count, text_shape_count),
                    error="Active slide has no readable text.",
                )

            return AppTextResult(
                text=text,
                text_source="powerpoint_com",
                source_quality="primary",
                metadata=self._metadata(active_window, slide, shape_count, text_shape_count),
            )
        except Exception as exc:
            return AppTextResult(
                text_source="powerpoint_com",
                source_quality="rejected",
                error=str(exc),
            )

    def _read_shape_text(self, shape: Any) -> str:
        try:
            if not bool(shape.HasTextFrame):
                return ""
            text_frame = shape.TextFrame
            if not bool(text_frame.HasText):
                return ""
            return str(text_frame.TextRange.Text or "").strip()
        except Exception:
            return ""

    def _read_notes_text(self, slide: Any) -> str:
        try:
            parts: list[str] = []
            for shape in slide.NotesPage.Shapes:
                text = self._read_shape_text(shape)
                if text:
                    parts.append(text)
            return "\n".join(parts).strip()
        except Exception:
            return ""

    def _metadata(
        self,
        active_window: Any,
        slide: Any,
        shape_count: int,
        text_shape_count: int,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "shape_count": shape_count,
            "text_shape_count": text_shape_count,
        }
        try:
            metadata["slide_index"] = int(slide.SlideIndex)
            metadata["slide_id"] = int(slide.SlideID)
        except Exception:
            pass
        try:
            metadata["presentation_name"] = str(active_window.Presentation.Name)
        except Exception:
            pass
        return metadata
