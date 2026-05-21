from .ocr_engine import OcrEngine
from .powerpoint_com import PowerPointComReader
from .screen_capture import ScreenCapture
from .text_extraction_targets import is_text_extraction_target
from .ui_automation import UiAutomationReader
from .window_context import WindowContextReader

__all__ = [
    "OcrEngine",
    "PowerPointComReader",
    "ScreenCapture",
    "UiAutomationReader",
    "WindowContextReader",
    "is_text_extraction_target",
]
