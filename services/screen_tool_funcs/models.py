from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class BoundingBox:
    """Screen-space rectangle."""

    x: int
    y: int
    width: int
    height: int


@dataclass
class WindowContext:
    """Foreground window metadata."""

    hwnd: int | None = None
    pid: int | None = None
    process_name: str = ""
    process_path: str = ""
    window_title: str = ""
    rect: BoundingBox | None = None
    error: str | None = None


@dataclass
class OcrResult:
    """OCR result over a captured image."""

    text: str = ""
    language: str = "ko-KR"
    lines: list[dict[str, Any]] = field(default_factory=list)
    image_size: list[int] | None = None
    error: str | None = None


@dataclass
class UiAutomationResult:
    """UI Automation text context from the focused control and text ranges."""

    top_window_title: str = ""
    focused_name: str = ""
    control_type: str = ""
    automation_id: str = ""
    class_name: str = ""
    bounding_rect: BoundingBox | None = None
    text: str = ""
    text_source: str = ""
    selection_text: str = ""
    current_paragraph_text: str = ""
    current_paragraph_source: str = ""
    current_paragraph_rect: BoundingBox | None = None
    hover_text: str = ""
    hover_rect: BoundingBox | None = None
    mouse_position: list[int] | None = None
    browser_url: str = ""
    source_quality: str = "rejected"
    reject_reason: str | None = None
    error: str | None = None


@dataclass
class AppTextResult:
    """Text extracted through an application-specific API or COM backend."""

    text: str = ""
    text_source: str = ""
    source_quality: str = "rejected"
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class FilteredScreenContext:
    """Screen context prepared for downstream intervention logic."""

    active_app_type: str = "unknown"
    active_editor_text: str = ""
    current_paragraph_text: str = ""
    current_paragraph_source: str = ""
    current_paragraph_rect: BoundingBox | None = None
    visible_context: str = ""
    changed_text: str = ""
    confidence: float = 0.0
    noise_removed: list[str] = field(default_factory=list)


@dataclass
class InterventionDecision:
    """Rule-based decision about whether the LLM should consider intervening."""

    should_consider_llm: bool = False
    score: float = 0.0
    priority: str = "low"
    reason_codes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScreenContextEvent:
    """One captured polling event."""

    event_id: str
    captured_at: str
    window: WindowContext
    ocr: OcrResult
    app_text: AppTextResult
    ui_automation: UiAutomationResult
    filtered: FilteredScreenContext
    intervention: InterventionDecision
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        *,
        event_id: str,
        window: WindowContext,
        ocr: OcrResult,
        app_text: AppTextResult,
        ui_automation: UiAutomationResult,
        filtered: FilteredScreenContext,
        intervention: InterventionDecision,
        diagnostics: dict[str, Any] | None = None,
    ) -> "ScreenContextEvent":
        return cls(
            event_id=event_id,
            captured_at=datetime.now().isoformat(timespec="seconds"),
            window=window,
            ocr=ocr,
            app_text=app_text,
            ui_automation=ui_automation,
            filtered=filtered,
            intervention=intervention,
            diagnostics=diagnostics or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
