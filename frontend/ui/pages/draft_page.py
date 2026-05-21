from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
	QApplication,
	QButtonGroup,
	QCheckBox,
	QFileDialog,
	QFrame,
	QGridLayout,
	QHBoxLayout,
	QLabel,
	QLineEdit,
	QPlainTextEdit,
	QPushButton,
	QScrollArea,
	QSizePolicy,
	QStackedWidget,
	QTextEdit,
	QVBoxLayout,
	QWidget,
)

from ...api_common import current_workspace_id
from ...components.buttons import AppButton
from ...controllers import AgentController, JobCategory, get_job_manager
from ..markdown_view import apply_markdown

__all__ = ["DraftPage"]


# ----------------------------------------------------------------- 카테고리 데이터
# 대분류 → 소분류 → 기본 섹션 골격. (프런트엔드 보유 — 백엔드 레지스트리로 이전 예정)
CATEGORIES: list[dict] = [
	{
		"key": "report",
		"label": "보고/분석",
		"subtypes": [
			{"key": "weekly", "label": "주간 보고", "sections": ["요약", "주요 진행 사항", "이슈 / 리스크", "다음 주 계획"]},
			{"key": "result", "label": "결과 보고", "sections": ["개요", "추진 배경", "수행 내용", "결과 및 성과", "결론 및 제언"]},
			{"key": "status", "label": "현황 분석", "sections": ["분석 개요", "현황", "문제점", "원인 분석", "개선 방향"]},
		],
	},
	{
		"key": "proposal",
		"label": "제안/기획",
		"subtypes": [
			{"key": "business", "label": "사업 제안서", "sections": ["제안 배경", "제안 내용", "기대 효과", "추진 일정", "예산"]},
			{"key": "plan", "label": "기획안", "sections": ["기획 의도", "목표", "주요 내용", "실행 계획", "기대 효과"]},
			{"key": "marketing", "label": "마케팅 플랜", "sections": ["시장 분석", "타깃", "전략", "채널 / 실행", "성과 지표"]},
		],
	},
	{
		"key": "record",
		"label": "기록/정리",
		"subtypes": [
			{"key": "minutes", "label": "회의록", "sections": ["회의 개요", "참석자", "안건", "논의 내용", "결정 사항", "후속 조치"]},
			{"key": "memo", "label": "업무 메모", "sections": ["목적", "핵심 내용", "참고 사항", "To-Do"]},
			{"key": "research", "label": "리서치 요약", "sections": ["조사 목적", "조사 방법", "주요 발견", "시사점"]},
		],
	},
	{
		"key": "notice",
		"label": "안내/공지",
		"subtypes": [
			{"key": "internal", "label": "사내 공지", "sections": ["제목", "공지 배경", "주요 내용", "유의 사항", "문의처"]},
			{"key": "customer", "label": "고객 안내", "sections": ["인사말", "안내 내용", "적용 일정", "유의 사항", "문의 안내"]},
			{"key": "event", "label": "이벤트 안내", "sections": ["이벤트 개요", "참여 방법", "혜택", "기간", "유의 사항"]},
		],
	},
	{
		"key": "academic",
		"label": "학술/조사",
		"subtypes": [
			{"key": "paper", "label": "조사 보고서", "sections": ["서론", "연구 방법", "결과", "논의", "결론", "참고문헌"]},
			{"key": "review", "label": "리뷰 / 고찰", "sections": ["개요", "배경", "주요 논점", "비교 분석", "결론"]},
			{"key": "abstract", "label": "초록", "sections": ["연구 목적", "방법", "결과", "결론"]},
		],
	},
]

# 영문 eyebrow — 카드 상단의 보조 라벨. (레퍼런스 mockup 차용)
CATEGORY_EYEBROWS = {
	"report": "Report",
	"proposal": "Proposal",
	"record": "Record",
	"notice": "Notice",
	"academic": "Academic",
}

TONES = ["격식체", "중립", "캐주얼"]
LENGTHS = ["짧게", "보통", "길게"]

_TONE_GUIDE = {"격식체": "격식 있고 공식적인 문체", "중립": "중립적이고 명료한 문체", "캐주얼": "부드럽고 친근한 문체"}
_LENGTH_GUIDE = {"짧게": "핵심 위주로 간결하게", "보통": "보통 수준의 분량으로", "길게": "충분히 상세하게"}

# 양식 파일 경로에도 "작성 옵션" 단계를 추가 → 직접 구성과 동일하게 톤/분량/대상/핵심을 받는다.
FILE_STEPS = ["소스", "양식 분석", "작성 옵션", "목차 확정", "초안"]
CUSTOM_STEPS = ["소스", "대분류", "소분류", "구성", "목차 확정", "초안"]

FILE_FILTER = "문서 (*.docx *.pdf *.md *.txt *.pptx *.ppt *.hwp *.hwpx);;모든 파일 (*.*)"
_TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".log"}


# ----------------------------------------------------------------- 페이지 전용 QSS
# 레퍼런스(aaa.html)의 레이아웃/컴포넌트는 차용하되, 색·폰트는 앱의 블루/슬레이트
# 계열로 통일. guide_page._PAGE_QSS 와 동일하게 페이지 루트에만 적용해 다른 화면에
# 영향이 없도록 격리한다. (전역 PrimaryButton/GhostButton 은 그대로 재사용)
_DRAFT_QSS = """
QWidget#DraftScrollInner, QScrollArea#DraftScroll, QScrollArea#DraftScroll > QWidget > QWidget {
	background: transparent;
}

QFrame#PageHead {
	background-color: #FFFFFF;
	border-bottom: 1px solid #E5E7EB;
}
QFrame#NavBar {
	background-color: #F8FAFC;
	border-top: 1px solid #E5E7EB;
}
QLabel#DraftH1 { color: #0F172A; font-size: 22px; font-weight: 800; }
QLabel#DraftH2 { color: #0F172A; font-size: 18px; font-weight: 800; }
QLabel#DraftSubtitle { color: #6B7280; font-size: 13px; }
QLabel#NavHint { color: #94A3B8; font-size: 12px; }

QLabel#Eyebrow {
	color: #94A3B8; font-size: 11px; font-weight: 800; letter-spacing: 1.4px;
}
QLabel#StepNum {
	color: #4F46E5; font-size: 11px; font-weight: 800; letter-spacing: 0.8px;
}
QLabel#Crumbs { color: #6B7280; font-size: 12px; font-weight: 600; }

QLabel#StatusPill {
	color: #6B7280; background-color: #FFFFFF; border: 1px solid #D1D5DB;
	border-radius: 11px; padding: 3px 10px; font-size: 11px; font-weight: 700;
	letter-spacing: 0.6px;
}
QLabel#WorkspacePill {
	color: #1F2937; background-color: #F8FAFC; border: 1px solid #E5E7EB;
	border-radius: 14px; padding: 4px 12px; font-size: 12px; font-weight: 600;
}

QFrame#DraftCard {
	background-color: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 14px;
}
QFrame#DraftCardFlat { background: transparent; border: none; }

/* 시작 방식 선택 타일 */
QPushButton#ChoiceTile {
	background-color: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 14px;
	text-align: left;
}
QPushButton#ChoiceTile:hover { border-color: #C7D2FE; background-color: #FBFBFE; }
QPushButton#ChoiceTile:checked { border: 2px solid #4F46E5; background-color: #F5F5FF; }
QLabel#IllChip { background-color: #EEF2FF; border-radius: 12px; }
QLabel#ChoiceTitle { color: #0F172A; font-size: 16px; font-weight: 800; }
QLabel#ChoiceDesc { color: #6B7280; font-size: 13px; }
QFrame#ChoiceMeta { border: none; border-top: 1px dashed #E5E7EB; }
QLabel#ChoiceMetaText { color: #94A3B8; font-size: 11px; font-weight: 700; letter-spacing: 0.8px; }
QLabel#ChoiceArrow {
	color: #4F46E5; background-color: #EEF2FF; border-radius: 13px;
	font-size: 14px; font-weight: 800;
}

/* 카테고리 / 소분류 카드 */
QPushButton#CatCard, QPushButton#SubCard {
	background-color: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 12px;
	text-align: left;
}
QPushButton#CatCard:hover, QPushButton#SubCard:hover { border-color: #C7D2FE; background-color: #FBFBFE; }
QPushButton#CatCard:checked, QPushButton#SubCard:checked { border: 2px solid #4F46E5; background-color: #F5F5FF; }
QLabel#CatIcon { background-color: #EEF2FF; border-radius: 10px; }
QLabel#CatEyebrow { color: #94A3B8; font-size: 11px; font-weight: 800; letter-spacing: 1.2px; }
QLabel#CatTitle { color: #0F172A; font-size: 16px; font-weight: 800; }
QLabel#CatListItem { color: #6B7280; font-size: 12px; }
QLabel#SubTitle { color: #0F172A; font-size: 15px; font-weight: 800; }
QLabel#SecPreviewChip { color: #6B7280; font-size: 12px; }

/* 구성 — 섹션 행 */
QFrame#SecRow { background-color: #F8FAFC; border: 1px solid #EEF0F3; border-radius: 8px; }
QLabel#SecNum { color: #94A3B8; font-size: 11px; font-weight: 700; }
QLabel#SecName { color: #0F172A; font-size: 13px; font-weight: 600; }
QPushButton#SecDel {
	color: #94A3B8; background: transparent; border: none; border-radius: 6px;
	font-size: 13px; font-weight: 700;
}
QPushButton#SecDel:hover { color: #DC2626; background-color: #FEF2F2; }

/* 작성 옵션 */
QLabel#DraftFieldLabel { color: #6B7280; font-size: 11px; font-weight: 700; letter-spacing: 0.4px; }
QWidget#Segmented { background-color: #F1F5F9; border: 1px solid #E5E7EB; border-radius: 10px; }
QPushButton#SegItem {
	background: transparent; border: none; border-radius: 7px; padding: 6px 14px;
	color: #6B7280; font-size: 12px; font-weight: 700;
}
QPushButton#SegItem:hover { color: #4F46E5; }
QPushButton#SegItem:checked { background-color: #4F46E5; color: #FFFFFF; }
QLineEdit#DraftInput, QPlainTextEdit#DraftTextarea {
	background-color: #FFFFFF; border: 1px solid #D1D5DB; border-radius: 8px;
	padding: 8px 12px; color: #0F172A; font-size: 13px;
	selection-background-color: #BFDBFE; selection-color: #0F172A;
}
QLineEdit#DraftInput:focus, QPlainTextEdit#DraftTextarea:focus { border-color: #4F46E5; }

/* 목차 */
QLabel#OutlineStat {
	color: #475569; background-color: #F1F5F9; border: 1px solid #E5E7EB;
	border-radius: 8px; padding: 5px 10px; font-size: 12px; font-weight: 600;
}
QFrame#OutlineRow { background-color: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 8px; }
QLabel#OlHandle { color: #CBD5E1; font-size: 14px; font-weight: 800; }
QLabel#OlNum { color: #94A3B8; font-size: 11px; font-weight: 700; }
QLineEdit#OlName {
	background: transparent; border: none; color: #0F172A; font-size: 13px; font-weight: 600;
}
QLineEdit#OlName:focus { background-color: #F8FAFC; border-radius: 6px; }
QPushButton#OlAction {
	color: #64748B; background: transparent; border: 1px solid #E5E7EB; border-radius: 6px;
	font-size: 11px; font-weight: 700;
}
QPushButton#OlAction:hover { color: #4F46E5; border-color: #C7D2FE; background-color: #EEF2FF; }

/* 안내 노트 */
QFrame#HelperNote { background-color: #F8FAFC; border: 1px solid #E5E7EB; border-radius: 10px; }
QLabel#HelperIcon {
	color: #6B7280; background-color: #FFFFFF; border: 1px solid #D1D5DB;
	border-radius: 9px; font-size: 10px; font-weight: 800;
}
QLabel#HelperText { color: #6B7280; font-size: 12px; }

/* 결과 */
QFrame#ResultCard { background-color: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 14px; }
QFrame#ResultToolbar { background-color: #F8FAFC; border: none; border-bottom: 1px solid #E5E7EB; }
QLabel#LiveDot { background-color: #4F46E5; border-radius: 4px; }
QLabel#ResultStatus { color: #475569; font-size: 12px; font-weight: 700; }
QLabel#MetaStrip { color: #94A3B8; font-size: 12px; }
QPushButton#ToolbarBtn {
	color: #1F2937; background-color: #FFFFFF; border: 1px solid #D1D5DB; border-radius: 8px;
	padding: 5px 12px; font-size: 12px; font-weight: 700;
}
QPushButton#ToolbarBtn:hover { border-color: #C7D2FE; color: #4F46E5; }
QPushButton#ToolbarBtn:disabled { color: #CBD5E1; border-color: #EEF0F3; }
QPushButton#ToolbarBtnPrimary {
	color: #FFFFFF; background-color: #4F46E5; border: 1px solid #4F46E5; border-radius: 8px;
	padding: 5px 12px; font-size: 12px; font-weight: 700;
}
QPushButton#ToolbarBtnPrimary:hover { background-color: #4338CA; }
QPushButton#ToolbarBtnPrimary:disabled { background-color: #C7D2FE; border-color: #C7D2FE; }
QTextEdit#DraftOutput {
	background-color: #FFFFFF; border: none; color: #111827; font-size: 14px;
	selection-background-color: #BFDBFE; selection-color: #0F172A;
}
QLabel#SettingsNote { color: #94A3B8; font-size: 12px; }
"""


def _mouse_through(widget: QWidget) -> QWidget:
	"""Let clicks fall through to the parent card button instead of the label."""
	widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
	return widget


def _make_icon(kind: str, size: int = 22, color: str = "#4F46E5") -> QPixmap:
	"""Painter-drawn line icons — no font glyphs or asset files, crisp at any DPI.

	Used for the category/source chips and the small row controls so they never
	fall back to tofu boxes the way Unicode symbols can.
	"""
	scale = 2
	phys = size * scale
	pixmap = QPixmap(phys, phys)
	pixmap.fill(Qt.transparent)
	painter = QPainter(pixmap)
	painter.setRenderHint(QPainter.Antialiasing, True)
	pen = QPen(QColor(color))
	pen.setWidthF(max(1.6, size * 0.085) * scale)
	pen.setCapStyle(Qt.RoundCap)
	pen.setJoinStyle(Qt.RoundJoin)
	painter.setPen(pen)
	painter.setBrush(Qt.NoBrush)

	def pt(fx: float, fy: float) -> QPointF:
		return QPointF(fx * phys, fy * phys)

	def line(x1, y1, x2, y2) -> None:
		painter.drawLine(pt(x1, y1), pt(x2, y2))

	def poly(points) -> None:
		painter.drawPolyline([pt(x, y) for x, y in points])

	if kind == "file":
		path = QPainterPath()
		path.moveTo(pt(0.30, 0.15))
		path.lineTo(pt(0.58, 0.15))
		path.lineTo(pt(0.72, 0.30))
		path.lineTo(pt(0.72, 0.85))
		path.lineTo(pt(0.30, 0.85))
		path.closeSubpath()
		painter.drawPath(path)
		poly([(0.58, 0.15), (0.58, 0.30), (0.72, 0.30)])
		for yy in (0.50, 0.62, 0.74):
			line(0.39, yy, 0.63, yy)
	elif kind == "compose":
		for cx in (0.24, 0.54):
			for cy in (0.24, 0.54):
				painter.drawRoundedRect(pt(cx, cy).x(), pt(cx, cy).y(), 0.22 * phys, 0.22 * phys, 3 * scale, 3 * scale)
	elif kind == "report":
		line(0.20, 0.82, 0.82, 0.82)
		line(0.32, 0.82, 0.32, 0.54)
		line(0.50, 0.82, 0.50, 0.40)
		line(0.68, 0.82, 0.68, 0.62)
	elif kind == "proposal":
		painter.drawEllipse(pt(0.30, 0.20).x(), pt(0.30, 0.20).y(), 0.40 * phys, 0.40 * phys)
		line(0.42, 0.66, 0.58, 0.66)
		line(0.44, 0.76, 0.56, 0.76)
		line(0.46, 0.60, 0.46, 0.66)
		line(0.54, 0.60, 0.54, 0.66)
	elif kind == "record":
		for yy in (0.32, 0.50, 0.68):
			poly([(0.22, yy), (0.27, yy + 0.05), (0.35, yy - 0.05)])
			line(0.44, yy, 0.80, yy)
	elif kind == "notice":
		path = QPainterPath()
		path.moveTo(pt(0.30, 0.66))
		path.cubicTo(pt(0.30, 0.34), pt(0.40, 0.26), pt(0.50, 0.26))
		path.cubicTo(pt(0.60, 0.26), pt(0.70, 0.34), pt(0.70, 0.66))
		painter.drawPath(path)
		line(0.26, 0.66, 0.74, 0.66)
		line(0.50, 0.18, 0.50, 0.26)
		poly([(0.44, 0.72), (0.50, 0.78), (0.56, 0.72)])
	elif kind == "academic":
		poly([(0.50, 0.24), (0.82, 0.40), (0.50, 0.56), (0.18, 0.40), (0.50, 0.24)])
		path = QPainterPath()
		path.moveTo(pt(0.38, 0.48))
		path.lineTo(pt(0.38, 0.64))
		path.cubicTo(pt(0.38, 0.72), pt(0.62, 0.72), pt(0.62, 0.64))
		path.lineTo(pt(0.62, 0.48))
		painter.drawPath(path)
		line(0.82, 0.40, 0.82, 0.60)
	elif kind == "arrow":
		line(0.24, 0.50, 0.74, 0.50)
		poly([(0.60, 0.37), (0.74, 0.50), (0.60, 0.63)])
	elif kind == "grip":
		painter.setBrush(QColor(color))
		for gx in (0.40, 0.60):
			for gy in (0.30, 0.50, 0.70):
				painter.drawEllipse(pt(gx, gy), 0.055 * phys, 0.055 * phys)
	elif kind == "up":
		poly([(0.30, 0.60), (0.50, 0.38), (0.70, 0.60)])
	elif kind == "down":
		poly([(0.30, 0.42), (0.50, 0.64), (0.70, 0.42)])
	elif kind == "close":
		line(0.32, 0.32, 0.68, 0.68)
		line(0.68, 0.32, 0.32, 0.68)

	painter.end()
	pixmap.setDevicePixelRatio(scale)
	return pixmap


class CardButton(QPushButton):
	"""A checkable button that hosts a child layout (icon + labels) as a card.

	QPushButton.sizeHint()/minimumSizeHint() are computed from the button text and
	ignore an attached layout, so a plain QPushButton collapses to a thin strip when
	used as a content card. Delegating to the layout lets the card grow to fit.
	"""

	def __init__(self, object_name: str, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName(object_name)
		self.setCheckable(True)
		self.setCursor(Qt.PointingHandCursor)
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

	def sizeHint(self):  # type: ignore[override]
		layout = self.layout()
		return layout.sizeHint() if layout is not None else super().sizeHint()

	def minimumSizeHint(self):  # type: ignore[override]
		layout = self.layout()
		return layout.minimumSize() if layout is not None else super().minimumSizeHint()


# ------------------------------------------------------------------- 세그먼트 토글
class Segmented(QWidget):
	"""격식체/중립/캐주얼 같은 단일 선택을 알약형 토글로 표현 (콤보박스 대체)."""

	def __init__(self, options: list[str], default: str | None = None, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("Segmented")
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
		layout = QHBoxLayout(self)
		layout.setContentsMargins(3, 3, 3, 3)
		layout.setSpacing(2)
		self._group = QButtonGroup(self)
		self._group.setExclusive(True)
		for opt in options:
			button = QPushButton(opt)
			button.setObjectName("SegItem")
			button.setCheckable(True)
			button.setCursor(Qt.PointingHandCursor)
			if opt == default:
				button.setChecked(True)
			self._group.addButton(button)
			layout.addWidget(button, 1)

	def value(self) -> str:
		button = self._group.checkedButton()
		return button.text() if button else ""

	def set_value(self, value: str) -> None:
		for button in self._group.buttons():
			button.setChecked(button.text() == value)


# ------------------------------------------------------------------- 작성 옵션 블록
class WritingOptions(QWidget):
	"""톤·분량·대상 독자·핵심 내용. 직접 구성과 양식 파일 경로가 각자 인스턴스를 가진다."""

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(14)

		self._tone_seg = Segmented(TONES, "중립")
		layout.addWidget(self._field("톤", self._tone_seg))

		self._length_seg = Segmented(LENGTHS, "보통")
		layout.addWidget(self._field("분량", self._length_seg))

		self._audience = QLineEdit()
		self._audience.setObjectName("DraftInput")
		self._audience.setPlaceholderText("예: 팀 리더 / 고객사 담당자")
		layout.addWidget(self._field("대상 독자", self._audience))

		self._keypoints = QPlainTextEdit()
		self._keypoints.setObjectName("DraftTextarea")
		self._keypoints.setPlaceholderText("초안에 꼭 담겨야 할 핵심 내용을 적어주세요.")
		self._keypoints.setMinimumHeight(96)
		layout.addWidget(self._field("핵심 내용 / 추가 지시", self._keypoints))

	def _field(self, label: str, widget: QWidget) -> QWidget:
		holder = QWidget()
		v = QVBoxLayout(holder)
		v.setContentsMargins(0, 0, 0, 0)
		v.setSpacing(6)
		caption = QLabel(label)
		caption.setObjectName("DraftFieldLabel")
		v.addWidget(caption)
		v.addWidget(widget)
		return holder

	def tone(self) -> str:
		return self._tone_seg.value()

	def length(self) -> str:
		return self._length_seg.value()

	def audience(self) -> str:
		return self._audience.text().strip()

	def keypoints(self) -> str:
		return self._keypoints.toPlainText().strip()

	def reset(self) -> None:
		self._tone_seg.set_value("중립")
		self._length_seg.set_value("보통")
		self._audience.clear()
		self._keypoints.clear()


# --------------------------------------------------------------------- 점 스테퍼
class DotStepper(QWidget):
	"""번호형 점 + 라벨. 메인 창과 공유하는 WorkflowStepper 대신 초안 페이지 전용."""

	def __init__(self, steps: list[str], active: int, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("DotStepper")
		layout = QHBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(6)
		layout.addStretch(1)
		for i, label in enumerate(steps):
			if i < active:
				state = "done"
			elif i == active:
				state = "current"
			else:
				state = "future"
			layout.addWidget(self._step(i + 1, label, state))
			if i < len(steps) - 1:
				connector = QFrame()
				connector.setFixedSize(22, 2)
				color = "#4F46E5" if i < active else "#E5E7EB"
				connector.setStyleSheet(f"background:{color}; border:none; border-radius:1px;")
				layout.addWidget(connector, 0, Qt.AlignVCenter)
		layout.addStretch(1)

	def _step(self, num: int, label: str, state: str) -> QWidget:
		holder = QWidget()
		row = QHBoxLayout(holder)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(8)

		dot = QLabel(str(num))
		dot.setAlignment(Qt.AlignCenter)
		dot.setFixedSize(24, 24)
		if state == "done":
			dot_css = "background:#4F46E5; color:#FFFFFF; border:1px solid #4F46E5;"
			label_css = "color:#4F46E5; font-weight:600;"
		elif state == "current":
			dot_css = "background:#FFFFFF; color:#4F46E5; border:2px solid #4F46E5;"
			label_css = "color:#0F172A; font-weight:700;"
		else:
			dot_css = "background:#F1F5F9; color:#94A3B8; border:1px solid #E5E7EB;"
			label_css = "color:#94A3B8; font-weight:600;"
		dot.setStyleSheet(f"QLabel{{border-radius:12px; font-size:11px; font-weight:800; {dot_css}}}")

		text = QLabel(label)
		text.setStyleSheet(f"QLabel{{font-size:12px; {label_css} background:transparent;}}")

		row.addWidget(dot)
		row.addWidget(text)
		return holder


class DraftPage(QWidget):
	# 결과 화면의 "에디터로 보내기" — frontend 전용 시그널. main_window 가 EditorWindow 로 연결.
	openEditorRequested = Signal(str, str)  # (workspace_id, markdown)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._workspace_id = current_workspace_id()
		self._controller = AgentController()

		# -- 위저드 상태 --------------------------------------------------------
		self._source: str | None = None  # "file" | "custom"
		self._category: dict | None = None
		self._subtype: dict | None = None
		self._uploaded_path: Path | None = None
		self._section_rows: list[dict] = []
		self._outline_items: list[str] = []
		self._outline_edits: list[QLineEdit] = []
		self._current: str = "source"
		self._last_draft_text = ""
		# 구조화 생성 후 결과 화면에서 저장된 설정/재생성을 보여주기 위한 기록.
		self._last_draft_number: int | None = None
		self._last_settings_file = ""

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(0)

		root.addWidget(self._build_header())

		self._stepper_holder = QFrame()
		self._stepper_holder.setObjectName("PageHead")
		self._stepper_holder_layout = QVBoxLayout(self._stepper_holder)
		self._stepper_holder_layout.setContentsMargins(36, 12, 36, 12)
		self._stepper_holder.setVisible(False)
		root.addWidget(self._stepper_holder)

		# 스크롤되는 본문 영역.
		self.stack = QStackedWidget()
		scroll = QScrollArea()
		scroll.setObjectName("DraftScroll")
		scroll.setWidgetResizable(True)
		scroll.setFrameShape(QFrame.NoFrame)
		scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
		scroll.setWidget(self.stack)
		root.addWidget(scroll, 1)

		self._idx: dict[str, int] = {}
		self._add_step("source", self._build_source_page())
		self._add_step("upload", self._build_upload_page())
		self._add_step("file_options", self._build_file_options_page())
		self._add_step("category", self._build_category_page())
		self._add_step("subtype", self._build_subtype_page())
		self._add_step("customize", self._build_customize_page())
		self._add_step("outline", self._build_outline_page())
		self._add_step("result", self._build_result_page())

		root.addWidget(self._build_nav_bar())

		self.setStyleSheet(_DRAFT_QSS)

		get_job_manager().busy_changed.connect(self._sync_busy_state)
		self._show_source()
		self._sync_busy_state()

	# ------------------------------------------------------------------ 헤더
	def _build_header(self) -> QWidget:
		header = QFrame()
		header.setObjectName("PageHead")
		outer = QVBoxLayout(header)
		outer.setContentsMargins(36, 20, 36, 18)
		outer.setSpacing(6)

		top = QHBoxLayout()
		top.setSpacing(10)
		title = QLabel("초안 생성")
		title.setObjectName("DraftH1")
		top.addWidget(title, 0)
		self._mode_pill = QLabel("준비")
		self._mode_pill.setObjectName("StatusPill")
		top.addWidget(self._mode_pill, 0, Qt.AlignVCenter)
		top.addStretch(1)

		self.workspace_label = QLabel(f"워크스페이스 · {self._workspace_id}")
		self.workspace_label.setObjectName("WorkspacePill")
		self.workspace_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
		top.addWidget(self.workspace_label, 0, Qt.AlignVCenter)

		self.restart_button = AppButton("처음부터", variant="ghost")
		self.restart_button.clicked.connect(self._reset)
		top.addWidget(self.restart_button, 0)
		outer.addLayout(top)

		subtitle = QLabel("양식 파일이 있으면 업로드해 그 구조에 맞추고, 없으면 카테고리를 따라 단계별로 구성을 만들어 초안을 생성합니다.")
		subtitle.setObjectName("DraftSubtitle")
		subtitle.setWordWrap(True)
		outer.addWidget(subtitle)
		return header

	# ----------------------------------------------------------------- 하단 내비
	def _build_nav_bar(self) -> QWidget:
		bar = QFrame()
		bar.setObjectName("NavBar")
		bar.setFixedHeight(64)
		layout = QHBoxLayout(bar)
		layout.setContentsMargins(36, 0, 36, 0)
		layout.setSpacing(12)

		self._nav_back = AppButton("이전", variant="ghost")
		self._nav_back.clicked.connect(lambda: self._nav_back_slot() if self._nav_back_slot else None)
		layout.addWidget(self._nav_back, 0)

		layout.addStretch(1)
		self._nav_hint = QLabel("")
		self._nav_hint.setObjectName("NavHint")
		layout.addWidget(self._nav_hint, 0)
		layout.addSpacing(8)

		self._nav_next = AppButton("다음", variant="primary")
		self._nav_next.clicked.connect(lambda: self._nav_next_slot() if self._nav_next_slot else None)
		layout.addWidget(self._nav_next, 0)

		self._nav_back_slot = None
		self._nav_next_slot = None
		return bar

	def _set_nav(self, *, back=None, next=None, hint: str = "") -> None:
		"""back/next: (label, slot, [enabled]) 또는 None(숨김)."""
		if back is None:
			self._nav_back.setVisible(False)
			self._nav_back_slot = None
		else:
			label, slot = back[0], back[1]
			self._nav_back.setText(label)
			self._nav_back.setVisible(True)
			self._nav_back_slot = slot
		if next is None:
			self._nav_next.setVisible(False)
			self._nav_next_slot = None
		else:
			label, slot = next[0], next[1]
			enabled = next[2] if len(next) > 2 else True
			self._nav_next.setText(label)
			self._nav_next.setVisible(True)
			self._nav_next.setEnabled(enabled)
			self._nav_next_slot = slot
		self._nav_hint.setText(hint)

	# --------------------------------------------------------------- 스텝 0: 소스
	def _build_source_page(self) -> QWidget:
		page, col = self._new_step()
		card, body = self._card(
			eyebrow="시작 방식",
			title="어떻게 시작할까요?",
			subtitle="작성할 초안의 시작 방식을 선택하세요. 양식 파일이 있다면 그 구조를 그대로 가져오고, 없다면 카테고리를 따라 단계별로 구성을 만들 수 있어요.",
			flat=True,
		)
		choices = QHBoxLayout()
		choices.setSpacing(14)
		choices.addWidget(self._choice_tile(
			"file",
			"양식 파일 사용",
			"기존 양식(docx · pdf · md 등)을 업로드하면 그 구조를 분석해 같은 형식으로 초안을 생성합니다.",
			"4 단계",
			lambda: self._choose_source("file"),
		))
		choices.addWidget(self._choice_tile(
			"compose",
			"직접 구성",
			"카테고리와 세부 유형을 따라 단계별로 문서 구성을 직접 만듭니다. 자유롭게 짜고 싶을 때 적합해요.",
			"6 단계",
			lambda: self._choose_source("custom"),
		))
		body.addLayout(choices)
		body.addWidget(self._helper_note(
			"어떤 방식으로 시작해도 <b>목차 확정</b> 단계에서 항목을 자유롭게 편집하고 순서를 바꿀 수 있어요. "
			"처음으로 돌아오려면 우측 상단의 <b>처음부터</b> 버튼을 누르세요."
		))
		col.addWidget(card)
		col.addStretch(1)
		return page

	def _choice_tile(self, icon: str, title: str, desc: str, steps_text: str, slot) -> QWidget:
		tile = CardButton("ChoiceTile")
		v = QVBoxLayout(tile)
		v.setContentsMargins(24, 24, 24, 22)
		v.setSpacing(10)

		ill = QLabel()
		ill.setObjectName("IllChip")
		ill.setFixedSize(48, 48)
		ill.setAlignment(Qt.AlignCenter)
		ill.setPixmap(_make_icon(icon, 26, "#4F46E5"))
		v.addWidget(_mouse_through(ill))

		title_l = QLabel(title)
		title_l.setObjectName("ChoiceTitle")
		v.addWidget(_mouse_through(title_l))

		desc_l = QLabel(desc)
		desc_l.setObjectName("ChoiceDesc")
		desc_l.setWordWrap(True)
		v.addWidget(_mouse_through(desc_l))
		v.addStretch(1)

		meta = QFrame()
		meta.setObjectName("ChoiceMeta")
		_mouse_through(meta)
		mrow = QHBoxLayout(meta)
		mrow.setContentsMargins(0, 14, 0, 0)
		steps_l = QLabel(steps_text)
		steps_l.setObjectName("ChoiceMetaText")
		arrow = QLabel()
		arrow.setObjectName("ChoiceArrow")
		arrow.setAlignment(Qt.AlignCenter)
		arrow.setFixedSize(26, 26)
		arrow.setPixmap(_make_icon("arrow", 15, "#4F46E5"))
		mrow.addWidget(steps_l, 0, Qt.AlignVCenter)
		mrow.addStretch(1)
		mrow.addWidget(arrow, 0, Qt.AlignVCenter)
		v.addWidget(meta)

		tile.clicked.connect(slot)
		return tile

	# ----------------------------------------------------- 스텝 A: 양식 파일 업로드
	def _build_upload_page(self) -> QWidget:
		page, col = self._new_step()
		card, body = self._card(
			title="양식 파일",
			step_num="STEP 1 / 2",
			subtitle="초안의 형식을 가져올 양식 파일을 선택하세요. (docx · pdf · md · txt · pptx · hwp)",
		)
		pick_row = QHBoxLayout()
		pick_row.setSpacing(10)
		self.pick_button = AppButton("파일 선택", variant="ghost")
		self.pick_button.clicked.connect(self._pick_file)
		self.file_label = QLabel("선택된 파일이 없습니다.")
		self.file_label.setObjectName("DraftSubtitle")
		self.file_label.setWordWrap(True)
		pick_row.addWidget(self.pick_button, 0)
		pick_row.addWidget(self.file_label, 1)
		body.addLayout(pick_row)

		body.addWidget(self._helper_note(
			"· .md / .txt 양식은 제목 구조를 자동으로 읽어 목차로 채웁니다.<br>"
			"· 그 외 포맷은 기본 골격을 제시하니 <b>목차 확정</b> 단계에서 직접 편집하세요."
		))
		col.addWidget(card)
		col.addStretch(1)
		return page

	# ------------------------------------------------ 스텝 A②: 작성 옵션 (양식 경로)
	def _build_file_options_page(self) -> QWidget:
		page, col = self._new_step()
		card, body = self._card(
			title="작성 옵션",
			step_num="STEP 2 / 2",
			subtitle="초안의 어조와 분량, 대상 독자 정보를 알려주세요. 업로드한 양식 구조에 이 설정을 적용합니다.",
		)
		self.file_options = WritingOptions()
		body.addWidget(self.file_options)
		col.addWidget(card)
		col.addStretch(1)
		return page

	# ------------------------------------------------------ 스텝 B①: 대분류
	def _build_category_page(self) -> QWidget:
		page, col = self._new_step()
		card, body = self._card(
			title="대분류 선택",
			step_num="STEP 1 / 3",
			subtitle="작성할 문서의 큰 갈래를 고르세요. 어떤 종류의 문서를 쓰는지에 따라 추천 구성이 달라집니다.",
		)
		grid = QGridLayout()
		grid.setSpacing(12)
		self._category_group = QButtonGroup(self)
		self._category_group.setExclusive(True)
		for i, cat in enumerate(CATEGORIES):
			grid.addWidget(self._cat_card(cat), i // 3, i % 3)
		self._category_group.buttonClicked.connect(self._on_category_clicked)
		body.addLayout(grid)
		col.addWidget(card)
		col.addStretch(1)
		return page

	def _cat_card(self, cat: dict) -> QWidget:
		btn = CardButton("CatCard")
		btn.setProperty("optionKey", cat["key"])
		v = QVBoxLayout(btn)
		v.setContentsMargins(18, 18, 18, 18)
		v.setSpacing(6)

		icon = QLabel()
		icon.setObjectName("CatIcon")
		icon.setFixedSize(40, 40)
		icon.setAlignment(Qt.AlignCenter)
		icon.setPixmap(_make_icon(cat["key"], 22, "#4F46E5"))
		v.addWidget(_mouse_through(icon))

		eyebrow = QLabel(CATEGORY_EYEBROWS.get(cat["key"], ""))
		eyebrow.setObjectName("CatEyebrow")
		v.addWidget(_mouse_through(eyebrow))

		title = QLabel(cat["label"])
		title.setObjectName("CatTitle")
		v.addWidget(_mouse_through(title))

		for sub in cat["subtypes"][:3]:
			item = QLabel(f"· {sub['label']}")
			item.setObjectName("CatListItem")
			v.addWidget(_mouse_through(item))

		self._category_group.addButton(btn)
		return btn

	# ------------------------------------------------------ 스텝 B②: 소분류
	def _build_subtype_page(self) -> QWidget:
		page, col = self._new_step()
		self._subtype_card, body = self._card(
			title="세부 유형 선택",
			step_num="STEP 2 / 3",
			crumbs="대분류 › 세부 유형",
			subtitle="선택한 대분류 안에서 구체적인 문서 유형을 고르세요. 미리 정의된 기본 섹션 골격을 제시합니다.",
		)
		self._subtype_crumbs = self._subtype_card.findChild(QLabel, "Crumbs")
		self._subtype_grid = QGridLayout()
		self._subtype_grid.setSpacing(12)
		self._subtype_group = QButtonGroup(self)
		self._subtype_group.setExclusive(True)
		body.addLayout(self._subtype_grid)
		col.addWidget(self._subtype_card)
		col.addStretch(1)
		return page

	def _sub_card(self, sub: dict) -> QWidget:
		btn = CardButton("SubCard")
		btn.setProperty("optionKey", sub["key"])
		v = QVBoxLayout(btn)
		v.setContentsMargins(18, 16, 18, 16)
		v.setSpacing(8)

		title = QLabel(sub["label"])
		title.setObjectName("SubTitle")
		v.addWidget(_mouse_through(title))

		for sec in sub.get("sections", []):
			chip = QLabel(f"· {sec}")
			chip.setObjectName("SecPreviewChip")
			v.addWidget(_mouse_through(chip))

		self._subtype_group.addButton(btn)
		return btn

	# ------------------------------------------------------ 스텝 B③: 구성 커스텀
	def _build_customize_page(self) -> QWidget:
		page, col = self._new_step(max_w=1080)
		two = QHBoxLayout()
		two.setSpacing(16)

		sec_card, sec_body = self._card(
			title="포함할 섹션",
			step_num="STEP 3 / 3",
			subtitle="기본 섹션에서 포함할 항목을 선택하고, 필요하면 직접 추가하세요.",
		)
		self._sections_box = QVBoxLayout()
		self._sections_box.setSpacing(6)
		sec_body.addLayout(self._sections_box)

		add_row = QHBoxLayout()
		self.section_input = QLineEdit()
		self.section_input.setObjectName("DraftInput")
		self.section_input.setPlaceholderText("추가할 섹션 이름")
		self.section_input.returnPressed.connect(self._add_custom_section)
		add_section_btn = AppButton("＋ 섹션 추가", variant="ghost")
		add_section_btn.clicked.connect(self._add_custom_section)
		add_row.addWidget(self.section_input, 1)
		add_row.addWidget(add_section_btn, 0)
		sec_body.addLayout(add_row)

		opt_card, opt_body = self._card(
			title="작성 옵션",
			subtitle="초안의 어조와 분량, 대상 독자 정보를 알려주세요.",
		)
		self.custom_options = WritingOptions()
		opt_body.addWidget(self.custom_options)
		opt_body.addStretch(1)

		two.addWidget(sec_card, 1)
		two.addWidget(opt_card, 1)
		col.addLayout(two)
		col.addStretch(1)
		return page

	def _add_section_row(self, name: str, checked: bool) -> None:
		row = QFrame()
		row.setObjectName("SecRow")
		h = QHBoxLayout(row)
		h.setContentsMargins(12, 8, 10, 8)
		h.setSpacing(12)

		checkbox = QCheckBox()
		checkbox.setChecked(checked)
		num = QLabel("")
		num.setObjectName("SecNum")
		name_label = QLabel(name)
		name_label.setObjectName("SecName")
		delete = QPushButton()
		delete.setObjectName("SecDel")
		delete.setCursor(Qt.PointingHandCursor)
		delete.setFixedSize(22, 22)
		delete.setIcon(QIcon(_make_icon("close", 12, "#94A3B8")))
		delete.setIconSize(QSize(12, 12))

		h.addWidget(checkbox, 0)
		h.addWidget(num, 0)
		h.addWidget(name_label, 1)
		h.addWidget(delete, 0)

		entry = {"row": row, "cb": checkbox, "name": name, "num": num}
		self._section_rows.append(entry)
		delete.clicked.connect(lambda: self._remove_section_row(entry))
		self._sections_box.addWidget(row)
		self._renumber_sections()

	def _remove_section_row(self, entry: dict) -> None:
		if entry not in self._section_rows:
			return
		self._section_rows.remove(entry)
		entry["row"].setParent(None)
		entry["row"].deleteLater()
		self._renumber_sections()

	def _renumber_sections(self) -> None:
		for i, entry in enumerate(self._section_rows, start=1):
			entry["num"].setText(f"{i:02d}")

	# ------------------------------------------------------ 공통 ④: 목차 확정
	def _build_outline_page(self) -> QWidget:
		page, col = self._new_step()
		card, body = self._card(
			title="목차 확정",
			step_num="STEP 4",
			subtitle="생성될 초안의 구성입니다. 항목을 클릭해 이름을 수정하거나 화살표로 순서를 바꾸고 삭제할 수 있어요.",
		)
		self._outline_stats_row = QHBoxLayout()
		self._outline_stats_row.setSpacing(8)
		self._outline_stats_row.addStretch(1)
		body.addLayout(self._outline_stats_row)

		self._outline_box = QVBoxLayout()
		self._outline_box.setSpacing(6)
		body.addLayout(self._outline_box)

		add_row = QHBoxLayout()
		self.outline_input = QLineEdit()
		self.outline_input.setObjectName("DraftInput")
		self.outline_input.setPlaceholderText("추가할 목차 항목")
		self.outline_input.returnPressed.connect(self._outline_add)
		add_btn = AppButton("＋ 항목 추가", variant="ghost")
		add_btn.clicked.connect(self._outline_add)
		add_row.addWidget(self.outline_input, 1)
		add_row.addWidget(add_btn, 0)
		body.addLayout(add_row)

		col.addWidget(card)
		col.addStretch(1)
		return page

	def _rebuild_outline(self) -> None:
		while self._outline_box.count():
			item = self._outline_box.takeAt(0)
			widget = item.widget()
			if widget is not None:
				widget.setParent(None)
				widget.deleteLater()
		self._outline_edits = []
		for i, name in enumerate(self._outline_items):
			self._outline_box.addWidget(self._outline_row(i, name))

	def _outline_row(self, index: int, name: str) -> QWidget:
		row = QFrame()
		row.setObjectName("OutlineRow")
		h = QHBoxLayout(row)
		h.setContentsMargins(12, 6, 10, 6)
		h.setSpacing(10)

		handle = QLabel()
		handle.setObjectName("OlHandle")
		handle.setFixedWidth(16)
		handle.setAlignment(Qt.AlignCenter)
		handle.setPixmap(_make_icon("grip", 16, "#CBD5E1"))
		num = QLabel(f"{index + 1:02d}")
		num.setObjectName("OlNum")
		edit = QLineEdit(name)
		edit.setObjectName("OlName")
		self._outline_edits.append(edit)

		up = self._row_action("up", lambda: self._outline_move(index, -1))
		down = self._row_action("down", lambda: self._outline_move(index, 1))
		delete = self._row_action("close", lambda: self._outline_remove(index))

		h.addWidget(handle, 0)
		h.addWidget(num, 0)
		h.addWidget(edit, 1)
		h.addWidget(up, 0)
		h.addWidget(down, 0)
		h.addWidget(delete, 0)
		return row

	def _row_action(self, icon: str, slot) -> QPushButton:
		button = QPushButton()
		button.setObjectName("OlAction")
		button.setFixedSize(26, 24)
		button.setCursor(Qt.PointingHandCursor)
		button.setIcon(QIcon(_make_icon(icon, 13, "#64748B")))
		button.setIconSize(QSize(13, 13))
		button.clicked.connect(slot)
		return button

	def _refresh_outline_stats(self) -> None:
		while self._outline_stats_row.count():
			item = self._outline_stats_row.takeAt(0)
			widget = item.widget()
			if widget is not None:
				widget.setParent(None)
				widget.deleteLater()
		chips: list[str] = []
		if self._source == "file":
			chips.append(self._uploaded_path.name if self._uploaded_path else "양식 파일")
			options = getattr(self, "file_options", None)
		else:
			if self._category and self._subtype:
				chips.append(f"{self._category['label']} · {self._subtype['label']}")
			options = getattr(self, "custom_options", None)
		if options is not None:
			chips.append(f"{options.tone()} · {options.length()}")
		for text in chips:
			label = QLabel(text)
			label.setObjectName("OutlineStat")
			self._outline_stats_row.addWidget(label, 0)
		self._outline_stats_row.addStretch(1)

	# ----------------------------------------------------------- 공통: 결과
	def _build_result_page(self) -> QWidget:
		page, col = self._new_step()
		card = QFrame()
		card.setObjectName("ResultCard")
		card_layout = QVBoxLayout(card)
		card_layout.setContentsMargins(0, 0, 0, 0)
		card_layout.setSpacing(0)

		toolbar = QFrame()
		toolbar.setObjectName("ResultToolbar")
		toolbar.setFixedHeight(48)
		trow = QHBoxLayout(toolbar)
		trow.setContentsMargins(18, 0, 14, 0)
		trow.setSpacing(8)
		live = QLabel()
		live.setObjectName("LiveDot")
		live.setFixedSize(8, 8)
		status = QLabel("초안 미리보기")
		status.setObjectName("ResultStatus")
		trow.addWidget(live, 0, Qt.AlignVCenter)
		trow.addWidget(status, 0, Qt.AlignVCenter)
		trow.addStretch(1)

		self.copy_button = QPushButton("초안 복사")
		self.copy_button.setObjectName("ToolbarBtn")
		self.copy_button.setCursor(Qt.PointingHandCursor)
		self.copy_button.clicked.connect(self._copy_output)
		self.regenerate_button = QPushButton("이 설정으로 재생성")
		self.regenerate_button.setObjectName("ToolbarBtn")
		self.regenerate_button.setCursor(Qt.PointingHandCursor)
		self.regenerate_button.setEnabled(False)
		self.regenerate_button.setToolTip("저장된 설정(draft_<번호>_settings.json)으로 초안을 다시 생성합니다.")
		self.regenerate_button.clicked.connect(self._regenerate_draft)
		self.editor_button = QPushButton("에디터로 보내기")
		self.editor_button.setObjectName("ToolbarBtnPrimary")
		self.editor_button.setCursor(Qt.PointingHandCursor)
		self.editor_button.setEnabled(False)
		self.editor_button.setToolTip("생성된 초안을 문서 작성 에디터로 보냅니다.")
		self.editor_button.clicked.connect(self._send_to_editor)
		trow.addWidget(self.copy_button, 0)
		trow.addWidget(self.regenerate_button, 0)
		trow.addWidget(self.editor_button, 0)
		card_layout.addWidget(toolbar)

		body = QWidget()
		body_layout = QVBoxLayout(body)
		body_layout.setContentsMargins(28, 22, 28, 24)
		body_layout.setSpacing(12)
		self.meta_strip = QLabel("")
		self.meta_strip.setObjectName("MetaStrip")
		self.meta_strip.setWordWrap(True)
		body_layout.addWidget(self.meta_strip)

		# QTextEdit(마크다운 렌더) — 요약/채팅과 같은 markdown_view 렌더러 사용.
		self.output = QTextEdit()
		self.output.setReadOnly(True)
		self.output.setObjectName("DraftOutput")
		self.output.setMinimumHeight(360)
		body_layout.addWidget(self.output, 1)

		self.settings_note = QLabel("")
		self.settings_note.setObjectName("SettingsNote")
		self.settings_note.setWordWrap(True)
		self.settings_note.setTextInteractionFlags(Qt.TextSelectableByMouse)
		body_layout.addWidget(self.settings_note)

		card_layout.addWidget(body, 1)
		col.addWidget(card, 1)
		return page

	# ------------------------------------------------------------------ 헬퍼
	def _new_step(self, max_w: int = 980) -> tuple[QWidget, QVBoxLayout]:
		page = QWidget()
		h = QHBoxLayout(page)
		h.setContentsMargins(36, 28, 36, 28)
		h.setSpacing(0)
		col_host = QWidget()
		col_host.setObjectName("DraftScrollInner")
		col_host.setMaximumWidth(max_w)
		col_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		col = QVBoxLayout(col_host)
		col.setContentsMargins(0, 0, 0, 0)
		col.setSpacing(16)
		h.addStretch(1)
		h.addWidget(col_host, 20)
		h.addStretch(1)
		return page, col

	def _card(
		self,
		*,
		eyebrow: str | None = None,
		title: str | None = None,
		step_num: str | None = None,
		crumbs: str | None = None,
		subtitle: str | None = None,
		flat: bool = False,
	) -> tuple[QFrame, QVBoxLayout]:
		card = QFrame()
		card.setObjectName("DraftCardFlat" if flat else "DraftCard")
		v = QVBoxLayout(card)
		if flat:
			v.setContentsMargins(0, 0, 0, 0)
		else:
			v.setContentsMargins(22, 22, 22, 22)
		v.setSpacing(14)

		head = QVBoxLayout()
		head.setSpacing(6)
		if eyebrow:
			eb = QLabel(eyebrow)
			eb.setObjectName("Eyebrow")
			head.addWidget(eb)
		if crumbs:
			cb = QLabel(crumbs)
			cb.setObjectName("Crumbs")
			head.addWidget(cb)
		if title or step_num:
			trow = QHBoxLayout()
			trow.setSpacing(10)
			if title:
				t = QLabel(title)
				t.setObjectName("DraftH2")
				trow.addWidget(t, 0)
			trow.addStretch(1)
			if step_num:
				sn = QLabel(step_num)
				sn.setObjectName("StepNum")
				trow.addWidget(sn, 0, Qt.AlignVCenter)
			head.addLayout(trow)
		if subtitle:
			s = QLabel(subtitle)
			s.setObjectName("DraftSubtitle")
			s.setWordWrap(True)
			head.addWidget(s)
		v.addLayout(head)
		return card, v

	def _helper_note(self, rich_text: str) -> QWidget:
		note = QFrame()
		note.setObjectName("HelperNote")
		h = QHBoxLayout(note)
		h.setContentsMargins(14, 12, 14, 12)
		h.setSpacing(10)
		icon = QLabel("i")
		icon.setObjectName("HelperIcon")
		icon.setAlignment(Qt.AlignCenter)
		icon.setFixedSize(18, 18)
		text = QLabel(rich_text)
		text.setObjectName("HelperText")
		text.setWordWrap(True)
		text.setTextFormat(Qt.RichText)
		h.addWidget(icon, 0, Qt.AlignTop)
		h.addWidget(text, 1)
		return note

	def _add_step(self, key: str, widget: QWidget) -> None:
		self._idx[key] = self.stack.addWidget(widget)

	def _set_steps(self, steps: list[str], active: int) -> None:
		while self._stepper_holder_layout.count():
			item = self._stepper_holder_layout.takeAt(0)
			widget = item.widget()
			if widget is not None:
				widget.setParent(None)
				widget.deleteLater()
		self._stepper_holder_layout.addWidget(DotStepper(steps, active))
		self._stepper_holder.setVisible(True)

	# --------------------------------------------------------------- 네비게이션
	def _show_source(self) -> None:
		self._current = "source"
		self._stepper_holder.setVisible(False)
		self.stack.setCurrentIndex(self._idx["source"])
		self._set_nav(hint="옵션을 선택하면 다음 단계로 진행됩니다.")

	def _show_upload(self) -> None:
		self._current = "upload"
		self._set_steps(FILE_STEPS, 1)
		self.stack.setCurrentIndex(self._idx["upload"])
		self._set_nav(
			back=("이전", self._show_source),
			next=("양식 분석", self._analyze_format, self._uploaded_path is not None),
		)

	def _show_file_options(self) -> None:
		self._current = "file_options"
		self._set_steps(FILE_STEPS, 2)
		self.stack.setCurrentIndex(self._idx["file_options"])
		self._set_nav(
			back=("이전", self._show_upload),
			next=("목차 만들기", self._show_outline),
		)

	def _show_category(self) -> None:
		self._current = "category"
		self._set_steps(CUSTOM_STEPS, 1)
		self.stack.setCurrentIndex(self._idx["category"])
		self._set_nav(
			back=("이전", self._show_source),
			next=("다음", self._show_subtype, self._category is not None),
		)

	def _show_subtype(self) -> None:
		if self._category is None:
			return
		self._current = "subtype"
		self._rebuild_subtypes()
		if self._subtype_crumbs is not None:
			self._subtype_crumbs.setText(f"{self._category['label']} › 세부 유형")
		self._set_steps(CUSTOM_STEPS, 2)
		self.stack.setCurrentIndex(self._idx["subtype"])
		self._set_nav(
			back=("이전", self._show_category),
			next=("다음", self._enter_customize, self._subtype is not None),
		)

	def _show_customize(self) -> None:
		self._current = "customize"
		self._set_steps(CUSTOM_STEPS, 3)
		self.stack.setCurrentIndex(self._idx["customize"])
		self._set_nav(
			back=("이전", self._show_subtype),
			next=("목차 만들기", self._build_outline_from_customize),
		)

	def _show_outline(self) -> None:
		self._current = "outline"
		self._refresh_outline_stats()
		if self._source == "file":
			self._set_steps(FILE_STEPS, 3)
		else:
			self._set_steps(CUSTOM_STEPS, 4)
		self.stack.setCurrentIndex(self._idx["outline"])
		self._set_nav(
			back=("이전", self._outline_back),
			next=("이 구성으로 초안 생성", self._generate_draft, not get_job_manager().is_blocked(JobCategory.DRAFT)),
		)

	def _show_result(self) -> None:
		self._current = "result"
		if self._source == "file":
			self._set_steps(FILE_STEPS, 4)
		else:
			self._set_steps(CUSTOM_STEPS, 5)
		self.stack.setCurrentIndex(self._idx["result"])
		self._set_nav(back=("목차로 돌아가기", self._show_outline))

	def _outline_back(self) -> None:
		if self._source == "file":
			self._show_file_options()
		else:
			self._show_customize()

	# ------------------------------------------------------------------ 액션
	def _choose_source(self, source: str) -> None:
		self._source = source
		if source == "file":
			self._mode_pill.setText("양식 파일 모드")
			self._show_upload()
		else:
			self._mode_pill.setText("직접 구성 모드")
			self._show_category()

	def _pick_file(self) -> None:
		path_str, _ = QFileDialog.getOpenFileName(self, "양식 파일 선택", "", FILE_FILTER)
		if not path_str:
			return
		self._uploaded_path = Path(path_str)
		self.file_label.setText(self._uploaded_path.name)
		# 파일이 선택되면 '양식 분석'을 진행할 수 있다.
		if self._current == "upload":
			self._nav_next.setEnabled(True)

	def _analyze_format(self) -> None:
		"""업로드한 양식 파일을 백엔드에서 분석해 구조(제목·목록·표)를 추출한다.

		.docx/.doc/.hwp/.hwpx/.pdf는 백엔드가 본문을 제거하고 구조만 md 템플릿으로
		변환한다. 네트워크 호출이므로 워커 스레드에서 수행하고, 실패 시 로컬 휴리스틱
		(헤딩 파싱)으로 폴백한다.
		"""
		path = self._uploaded_path
		if path is None:
			return
		self.analyze_button.setEnabled(False)
		self.file_label.setText(f"{path.name} — 양식 분석 중...")
		controller = self._controller

		def _work():
			return controller.import_draft_form(path)

		get_job_manager().run_detached(
			_work,
			on_success=self._on_form_analyzed,
			on_error=self._on_form_analyze_failed,
		)

	def _on_form_analyzed(self, response) -> None:
		outline: list[str] = []
		markdown = ""
		note = ""
		if isinstance(response, dict):
			outline = [str(s).strip() for s in (response.get("outline") or []) if str(s).strip()]
			markdown = str(response.get("markdown") or "")
			note = str(response.get("note") or "")
		self._form_markdown = markdown
		self.analyze_button.setEnabled(True)
		if not outline:
			outline = ["제목", "개요", "본문", "결론"]
		self._set_outline(outline)
		if self._uploaded_path is not None:
			label = self._uploaded_path.name
			if note:
				label += f"  ({note})"
			self.file_label.setText(label)
		self._show_outline()

	def _on_form_analyze_failed(self, message: str) -> None:
		# 백엔드 분석 실패 → 로컬 헤딩 파싱(텍스트 계열) 또는 기본 골격으로 폴백.
		self.analyze_button.setEnabled(True)
		self._form_markdown = ""
		sections: list[str] = []
		path = self._uploaded_path
		if path is not None and path.suffix.lower() in _TEXT_SUFFIXES:
			try:
				sections = self._parse_headings(path.read_text(encoding="utf-8", errors="ignore"))
			except Exception:
				sections = []
		if not sections:
			sections = ["제목", "개요", "본문", "결론"]
		self._set_outline(sections)
		self._show_file_options()

	@staticmethod
	def _parse_headings(text: str) -> list[str]:
		sections: list[str] = []
		for line in text.splitlines():
			stripped = line.strip()
			heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
			if heading:
				title = heading.group(1).strip()
			else:
				numbered = re.match(r"^(\d+(?:\.\d+)*)[.)]\s+(.+)$", stripped)
				title = numbered.group(2).strip() if numbered else ""
			if title:
				sections.append(title)
		# 중복 제거(순서 유지) 후 상한.
		seen: set[str] = set()
		unique = [s for s in sections if not (s in seen or seen.add(s))]
		return unique[:30]

	def _on_category_clicked(self, button) -> None:
		key = button.property("optionKey")
		self._category = next((c for c in CATEGORIES if c["key"] == key), None)
		self._subtype = None
		if self._current == "category":
			self._nav_next.setEnabled(self._category is not None)

	def _rebuild_subtypes(self) -> None:
		while self._subtype_grid.count():
			item = self._subtype_grid.takeAt(0)
			widget = item.widget()
			if widget is not None:
				widget.setParent(None)
				widget.deleteLater()
		self._subtype_group = QButtonGroup(self)
		self._subtype_group.setExclusive(True)
		subtypes = self._category["subtypes"] if self._category else []
		for i, sub in enumerate(subtypes):
			self._subtype_grid.addWidget(self._sub_card(sub), i // 3, i % 3)
		self._subtype_group.buttonClicked.connect(self._on_subtype_clicked)

	def _on_subtype_clicked(self, button) -> None:
		key = button.property("optionKey")
		subtypes = self._category["subtypes"] if self._category else []
		self._subtype = next((s for s in subtypes if s["key"] == key), None)
		if self._current == "subtype":
			self._nav_next.setEnabled(self._subtype is not None)

	def _enter_customize(self) -> None:
		if self._subtype is None:
			return
		for entry in list(self._section_rows):
			entry["row"].setParent(None)
			entry["row"].deleteLater()
		self._section_rows = []
		for name in self._subtype.get("sections", []):
			self._add_section_row(name, checked=True)
		self._show_customize()

	def _add_custom_section(self) -> None:
		name = self.section_input.text().strip()
		if not name:
			return
		self._add_section_row(name, checked=True)
		self.section_input.clear()

	def _build_outline_from_customize(self) -> None:
		sections = [entry["name"] for entry in self._section_rows if entry["cb"].isChecked()]
		if not sections:
			sections = ["서론", "본문", "결론"]
		self._set_outline(sections)
		self._show_outline()

	# -- 목차 리스트 조작 -----------------------------------------------------
	def _set_outline(self, sections: list[str]) -> None:
		self._outline_items = list(sections)
		self._rebuild_outline()

	def _capture_outline(self) -> None:
		if self._outline_edits:
			self._outline_items = [edit.text().strip() for edit in self._outline_edits]

	def _outline_add(self) -> None:
		name = self.outline_input.text().strip()
		if not name:
			return
		self._capture_outline()
		self._outline_items.append(name)
		self.outline_input.clear()
		self._rebuild_outline()

	def _outline_remove(self, index: int) -> None:
		self._capture_outline()
		if 0 <= index < len(self._outline_items):
			del self._outline_items[index]
			self._rebuild_outline()

	def _outline_move(self, index: int, delta: int) -> None:
		self._capture_outline()
		target = index + delta
		if index < 0 or target < 0 or target >= len(self._outline_items):
			return
		self._outline_items[index], self._outline_items[target] = (
			self._outline_items[target],
			self._outline_items[index],
		)
		self._rebuild_outline()

	def _outline_sections(self) -> list[str]:
		self._capture_outline()
		return [name for name in self._outline_items if name]

	# -- 생성 -----------------------------------------------------------------
	def _compose_prompt(self) -> str:
		sections = self._outline_sections()
		doc_type = f"업로드 양식 기반 ({self._uploaded_path.name})" if self._uploaded_path else "업로드 양식 기반"
		options = getattr(self, "file_options", None)
		tone = options.tone() if options else "중립"
		length = options.length() if options else "보통"
		audience = options.audience() if options else ""
		key_points = options.keypoints() if options else ""

		outline_text = "\n".join(f"{i}. {name}" for i, name in enumerate(sections, start=1))
		lines = [
			"다음 구성을 따라 바로 사용할 수 있는 한국어 초안을 작성해 주세요.",
			"",
			f"[문서 유형] {doc_type}",
			f"[톤] {_TONE_GUIDE.get(tone, tone)}",
			f"[분량] {_LENGTH_GUIDE.get(length, length)}",
		]
		if audience:
			lines.append(f"[대상 독자] {audience}")
		if key_points:
			lines.append("[핵심 내용]")
			lines.append(key_points)
		lines += [
			"",
			"[목차]",
			outline_text,
			"",
			"각 목차 항목을 마크다운 제목(##)으로 두고 그 아래에 내용을 채워 주세요.",
		]
		return "\n".join(lines)

	def _compose_settings(self) -> dict:
		"""직접 구성 초안의 구조화 설정. 백엔드가 톤을 샘플링 전략으로 매핑하고
		drafts/draft_<n>_settings.json 으로 저장한다."""
		options = self.custom_options
		return {
			"source": "custom",
			"category": {"key": self._category["key"], "label": self._category["label"]} if self._category else None,
			"subtype": {"key": self._subtype["key"], "label": self._subtype["label"]} if self._subtype else None,
			"outline": self._outline_sections(),
			"tone": options.tone(),
			"length": options.length(),
			"audience": options.audience(),
			"keyPoints": options.keypoints(),
		}

	def _generate_draft(self) -> None:
		if not self._outline_sections():
			self.output.setPlainText("목차에 항목을 1개 이상 추가하세요.")
			self._show_result()
			return
		self._workspace_id = current_workspace_id()
		# 직접 구성 → 구조화된 설정으로 톤별 샘플링·지식베이스 종합·설정 저장까지 수행.
		# 업로드 양식(file)은 평문 프롬프트 경로(작성 옵션을 프롬프트에 반영).
		if self._source == "custom":
			started = get_job_manager().submit(
				JobCategory.DRAFT,
				self._controller.generate_builtin_draft,
				self._workspace_id,
				self._compose_settings(),
				on_success=self._on_draft_generated,
				on_error=self._on_draft_failed,
			)
		else:
			started = get_job_manager().submit(
				JobCategory.DRAFT,
				self._controller.generate_draft,
				self._workspace_id,
				self._compose_prompt(),
				on_success=self._on_draft_generated,
				on_error=self._on_draft_failed,
			)
		if not started:
			return
		self.settings_note.setText("")
		self.meta_strip.setText(self._result_meta())
		self.output.setPlainText("agent가 초안을 생성하는 중입니다...")
		self._show_result()

	def _result_meta(self) -> str:
		if self._source == "file":
			doc_type = self._uploaded_path.name if self._uploaded_path else "업로드 양식"
			options = getattr(self, "file_options", None)
		else:
			doc_type = f"{self._category['label']} · {self._subtype['label']}" if (self._category and self._subtype) else "직접 구성"
			options = getattr(self, "custom_options", None)
		parts = [doc_type]
		if options is not None:
			parts.append(f"{options.tone()} · {options.length()}")
		parts.append(f"{len(self._outline_sections())}개 섹션")
		return "  ·  ".join(parts)

	def _regenerate_draft(self) -> None:
		if self._last_draft_number is None:
			return
		started = get_job_manager().submit(
			JobCategory.DRAFT,
			self._controller.regenerate_builtin_draft,
			self._workspace_id,
			self._last_draft_number,
			on_success=self._on_draft_generated,
			on_error=self._on_draft_failed,
		)
		if not started:
			return
		self.output.setPlainText(f"동일 설정(초안 #{self._last_draft_number})으로 다시 생성하는 중입니다...")
		self._show_result()

	def _on_draft_generated(self, response) -> None:
		number = None
		settings_file = ""
		has_kb = None
		if isinstance(response, dict):
			text = str(response.get("content") or "")
			number = response.get("draftNumber")
			settings_file = str(response.get("settingsFileName") or "")
			has_kb = response.get("hasKnowledgeBase")
		else:
			text = str(response or "")
		self._last_draft_text = text
		# 완료된 초안은 마크다운으로 렌더링; 빈 결과만 평문으로.
		if text.strip():
			apply_markdown(self.output, text)
			self.editor_button.setEnabled(True)
		else:
			self.output.setPlainText("(빈 초안이 생성되었습니다)")
			self.editor_button.setEnabled(False)

		if number is not None:
			self._last_draft_number = int(number)
			self._last_settings_file = settings_file
			note = f"설정이 {settings_file} 으로 저장되었습니다 · 초안 #{self._last_draft_number}"
			if has_kb is False:
				note += "  (지식베이스 없음 — 골격 위주로 생성)"
			self.settings_note.setText(note)
		else:
			# 업로드 양식 등 비구조화 경로 — 재생성 대상 없음.
			self._last_draft_number = None
			self._last_settings_file = ""
			self.settings_note.setText("")
		self._sync_busy_state()

	def _on_draft_failed(self, message: str) -> None:
		self.output.setPlainText(f"API 요청 실패: {message}")

	def _copy_output(self) -> None:
		# 렌더된 리치텍스트가 아닌 원본 마크다운을 복사 → 에디터에 깔끔히 붙는다.
		text = self._last_draft_text or self.output.toPlainText()
		QApplication.clipboard().setText(text)

	def _send_to_editor(self) -> None:
		if not self._last_draft_text.strip():
			return
		self.openEditorRequested.emit(self._workspace_id or current_workspace_id(), self._last_draft_text)

	def _reset(self) -> None:
		self._source = None
		self._category = None
		self._subtype = None
		self._uploaded_path = None
		for entry in list(self._section_rows):
			entry["row"].setParent(None)
			entry["row"].deleteLater()
		self._section_rows = []
		self._outline_items = []
		self._rebuild_outline()
		self._last_draft_text = ""
		self._last_draft_number = None
		self._last_settings_file = ""
		self._mode_pill.setText("준비")
		self.settings_note.setText("")
		self.meta_strip.setText("")
		self.file_label.setText("선택된 파일이 없습니다.")
		self.file_options.reset()
		self.custom_options.reset()
		self.editor_button.setEnabled(False)
		if self._category_group.checkedButton() is not None:
			self._category_group.setExclusive(False)
			self._category_group.checkedButton().setChecked(False)
			self._category_group.setExclusive(True)
		self.output.clear()
		self._show_source()

	# ------------------------------------------------------------------ 외부 API
	def set_workspace_by_name(self, workspace_name: str) -> None:
		# 사이드바가 갱신한 부트스트랩 캐시와 id 를 동기화 → 현재 보이는 워크스페이스
		# 기준으로 초안을 생성한다.
		self._workspace_id = current_workspace_id()
		self.workspace_label.setText(f"워크스페이스 · {workspace_name or self._workspace_id}")
		# 초안과 저장 설정은 만들어진 워크스페이스에 속하므로, 전환 시 위저드를 처음으로.
		self._reset()

	def _sync_busy_state(self) -> None:
		blocked = get_job_manager().is_blocked(JobCategory.DRAFT)
		if self._current == "outline":
			self._nav_next.setEnabled(not blocked)
		self.regenerate_button.setEnabled(not blocked and self._last_draft_number is not None)
