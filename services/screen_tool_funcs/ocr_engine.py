from __future__ import annotations

import asyncio
import io
from typing import Any, cast

from .models import OcrResult


class OcrEngine:
    """OCR engine backed by Windows.Media.Ocr through the `winsdk` bridge.

    WinRT dependencies are imported lazily so the screen context package can be
    loaded on machines where Windows OCR support is not installed.
    """

    def __init__(self, language: str = "ko-KR", scale: float = 2.0) -> None:
        self.language = language
        self.scale = scale

    def recognize(self, image: Any | None) -> OcrResult:
        if image is None:
            return OcrResult(language=self.language, error="No captured image.")

        try:
            return self._run_async(self._recognize_with_winrt(self._prepare_image(image)))
        except ImportError:
            return OcrResult(
                language=self.language,
                error=(
                    "Windows OCR bridge package is not installed. "
                    "Install a Python WinRT bridge such as `winsdk` to use Windows.Media.Ocr."
                ),
            )
        except Exception as exc:
            return OcrResult(language=self.language, error=str(exc))

    def _prepare_image(self, image: Any) -> Any:
        if self.scale == 1.0:
            return image

        try:
            from PIL import Image
        except ImportError:
            return image

        width, height = image.size
        if width <= 0 or height <= 0:
            return image

        resampling = getattr(Image, "Resampling", Image).LANCZOS
        return image.resize(
            (max(int(width * self.scale), 1), max(int(height * self.scale), 1)),
            resampling,
        )

    async def _recognize_with_winrt(self, image: Any) -> OcrResult:
        from winsdk.system import Array
        from winsdk.windows.globalization import Language
        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.media.ocr import OcrEngine as WinRtOcrEngine
        from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        stream = InMemoryRandomAccessStream()
        # DataWriter requires an explicit IOutputStream from the random access stream.
        output_stream = stream.get_output_stream_at(0)
        writer = DataWriter(output_stream)
        # Wrap PNG bytes in winsdk.system.Array to satisfy WinRT's byte-array ABI.
        winrt_bytes = cast(Any, Array)("B", self._image_to_png_bytes(image))
        writer.write_bytes(winrt_bytes)
        await writer.store_async()
        await writer.flush_async()
        writer.detach_stream()
        stream.seek(0)

        decoder = await BitmapDecoder.create_async(cast(Any, stream))
        bitmap = await decoder.get_software_bitmap_async()

        engine = WinRtOcrEngine.try_create_from_language(Language(self.language))
        if engine is None:
            return OcrResult(
                language=self.language,
                error=f"Windows OCR engine is not available for language: {self.language}",
            )

        result = await engine.recognize_async(bitmap)
        text = self._read_attr(result, "text", "Text") or ""

        return OcrResult(
            text=str(text).strip(),
            language=self.language,
            lines=self._extract_lines(result),
            image_size=[int(image.size[0]), int(image.size[1])] if hasattr(image, "size") else None,
        )

    def _image_to_png_bytes(self, image: Any) -> bytes:
        """Convert a PIL image to PNG bytes readable by WinRT BitmapDecoder."""

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def _extract_lines(self, result: Any) -> list[dict[str, Any]]:
        """Normalize WinRT OCR line/word results into JSON-serializable dicts."""

        raw_lines = self._read_attr(result, "lines", "Lines") or []
        lines: list[dict[str, Any]] = []

        for line in raw_lines:
            line_text = self._read_attr(line, "text", "Text") or ""
            words_payload: list[dict[str, Any]] = []

            for word in self._read_attr(line, "words", "Words") or []:
                rect = self._read_attr(word, "bounding_rect", "BoundingRect")
                words_payload.append(
                    {
                        "text": str(self._read_attr(word, "text", "Text") or ""),
                        "bbox": self._rect_to_dict(rect),
                    }
                )

            line_bbox = self._merge_bboxes(
                [word["bbox"] for word in words_payload if word.get("bbox")]
            )
            lines.append(
                {
                    "text": str(line_text),
                    "bbox": line_bbox,
                    "words": words_payload,
                }
            )

        return lines

    def _rect_to_dict(self, rect: Any) -> dict[str, float] | None:
        if rect is None:
            return None

        return {
            "x": float(self._read_attr(rect, "x", "X") or 0),
            "y": float(self._read_attr(rect, "y", "Y") or 0),
            "width": float(self._read_attr(rect, "width", "Width") or 0),
            "height": float(self._read_attr(rect, "height", "Height") or 0),
        }

    def _merge_bboxes(self, boxes: list[dict[str, float]]) -> dict[str, float] | None:
        if not boxes:
            return None

        left = min(box["x"] for box in boxes)
        top = min(box["y"] for box in boxes)
        right = max(box["x"] + box["width"] for box in boxes)
        bottom = max(box["y"] + box["height"] for box in boxes)
        return {
            "x": round(left, 2),
            "y": round(top, 2),
            "width": round(right - left, 2),
            "height": round(bottom - top, 2),
        }

    def _read_attr(self, obj: Any, *names: str) -> Any:
        for name in names:
            if not hasattr(obj, name):
                continue
            value = getattr(obj, name)
            return value() if callable(value) else value
        return None

    def _run_async(self, coroutine):
        """Run the WinRT async API from the synchronous service boundary."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)

        # The service currently runs in a synchronous polling thread. If an event
        # loop is already active, callers should move OCR to an explicit async path.
        raise RuntimeError("OcrEngine.recognize cannot run inside an active asyncio event loop.")
