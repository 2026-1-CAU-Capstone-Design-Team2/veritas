from __future__ import annotations

from typing import Any

from ..core.models import WindowContext


class ScreenCapture:
    """foreground window 영역을 이미지로 캡처합니다."""

    def __init__(
        self,
        *,
        crop_left: int = 0,
        crop_top: int = 0,
        crop_right: int = 0,
        crop_bottom: int = 0,
    ) -> None:
        self.crop_left = max(crop_left, 0)
        self.crop_top = max(crop_top, 0)
        self.crop_right = max(crop_right, 0)
        self.crop_bottom = max(crop_bottom, 0)

    def capture_window(self, window: WindowContext) -> Any | None:
        if window.rect is None:
            return None

        try:
            from PIL import ImageGrab
        except ImportError:
            return None

        rect = window.rect
        bbox = (
            rect.x,
            rect.y,
            rect.x + rect.width,
            rect.y + rect.height,
        )

        try:
            image = ImageGrab.grab(bbox=bbox)
            return self._crop_image(image)
        except Exception:
            return None

    def _crop_image(self, image: Any) -> Any:
        width, height = image.size
        left = min(self.crop_left, width)
        top = min(self.crop_top, height)
        right = max(width - self.crop_right, left)
        bottom = max(height - self.crop_bottom, top)
        if (left, top, right, bottom) == (0, 0, width, height):
            return image
        return image.crop((left, top, right, bottom))
