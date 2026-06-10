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
    # 사용자가 *지금 쓰고 있는* 텍스트 영역(caret 문단 → 최근 편집영역 → 문서 꼬리).
    # 위치 특정 시나리오(acronym/citation/quote/heading 등)는 전체 문서가 아니라 이
    # 필드를 스캔해, 커서에서 먼 곳의 트리거에 발화하지 않는다. 전체 검토형
    # 시나리오(whole_document_review 등)만 active_editor_text를 그대로 본다.
    cursor_scope_text: str = ""
    # 커서 위치를 *신뢰있게* 잡았는가(UIA caret 문단 또는 캡처 간 diff로 확정).
    # False면 cursor_scope_text는 문서 꼬리 추정치이거나 OCR 잡음이라 "지금 쓰는 곳"이
    # 아니다. 이어쓰기/재작성 같은 커서-로컬 제안은 이 값이 True일 때만 발화한다
    # (커서 모르면 아예 제안하지 않는다 — native editor 방식).
    cursor_located: bool = False
    # 커서가 속한 섹션의 마크다운 헤딩 제목(문서 머리~커서에서 가장 가까운 #헤딩).
    # native editor와 동일하게 이어쓰기를 그 섹션 주제 범위 안에 두기 위해 주입한다.
    section_heading: str = ""
    visible_context: str = ""
    changed_text: str = ""
    confidence: float = 0.0
    noise_removed: list[str] = field(default_factory=list)


@dataclass
class InterventionDecision:
    """Rule-based decision about whether the LLM should consider intervening."""

    should_consider_llm: bool = False
    intervention_type: str = "none"
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
