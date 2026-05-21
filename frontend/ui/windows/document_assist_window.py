from __future__ import annotations

import math
import re
from pathlib import Path

from PySide6.QtCore import (
	QPoint,
	QPointF,
	QRectF,
	QSize,
	Qt,
	QTimer,
	Signal,
)
from PySide6.QtGui import (
	QBrush,
	QColor,
	QCloseEvent,
	QFont,
	QKeyEvent,
	QKeySequence,
	QMouseEvent,
	QPainter,
	QPainterPath,
	QPen,
	QPixmap,
	QRadialGradient,
	QShortcut,
	QTextCursor,
	QTextDocument,
	QWheelEvent,
)
from PySide6.QtWidgets import (
	QApplication,
	QFrame,
	QGraphicsDropShadowEffect,
	QHBoxLayout,
	QLabel,
	QPushButton,
	QScrollArea,
	QSizeGrip,
	QSizePolicy,
	QSplitter,
	QTextBrowser,
	QTextEdit,
	QVBoxLayout,
	QWidget,
)

from ...controllers import format_screen_event, get_screen_event_store
from ..markdown_view import render_markdown_html


def add_text_breakpoints(text: str, chunk_size: int = 24) -> str:
	def break_long_token(match: re.Match[str]) -> str:
		token = match.group(0)
		return "\u200b".join(token[index : index + chunk_size] for index in range(0, len(token), chunk_size))

	return re.sub(r"\S{32,}", break_long_token, text)


def _render_assistant_markdown(text: str) -> str:
	"""Render an assistant message (markdown) to an HTML fragment.

	A finished assistant answer is parsed once and shown in a QTextBrowser as
	formatted HTML instead of raw `**` / `#` / table syntax. Prefers the
	`markdown` package (correct GFM tables); falls back to Qt's own markdown
	parser so formatting still works even when that package is not installed.
	"""
	rendered = render_markdown_html(text or "", font_size=None)
	if rendered:
		return rendered
	doc = QTextDocument()
	doc.setMarkdown(text or "")
	return doc.toHtml()


def render_history_html(history: list) -> list[dict]:
	"""Pre-render a raw chat history into display-ready bubble specs.

	Each spec is ``{"role", "text", "is_user", "html"}``. This is pure and
	Qt-free (only :func:`render_markdown_html`), so it is safe to run on a
	worker thread — that is the point: hydrating a long workspace history used
	to parse every assistant message's markdown on the UI thread. ``html`` is an
	empty string for user messages and whenever the optional ``markdown``
	package is missing; the bubble then falls back to its own rendering.
	"""
	prepared: list[dict] = []
	for item in history:
		if not isinstance(item, dict):
			continue
		role = str(item.get("role") or "")
		text = str(item.get("text") or "")
		if not text:
			continue
		is_user = role == "user"
		prepared.append(
			{
				"role": role,
				"text": text,
				"is_user": is_user,
				"html": "" if is_user else render_markdown_html(text, font_size=None),
			}
		)
	return prepared


try:
	from shiboken6 import isValid as _shiboken_is_valid
except Exception:  # pragma: no cover - shiboken6 ships with PySide6
	_shiboken_is_valid = None


def _qt_is_alive(obj: object) -> bool:
	"""True when *obj*'s underlying Qt C++ object still exists.

	A streaming chat answer can outlive the bubble it is writing into \u2014 e.g.
	a workspace switch (research completion) clears the panel mid-stream. A
	late chunk that touches the freed QLabel would otherwise raise
	``RuntimeError: Internal C++ object already deleted``.
	"""
	if obj is None:
		return False
	if _shiboken_is_valid is None:
		return True
	try:
		return bool(_shiboken_is_valid(obj))
	except Exception:
		return False


class ChatInputEdit(QTextEdit):
	sendRequested = Signal()

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setAcceptRichText(False)

	def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
		if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
			event.accept()
			self.sendRequested.emit()
			return
		super().keyPressEvent(event)


class StatusBadge(QLabel):
	COLORS = {
		"working": ("#DBEAFE", "#1D4ED8", "#BFDBFE"),
		"idle": ("#F3F4F6", "#6B7280", "#E5E7EB"),
		"warning": ("#FEF3C7", "#B45309", "#FDE68A"),
		"error": ("#FEE2E2", "#DC2626", "#FECACA"),
	}

	def __init__(self, text: str, tone: str = "idle", parent: QWidget | None = None) -> None:
		super().__init__(text, parent)
		self.setObjectName("AssistStatusBadge")
		bg, fg, border = self.COLORS.get(tone, self.COLORS["idle"])
		self.setStyleSheet(
			f"""
			QLabel#AssistStatusBadge {{
				background-color: {bg};
				color: {fg};
				border: 1px solid {border};
				border-radius: 10px;
				padding: 3px 8px;
				font-size: 11px;
				font-weight: 800;
			}}
			"""
		)


# Window-control glyphs. Plain-text symbols (－ ▢ ×) render as tofu in the UI
# font; these Private-Use codepoints come from Windows' built-in icon fonts
# (Win11: "Segoe Fluent Icons", Win10: "Segoe MDL2 Assets") and draw crisp
# minimise / maximise / restore / close marks. Shared by the editor title bar
# (which imports them) so both frameless windows match.
WIN_GLYPH_MINIMIZE = chr(0xE921)  # ChromeMinimize
WIN_GLYPH_MAXIMIZE = chr(0xE922)  # ChromeMaximize
WIN_GLYPH_RESTORE = chr(0xE923)  # ChromeRestore
WIN_GLYPH_CLOSE = chr(0xE8BB)  # ChromeClose


def apply_window_icon_font(button: QPushButton, point_size: int = 10) -> None:
	"""Render a window-control button with the OS icon font so its glyph is not
	subject to the UI font (which lacks these symbols)."""
	font = QFont()
	font.setFamilies(["Segoe Fluent Icons", "Segoe MDL2 Assets"])
	font.setPointSize(point_size)
	button.setFont(font)


_LOGO_PNG = Path(__file__).resolve().parent.parent / "public" / "images" / "veritas_logo.png"


class WindowControlButton(QPushButton):
	"""Minimise / maximise / restore / close button that *draws* its own glyph
	with QPainter, so it is always visible regardless of which icon fonts the
	system has (the earlier font-glyph buttons rendered blank on some setups)."""

	def __init__(self, role: str, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._role = role  # 'min' | 'max' | 'restore' | 'close'
		self.setObjectName("WinCtlCloseButton" if role == "close" else "WinCtlButton")
		self.setFixedSize(44, 30)
		self.setCursor(Qt.PointingHandCursor)
		self.setFocusPolicy(Qt.NoFocus)

	def set_role(self, role: str) -> None:
		self._role = role
		self.update()

	def paintEvent(self, event) -> None:  # type: ignore[override]
		super().paintEvent(event)  # background / hover from the stylesheet
		painter = QPainter(self)
		painter.setRenderHint(QPainter.Antialiasing, True)
		hovered = self.underMouse()
		color = QColor("#FFFFFF") if (self._role == "close" and hovered) else QColor("#3C4043")
		pen = QPen(color)
		pen.setWidthF(1.4)
		painter.setPen(pen)
		r = self.rect()
		cx, cy = r.center().x() + 1, r.center().y() + 1
		h = 5
		if self._role == "min":
			painter.drawLine(cx - h, cy, cx + h, cy)
		elif self._role == "max":
			painter.drawRect(cx - h, cy - h, 2 * h, 2 * h)
		elif self._role == "restore":
			painter.drawRect(cx - h, cy - h + 2, 2 * h - 2, 2 * h - 2)
			painter.drawLine(cx - h + 2, cy - h + 2, cx - h + 2, cy - h)
			painter.drawLine(cx - h + 2, cy - h, cx + h, cy - h)
			painter.drawLine(cx + h, cy - h, cx + h, cy + h - 2)
			painter.drawLine(cx + h, cy + h - 2, cx + h - 2, cy + h - 2)
		else:  # close
			painter.drawLine(cx - h, cy - h, cx + h, cy + h)
			painter.drawLine(cx - h, cy + h, cx + h, cy - h)
		painter.end()


class VeritasTitleBar(QFrame):
	"""Shared frameless title bar: Veritas logo + 'VERITAS' + optional subtitle /
	status badge, with drawn minimise / maximise / close buttons and drag-to-move.
	Used by every Veritas window so they share one chrome."""

	def __init__(
		self,
		window: QWidget,
		*,
		subtitle: bool = False,
		status: bool = False,
		maximize: bool = True,
		on_close=None,
		parent: QWidget | None = None,
	) -> None:
		super().__init__(parent)
		self._window = window
		self._drag_start: QPoint | None = None
		self._can_maximize = maximize
		self.setObjectName("VeritasTitleBar")

		layout = QHBoxLayout(self)
		layout.setContentsMargins(12, 7, 8, 7)
		layout.setSpacing(8)

		logo = QLabel()
		logo.setObjectName("VeritasTitleLogo")
		if _LOGO_PNG.exists():
			logo.setPixmap(QPixmap(str(_LOGO_PNG)).scaled(20, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation))
		layout.addWidget(logo, 0, Qt.AlignVCenter)

		brand = QLabel("VERITAS")
		brand.setObjectName("VeritasTitleBrand")
		layout.addWidget(brand, 0, Qt.AlignVCenter)

		self.subtitle = None
		if subtitle:
			sep = QLabel("·")
			sep.setObjectName("VeritasTitleSep")
			layout.addWidget(sep, 0, Qt.AlignVCenter)
			self.subtitle = QLabel("")
			self.subtitle.setObjectName("VeritasTitleSub")
			layout.addWidget(self.subtitle, 0, Qt.AlignVCenter)

		layout.addStretch(1)

		self.status = None
		if status:
			self.status = StatusBadge("● 대기", "idle")
			layout.addWidget(self.status, 0, Qt.AlignVCenter)

		self.minimize_button = WindowControlButton("min")
		self.minimize_button.clicked.connect(window.showMinimized)
		layout.addWidget(self.minimize_button)

		self.maximize_button = WindowControlButton("max")
		if maximize:
			self.maximize_button.clicked.connect(self._toggle_max_restore)
			layout.addWidget(self.maximize_button)

		self.close_button = WindowControlButton("close")
		self.close_button.clicked.connect(on_close or window.close)
		layout.addWidget(self.close_button)

	def set_subtitle(self, text: str) -> None:
		if self.subtitle is not None:
			self.subtitle.setText(text or "")

	def _toggle_max_restore(self) -> None:
		if self._window.isMaximized():
			self._window.showNormal()
			self.maximize_button.set_role("max")
		else:
			self._window.showMaximized()
			self.maximize_button.set_role("restore")

	def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
		if self._can_maximize:
			self._toggle_max_restore()

	def mousePressEvent(self, event) -> None:  # type: ignore[override]
		if event.button() == Qt.LeftButton and not self._window.isMaximized():
			self._drag_start = event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
			event.accept()
			return
		super().mousePressEvent(event)

	def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
		if self._drag_start is not None and event.buttons() & Qt.LeftButton:
			self._window.move(event.globalPosition().toPoint() - self._drag_start)
			event.accept()
			return
		super().mouseMoveEvent(event)

	def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
		self._drag_start = None
		super().mouseReleaseEvent(event)


# Stylesheet for the shared title bar; appended to each window's stylesheet so
# all three windows share one chrome look.
VERITAS_TITLEBAR_QSS = """
	QFrame#VeritasTitleBar {
		background-color: #FFFFFF;
		border-top-left-radius: 16px;
		border-top-right-radius: 16px;
		border-bottom: 1px solid #E5E7EB;
	}
	QLabel#VeritasTitleBrand { color: #111827; font-size: 13px; font-weight: 850; letter-spacing: 1px; }
	QLabel#VeritasTitleSub { color: #6B7280; font-size: 11px; font-weight: 650; }
	QLabel#VeritasTitleSep { color: #CBD5E1; font-size: 12px; }
	QPushButton#WinCtlButton, QPushButton#WinCtlCloseButton {
		background-color: transparent; border: none; border-radius: 6px;
	}
	QPushButton#WinCtlButton:hover { background-color: #EDEFF2; }
	QPushButton#WinCtlCloseButton:hover { background-color: #E81123; }
"""


# Chat surface styling, copied from the main window so the editor's 대화 tab
# renders identically to the main 채팅 page (both reuse ChatPanel / ChatInputBar /
# ChatMessageBubble, but each top-level window must carry the rules itself).
# Keep in sync with the matching rules in MainWindow._build_stylesheet.
CHAT_QSS = """
	QFrame#AssistPagePanel { background-color: #F8FAFC; border: 1px solid #E5E7EB; border-radius: 16px; }
	QFrame#AssistSectionCard { background-color: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 13px; }
	QLabel#AssistSectionTitle { color: #111827; font-size: 13px; font-weight: 850; }
	QScrollArea#AssistScrollArea { background-color: transparent; border: none; }
	QScrollArea#ChatScroll { background: transparent; border: none; }
	QLabel#AssistEmptyState { background-color: #F8FAFC; border: 1px dashed #CBD5E1; border-radius: 12px; color: #6B7280; padding: 18px 14px; font-weight: 650; }
	QPushButton#AssistCopyButton { background-color: #FFFFFF; color: #4B5563; border: 1px solid #D1D5DB; border-radius: 8px; padding: 5px 8px; font-size: 11px; font-weight: 800; }
	QPushButton#AssistCopyButton:hover { background-color: #F3F4F6; color: #111827; }
	QFrame#AssistUserBubble { background-color: #DBEAFE; border: 1px solid #BFDBFE; border-radius: 13px; border-top-right-radius: 4px; }
	QFrame#AssistAiBubble { background-color: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 13px; border-top-left-radius: 4px; }
	QLabel#AssistBubbleMeta { color: #6B7280; font-size: 10px; font-weight: 800; }
	QTextBrowser#AssistBubbleText { color: #1F2937; font-size: 12px; font-weight: 600; background: transparent; border: none; }
	QFrame#AssistInputBar { background-color: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 14px; }
	QTextEdit#AssistChatInput { background-color: #F8FAFC; border: 1px solid #E5E7EB; border-radius: 11px; padding: 8px 10px; color: #111827; selection-background-color: #BFDBFE; selection-color: #111827; }
	QTextEdit#AssistChatInput:focus { background-color: #FFFFFF; border: 1px solid #3B82F6; }
	QPushButton#AssistSendButton { background-color: #3B82F6; border: 1px solid #2563EB; border-radius: 11px; color: #FFFFFF; font-weight: 850; }
	QPushButton#AssistSendButton:hover { background-color: #2563EB; }
	QPushButton#AssistModeButton { background-color: #F1F5F9; color: #475569; border: 1px solid #D1D5DB; border-radius: 11px; padding: 0px; font-size: 13px; font-weight: 800; }
	QPushButton#AssistModeButton:hover { background-color: #E0E7FF; border-color: #818CF8; color: #3730A3; }
	QPushButton#AssistModeButton[researchActive="true"] { background-color: #1E3A8A; border-color: #1E3A8A; color: #FFFFFF; }
	QPushButton#AssistModeButton[researchActive="true"]:hover { background-color: #1E40AF; border-color: #1E40AF; color: #FFFFFF; }
"""


class WindowIconButton(QPushButton):
	def __init__(self, text: str, role: str, parent: QWidget | None = None) -> None:
		super().__init__(text, parent)
		self.setObjectName("AssistCloseButton" if role == "close" else "AssistMinimizeButton")
		self.setFixedSize(32, 32)
		apply_window_icon_font(self)
		self.setCursor(Qt.PointingHandCursor)


class CustomTitleBar(QFrame):
	def __init__(self, window: QWidget, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._window = window
		self._drag_start: QPoint | None = None
		self.setObjectName("AssistTitleBar")

		layout = QHBoxLayout(self)
		layout.setContentsMargins(14, 10, 10, 8)
		layout.setSpacing(8)

		title_col = QVBoxLayout()
		title_col.setContentsMargins(0, 0, 0, 0)
		title_col.setSpacing(1)

		title = QLabel("Veritas Assist")
		title.setObjectName("AssistWindowTitle")
		self.document_context = QLabel("보고서_초안.docx · 마지막 분석: 방금 전")
		self.document_context.setObjectName("AssistTitleContext")

		title_col.addWidget(title)
		title_col.addWidget(self.document_context)

		self.status = StatusBadge("● 분석 중", "working")
		self.minimize_button = WindowIconButton(WIN_GLYPH_MINIMIZE, "minimize")
		self.close_button = WindowIconButton(WIN_GLYPH_CLOSE, "close")
		self.minimize_button.clicked.connect(window.showMinimized)
		self.close_button.clicked.connect(window.hide)

		layout.addLayout(title_col)
		layout.addStretch(1)
		layout.addWidget(self.status)
		layout.addWidget(self.minimize_button)
		layout.addWidget(self.close_button)

	def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
		if event.button() == Qt.LeftButton:
			self._drag_start = event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
			event.accept()
			return
		super().mousePressEvent(event)

	def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
		if self._drag_start is not None and event.buttons() & Qt.LeftButton:
			self._window.move(event.globalPosition().toPoint() - self._drag_start)
			event.accept()
			return
		super().mouseMoveEvent(event)

	def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
		self._drag_start = None
		super().mouseReleaseEvent(event)


class SuggestionCard(QFrame):
	def __init__(self, category: str, text: str, tone: str = "working", parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("SuggestionCard")
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

		layout = QVBoxLayout(self)
		layout.setContentsMargins(12, 11, 12, 11)
		layout.setSpacing(8)

		header = QHBoxLayout()
		header.setSpacing(8)

		badge = StatusBadge(category, tone)
		copy_button = QPushButton("복사")
		copy_button.setObjectName("AssistCopyButton")
		copy_button.setFixedSize(46, 28)
		copy_button.setCursor(Qt.PointingHandCursor)
		self._copy_value = text
		copy_button.clicked.connect(lambda: self._copy_text(self._copy_value))

		header.addWidget(badge, 0, Qt.AlignTop)
		header.addStretch(1)
		header.addWidget(copy_button)

		self._body = QLabel(add_text_breakpoints(text))
		self._body.setObjectName("SuggestionText")
		self._body.setWordWrap(True)
		self._body.setTextFormat(Qt.PlainText)
		self._body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
		self._body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
		self._body.setAlignment(Qt.AlignLeft | Qt.AlignTop)

		layout.addLayout(header)
		layout.addWidget(self._body)

	def set_card_width(self, width: int) -> None:
		self._card_width = width
		self.setMinimumWidth(width)
		self.setMaximumWidth(width)
		self._sync_height()

	def _sync_height(self) -> None:
		"""Pin the card to the real wrapped text height for its current width.

		A word-wrapped QLabel reports a width-agnostic sizeHint/minimumSizeHint
		that over-estimates its height, which forced the surrounding list to
		reserve slack below the text. Locking the height to ``heightForWidth``
		makes the card hug its text exactly, so no blank trails it.
		"""
		width = getattr(self, "_card_width", 0)
		if width <= 0:
			return
		height = self.layout().heightForWidth(width)
		if height > 0:
			self.setFixedHeight(height)
			self.updateGeometry()

	def _copy_text(self, text: str) -> None:
		app = QApplication.instance()
		if app is not None:
			app.clipboard().setText(text)

	def set_text(self, text: str) -> None:
		"""Replace the card body text in place (used for streaming updates)."""
		self._copy_value = text
		self._body.setText(add_text_breakpoints(text))
		# Streaming text grows the body; re-pin so the card tracks it.
		self._sync_height()


class SuggestionList(QFrame):
	# When ``hug_content`` is set the card sizes itself to its actual content
	# instead of claiming whatever slice of the window the layout offers, so
	# no blank space trails the last suggestion. It still refuses to grow past
	# this share of the parent's height — beyond that the list scrolls (still
	# no trailing gap). Left off, the card keeps the default fill behaviour for
	# callers that give it a whole panel (e.g. DocumentAssistPage).
	MAX_HEIGHT_RATIO = 0.55

	def __init__(self, parent: QWidget | None = None, *, hug_content: bool = False) -> None:
		super().__init__(parent)
		self.setObjectName("AssistSectionCard")
		self._hug_content = hug_content
		if hug_content:
			# Maximum: take exactly the content height when it fits, never more.
			self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
		self._suggestions: list[dict[str, str]] = []
		self._cards: list[SuggestionCard] = []
		self._cards_by_id: dict[str, SuggestionCard] = {}

		self._root = QVBoxLayout(self)
		self._root.setContentsMargins(12, 12, 12, 12)
		self._root.setSpacing(8)

		header = QHBoxLayout()
		header.setSpacing(8)

		self._title = QLabel("실시간 수정 결과")
		self._title.setObjectName("AssistSectionTitle")
		self.count_label = QLabel("0개")
		self.count_label.setObjectName("AssistSubText")

		header.addWidget(self._title)
		header.addStretch(1)
		header.addWidget(self.count_label)

		self.scroll = QScrollArea()
		self.scroll.setWidgetResizable(True)
		self.scroll.setFrameShape(QFrame.NoFrame)
		self.scroll.setObjectName("AssistScrollArea")
		self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
		self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

		self.container = QWidget()
		self.container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
		self.layout = QVBoxLayout(self.container)
		self.layout.setContentsMargins(0, 0, 0, 0)
		self.layout.setSpacing(8)
		self.scroll.setWidget(self.container)

		self.empty = QLabel("문서를 작성하면 수정 결과가 여기에 표시됩니다.")
		self.empty.setObjectName("AssistEmptyState")
		self.empty.setAlignment(Qt.AlignCenter)
		self.empty.setWordWrap(True)

		self._root.addLayout(header)
		self._root.addWidget(self.scroll, 1)
		self.set_suggestions([])

	def set_suggestions(self, suggestions: list[dict[str, str]]) -> None:
		self._suggestions = [dict(item) for item in suggestions]
		self._clear()
		self._cards = []
		self._cards_by_id = {}
		if not self._suggestions:
			self.layout.addWidget(self.empty)
			self.empty.show()
			self.count_label.setText("0개")
			self.layout.addStretch(1)
			self.schedule_width_update()
			if self._hug_content:
				self.updateGeometry()
			return

		for item in self._suggestions:
			card = SuggestionCard(
				item.get("category", "수정"),
				item.get("text", item.get("description", "")),
				item.get("tone", "working"),
			)
			card.set_card_width(self._content_width())
			self._cards.append(card)
			item_id = str(item.get("id") or "")
			if item_id:
				self._cards_by_id[item_id] = card
			# Every card keeps its natural height so the text box hugs its text.
			self.layout.addWidget(card)
		# A trailing stretch soaks up the leftover panel height, so the slack
		# falls below the list as plain background instead of inflating the
		# last card and trailing a blank gap under its text.
		self.layout.addStretch(1)

		self.count_label.setText(f"{len(self._suggestions)}개")
		self.schedule_width_update()
		self.schedule_scroll_to_bottom()
		if self._hug_content:
			self.updateGeometry()

	def add_suggestion(self, category: str, text: str, tone: str = "working", *, event_id: str = "") -> None:
		"""Append a single suggestion card.

		Unlike :meth:`set_suggestions`, this does not tear down and rebuild every
		existing card. The screen-monitoring poller feeds suggestions in one at a
		time, so a full rebuild per event would be O(n²) as the list grows.
		"""
		item: dict[str, str] = {"category": category, "text": text, "tone": tone}
		if event_id:
			item["id"] = event_id
		if not self._cards:
			# Leaving the empty state: drop the placeholder + its trailing stretch.
			self._clear()
		else:
			# Lift the trailing stretch so the new card lands above it; it gets
			# re-added below to keep absorbing the leftover panel height.
			self._drop_trailing_stretch()

		self._suggestions.append(item)
		card = SuggestionCard(item["category"], item["text"], item["tone"])
		card.set_card_width(self._content_width())
		self._cards.append(card)
		if event_id:
			self._cards_by_id[event_id] = card
		self.layout.addWidget(card)
		self.layout.addStretch(1)

		self.count_label.setText(f"{len(self._suggestions)}개")
		self.schedule_width_update()
		self.schedule_scroll_to_bottom()

	def upsert_suggestion(self, event_id: str, category: str, text: str, tone: str = "working") -> None:
		"""Update an existing card's text by ``event_id``, or add a new card.

		Streaming screen-intervention answers arrive as the same ``event_id``
		with growing text; updating in place avoids spawning a duplicate card
		on every poll.
		"""
		card = self._cards_by_id.get(event_id) if event_id else None
		if card is not None:
			card.set_text(text)
			for item in self._suggestions:
				if item.get("id") == event_id:
					item["text"] = text
					break
			self.schedule_scroll_to_bottom()
			return
		self.add_suggestion(category, text, tone, event_id=event_id)
		if self._hug_content:
			self.updateGeometry()

	def schedule_scroll_to_bottom(self) -> None:
		for delay in (0, 25, 80):
			QTimer.singleShot(delay, self._scroll_to_bottom)

	def schedule_width_update(self) -> None:
		for delay in (0, 25, 80, 160):
			QTimer.singleShot(delay, self._update_card_widths)

	def _scroll_to_bottom(self) -> None:
		self._update_card_widths()
		self.container.adjustSize()
		bar = self.scroll.verticalScrollBar()
		bar.setValue(bar.maximum())

	def resizeEvent(self, event) -> None:  # type: ignore[override]
		super().resizeEvent(event)
		self.schedule_width_update()
		# The height cap tracks the parent, which changes as the window
		# resizes — re-query our sizeHint so the cap stays in sync.
		if self._hug_content:
			self.updateGeometry()

	def showEvent(self, event) -> None:  # type: ignore[override]
		super().showEvent(event)
		self.schedule_width_update()
		if self._hug_content:
			self.updateGeometry()

	def sizeHint(self) -> QSize:  # type: ignore[override]
		hint = super().sizeHint()
		if self._hug_content:
			hint.setHeight(self._preferred_height())
		return hint

	def _preferred_height(self) -> int:
		"""Height that exactly wraps the current content, capped so a long
		list scrolls instead of crowding out the chat panel."""
		margins = self._root.contentsMargins()
		chrome = margins.top() + margins.bottom() + self._root.spacing()
		header_h = max(
			self._title.sizeHint().height(),
			self.count_label.sizeHint().height(),
		)
		# +2px rounding cushion so the list never shows a scrollbar while it
		# actually fits; the trailing stretch soaks up the slack.
		content_h = self.container.sizeHint().height() + 2
		total = chrome + header_h + content_h
		cap = self._max_height()
		if cap:
			total = min(total, cap)
		return max(96, total)

	def _max_height(self) -> int:
		# Cap against the top-level window, not the immediate parent: on the
		# DocumentAssistPage the parent panel hugs *this* widget, so reading the
		# parent's height would feed back on itself and collapse the box. The
		# window height is a stable reference in both the page and the floating
		# window (where parent ≈ window anyway).
		window = self.window()
		ref = window.height() if window is not None else 0
		if ref <= 0:
			parent = self.parentWidget()
			ref = parent.height() if parent is not None else 0
		if ref <= 0:
			return 0
		return max(160, int(ref * self.MAX_HEIGHT_RATIO))

	def _content_width(self) -> int:
		return max(120, self.scroll.viewport().width() - 2)

	def _update_card_widths(self) -> None:
		width = self._content_width()
		self.container.setMinimumWidth(width)
		for card in self._cards:
			card.set_card_width(width)
		# Card heights (word-wrapped text) just changed — refresh our hint.
		if self._hug_content:
			self.updateGeometry()

	def _drop_trailing_stretch(self) -> None:
		"""Remove the trailing stretch spacer if the layout currently has one."""
		count = self.layout.count()
		if count == 0:
			return
		item = self.layout.itemAt(count - 1)
		if item is not None and item.widget() is None:
			self.layout.takeAt(count - 1)

	def _clear(self) -> None:
		while self.layout.count():
			item = self.layout.takeAt(0)
			widget = item.widget()
			if widget is not None:
				widget.setParent(None)


class CopyButton(QPushButton):
	"""Icon-only button that copies a chat message to the clipboard.

	The conventional "two overlapping sheets" copy glyph is hand-painted so it
	looks identical regardless of which window's stylesheet is active, and
	briefly swaps to a green checkmark as copy confirmation.
	"""

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("ChatCopyButton")
		self.setCursor(Qt.PointingHandCursor)
		self.setFixedSize(22, 22)
		self.setToolTip("답변 복사")
		self.setStyleSheet(
			"QPushButton#ChatCopyButton { background-color: transparent; border: none; "
			"border-radius: 6px; }"
			"QPushButton#ChatCopyButton:hover { background-color: rgba(15, 23, 42, 0.07); }"
		)
		self._copied = False
		self._reset_timer = QTimer(self)
		self._reset_timer.setSingleShot(True)
		self._reset_timer.setInterval(1300)
		self._reset_timer.timeout.connect(self._reset_state)

	def show_copied(self) -> None:
		self._copied = True
		self.setToolTip("복사됨")
		self._reset_timer.start()
		self.update()

	def _reset_state(self) -> None:
		self._copied = False
		self.setToolTip("답변 복사")
		self.update()

	def paintEvent(self, event) -> None:  # type: ignore[override]
		super().paintEvent(event)  # hover background from the stylesheet
		painter = QPainter(self)
		painter.setRenderHint(QPainter.Antialiasing, True)
		center = self.rect().center()
		cx, cy = center.x() + 0.5, center.y() + 0.5

		if self._copied:
			pen = QPen(QColor("#15803D"))
			pen.setWidthF(1.7)
			pen.setCapStyle(Qt.RoundCap)
			pen.setJoinStyle(Qt.RoundJoin)
			painter.setPen(pen)
			check = QPainterPath()
			check.moveTo(cx - 4.2, cy + 0.4)
			check.lineTo(cx - 1.2, cy + 3.4)
			check.lineTo(cx + 4.6, cy - 3.6)
			painter.drawPath(check)
			return

		color = QColor("#0F172A") if self.underMouse() else QColor("#94A3B8")
		pen = QPen(color)
		pen.setWidthF(1.4)
		pen.setJoinStyle(Qt.RoundJoin)
		painter.setPen(pen)
		# Back sheet peeks out to the top-right; the front sheet is filled so it
		# cleanly occludes the overlapping back-sheet edges.
		back = QRectF(cx - 2.4, cy - 5.8, 8.2, 8.2)
		front = QRectF(cx - 5.8, cy - 2.4, 8.2, 8.2)
		painter.drawRoundedRect(back, 1.8, 1.8)
		front_path = QPainterPath()
		front_path.addRoundedRect(front, 1.8, 1.8)
		painter.fillPath(front_path, QColor("#FFFFFF"))
		painter.drawPath(front_path)


class TypingIndicator(QWidget):
	"""Animated 'assistant is generating an answer' indicator.

	Ports the 'pulse' loader from logo.html: the Veritas logo gently breathes
	(scale + opacity) over a soft blue radial glow that pulses behind it.
	Shown inside an assistant bubble in place of the answer text until the
	first streamed token arrives, replacing the old static '…' placeholder.

	A plain ``QTimer`` drives the motion: it is started/stopped explicitly by
	``set_typing`` and never tied to show/hide events, so a transient hide — a
	page switch, a layout reflow — can never silently freeze it.
	"""

	_SIZE = 40
	_LOGO_SIZE = 22
	_GLOW_BASE_RADIUS = 14.0
	_TICK_MS = 40
	_PERIOD_MS = 1400

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._phase = 0.0
		self.setFixedSize(self._SIZE, self._SIZE)
		logo_path = (
			Path(__file__).resolve().parent.parent
			/ "public" / "images" / "veritas_logo.png"
		)
		pixmap = QPixmap(str(logo_path)) if logo_path.exists() else QPixmap()
		if not pixmap.isNull():
			pixmap = pixmap.scaled(
				self._LOGO_SIZE * 2,
				self._LOGO_SIZE * 2,
				Qt.KeepAspectRatio,
				Qt.SmoothTransformation,
			)
		self._logo = pixmap
		self._timer = QTimer(self)
		self._timer.setInterval(self._TICK_MS)
		self._timer.timeout.connect(self._advance)

	def _advance(self) -> None:
		self._phase = (self._phase + self._TICK_MS / self._PERIOD_MS) % 1.0
		self.update()

	def start(self) -> None:
		if not self._timer.isActive():
			self._timer.start()

	def stop(self) -> None:
		self._timer.stop()

	def paintEvent(self, event) -> None:  # type: ignore[override]
		painter = QPainter(self)
		painter.setRenderHint(QPainter.Antialiasing, True)
		painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
		cx = self.width() / 2.0
		cy = self.height() / 2.0
		# Smooth 0 -> 1 -> 0 'breathing' value (ease-in-out), matching the
		# logo.html 'pulse' keyframes (0%/100% small, 50% large).
		breath = (1.0 - math.cos(2.0 * math.pi * self._phase)) / 2.0

		# Pulsing radial glow behind the logo (scale 0.6..1.4, fades in/out).
		glow_radius = self._GLOW_BASE_RADIUS * (0.6 + 0.8 * breath)
		if glow_radius > 0.5:
			gradient = QRadialGradient(cx, cy, glow_radius)
			center = QColor(30, 98, 255)
			center.setAlphaF(0.33 * breath)
			edge = QColor(30, 98, 255, 0)
			gradient.setColorAt(0.0, center)
			gradient.setColorAt(0.65, edge)
			gradient.setColorAt(1.0, edge)
			painter.fillRect(self.rect(), QBrush(gradient))

		# Breathing logo (scale 0.85..1.05, opacity 0.7..1.0).
		logo_scale = 0.85 + 0.20 * breath
		if not self._logo.isNull():
			draw = self._LOGO_SIZE * logo_scale
			painter.setOpacity(0.7 + 0.30 * breath)
			painter.drawPixmap(
				QRectF(cx - draw / 2.0, cy - draw / 2.0, draw, draw),
				self._logo,
				QRectF(self._logo.rect()),
			)
			painter.setOpacity(1.0)
		else:
			# Fallback when the logo asset is missing: a breathing blue dot.
			radius = (self._LOGO_SIZE / 2.0) * logo_scale
			dot = QColor(30, 98, 255)
			dot.setAlphaF(0.7 + 0.30 * breath)
			painter.setPen(Qt.NoPen)
			painter.setBrush(dot)
			painter.drawEllipse(QPointF(cx, cy), radius, radius)


class _BubbleTextBrowser(QTextBrowser):
	"""Read-only QTextBrowser used as a chat-message body.

	It grows to fit its content (the inner scrollbar is disabled) so the bubble
	height tracks the message. While a streamed answer is still arriving the
	raw text is shown verbatim — fast, no markdown parse per token — and the
	finished answer is rendered from markdown to HTML exactly once.
	"""

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("AssistBubbleText")
		self.setReadOnly(True)
		self.setOpenExternalLinks(True)
		self.setFrameShape(QFrame.NoFrame)
		self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
		self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
		self.setTextInteractionFlags(
			Qt.TextSelectableByMouse
			| Qt.TextSelectableByKeyboard
			| Qt.LinksAccessibleByMouse
		)
		self.viewport().setAutoFillBackground(False)
		self.document().setDocumentMargin(0)
		self.document().contentsChanged.connect(self._sync_height)

	def set_plain(self, text: str) -> None:
		self.setPlainText(text or "")

	def append_plain(self, chunk: str) -> None:
		"""Append a streamed chunk at the end — O(chunk), unlike re-setting the
		whole document with setPlainText() on every token."""
		cursor = QTextCursor(self.document())
		cursor.movePosition(QTextCursor.End)
		cursor.insertText(chunk)

	def set_html(self, html: str) -> None:
		self.setHtml(html or "")

	def refresh_height(self) -> None:
		self._sync_height()

	def _sync_height(self) -> None:
		"""Pin the widget height to the document height so the bubble — not an
		inner scrollbar — grows with the message."""
		width = self.viewport().width()
		if width <= 0:
			return
		doc = self.document()
		doc.setTextWidth(width)
		height = int(math.ceil(doc.size().height())) + 2
		if height != self.height():
			self.setFixedHeight(height)

	def resizeEvent(self, event) -> None:  # type: ignore[override]
		super().resizeEvent(event)
		self._sync_height()

	def wheelEvent(self, event) -> None:  # type: ignore[override]
		# Don't consume wheel scrolling — let it bubble up to the chat panel's
		# scroll area so the conversation scrolls when hovering over a message.
		event.ignore()


class ChatMessageBubble(QFrame):
	def __init__(
		self,
		sender: str,
		message: str,
		is_user: bool,
		parent: QWidget | None = None,
		*,
		rendered_html: str | None = None,
	) -> None:
		super().__init__(parent)
		self._is_user = is_user
		# The original message text, kept so the copy button yields clean text
		# rather than anything re-extracted from the rendered HTML.
		self._raw_message = message or ""
		self.setObjectName("AssistUserBubble" if is_user else "AssistAiBubble")
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

		layout = QVBoxLayout(self)
		layout.setContentsMargins(11, 9, 11, 9)
		layout.setSpacing(3)

		meta = QLabel(sender)
		meta.setObjectName("AssistBubbleMeta")

		self.text = _BubbleTextBrowser()
		# User messages and the live streaming view are shown as raw text — fast,
		# no markdown parse on the per-token path. A finished assistant answer is
		# rendered from markdown to HTML once, via set_markdown(); an empty
		# assistant bubble is a streaming turn about to begin, so it stays in
		# raw-text mode until then. `rendered_html` lets a caller supply HTML that
		# was already parsed off the UI thread (history hydration) so the parse
		# is never paid here.
		if is_user:
			self.text.set_plain(message or "")
		elif rendered_html:
			self.text.set_html(rendered_html)
		elif message:
			self.text.set_html(_render_assistant_markdown(message))
		else:
			self.text.set_plain("")

		layout.addWidget(meta)
		layout.addWidget(self.text)

		# Animated 'generating…' indicator, shown in place of `text` while a
		# streaming answer has not produced its first token yet.
		self.typing: TypingIndicator | None = None
		if not is_user:
			self.typing = TypingIndicator()
			self.typing.hide()
			layout.addWidget(self.typing, 0, Qt.AlignLeft)

		# Only AI answers are copyable; user messages already came from them.
		# The copy button sits in a footer row, aligned to the bottom-right.
		self.copy_button: CopyButton | None = None
		if not is_user:
			footer = QHBoxLayout()
			footer.setContentsMargins(0, 0, 0, 0)
			footer.setSpacing(0)
			footer.addStretch(1)
			self.copy_button = CopyButton()
			self.copy_button.clicked.connect(self._copy_to_clipboard)
			footer.addWidget(self.copy_button, 0, Qt.AlignVCenter)
			layout.addLayout(footer)

	def set_typing(self, active: bool) -> None:
		"""Show the animated typing indicator in place of the answer text.

		Used on an assistant bubble between the request and its first streamed
		token; the first ``append_text`` call flips it back off.
		"""
		if self.typing is None:
			return
		if not _qt_is_alive(self.typing) or not _qt_is_alive(self.text):
			return
		self.typing.setVisible(active)
		self.text.setVisible(not active)
		if active:
			self.typing.start()
		else:
			self.typing.stop()

	def append_text(self, chunk: str) -> None:
		"""Append one streamed chunk as raw text.

		Appends only the new chunk (O(chunk)) instead of re-setting the whole
		answer every token, so streaming stays fast as the answer grows. The
		finished answer is re-rendered once from markdown via set_markdown().
		"""
		if not chunk:
			return
		# Defensive: the browser can be torn down (panel cleared / window
		# closed) while a streaming answer is still arriving.
		if not _qt_is_alive(self.text):
			return
		self.text.append_plain(chunk)
		# The first streamed token ends the typing phase.
		self.set_typing(False)

	def set_markdown(self, raw: str) -> None:
		"""Render *raw* markdown to HTML once and show it — used when a streamed
		answer is finalised, so the expensive parse happens a single time.
		"""
		self._raw_message = raw or ""
		if not _qt_is_alive(self.text):
			return
		self.text.set_html(_render_assistant_markdown(self._raw_message))
		self.set_typing(False)

	def _copy_to_clipboard(self) -> None:
		# `_raw_message` is filled in by set_markdown() once the answer is
		# finalised; mid-stream it is still empty, so fall back to whatever raw
		# text the browser is currently showing.
		text = self._raw_message
		if not text and _qt_is_alive(self.text):
			text = self.text.toPlainText()
		app = QApplication.instance()
		if app is not None:
			app.clipboard().setText(text)
		if self.copy_button is not None:
			self.copy_button.show_copied()

	def set_bubble_width(self, max_width: int) -> None:
		self.setMaximumWidth(max_width)
		# The browser fills the bubble width; nudge it to re-measure its height
		# for the new wrap width (and after a font-zoom change).
		if _qt_is_alive(self.text):
			self.text.refresh_height()


class ChatPanel(QFrame):
	# Chat-answer text size. The base is bumped up from the old 12px (the
	# answers read too small) and Ctrl +/- steps it between these bounds.
	_BASE_FONT_PT = 14
	_MIN_FONT_PT = 9
	_MAX_FONT_PT = 30

	def __init__(self, title_text: str = "문서 채팅", parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("AssistSectionCard")
		self._bubble_font_pt = self._BASE_FONT_PT

		root = QVBoxLayout(self)
		root.setContentsMargins(12, 12, 12, 12)
		root.setSpacing(8)

		title = QLabel(title_text)
		title.setObjectName("AssistSectionTitle")

		self.scroll = QScrollArea()
		self.scroll.setWidgetResizable(True)
		self.scroll.setFrameShape(QFrame.NoFrame)
		self.scroll.setObjectName("AssistScrollArea")
		self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

		self.container = QWidget()
		self.container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
		self.layout = QVBoxLayout(self.container)
		self.layout.setContentsMargins(0, 0, 0, 0)
		self.layout.setSpacing(8)
		self.layout.addStretch(1)
		self.scroll.setWidget(self.container)

		self.empty = QLabel("문서 내용에 대해 질문해보세요.\n예: 이 문단 자연스러워? 근거가 부족한 부분 찾아줘")
		self.empty.setObjectName("AssistEmptyState")
		self.empty.setAlignment(Qt.AlignCenter)
		self.empty.setWordWrap(True)
		self.layout.insertWidget(0, self.empty)

		root.addWidget(title)
		root.addWidget(self.scroll, 1)
		self._bubbles: list[ChatMessageBubble] = []
		self._streaming_chunks: list[str] = []
		# Throttles scroll-to-bottom: a burst of streamed chunks coalesces into
		# one scroll per tick instead of queuing fresh timers on every token.
		self._scroll_timer = QTimer(self)
		self._scroll_timer.setSingleShot(True)
		self._scroll_timer.setInterval(55)
		self._scroll_timer.timeout.connect(self._scroll_to_bottom)
		# Throttles bubble re-flow during a window drag-resize: re-measuring
		# every bubble on every resize frame is wasted work, so a burst of
		# resize events coalesces into one re-flow per tick.
		self._resize_timer = QTimer(self)
		self._resize_timer.setSingleShot(True)
		self._resize_timer.setInterval(60)
		self._resize_timer.timeout.connect(self._update_bubble_widths)
		self._apply_bubble_font()

	# -- chat answer text zoom (Ctrl +/-) --------------------------------

	def _apply_bubble_font(self) -> None:
		"""Push the current bubble font size onto this panel.

		A stylesheet set on the panel itself wins over the window-level
		`QTextBrowser#AssistBubbleText` rule for every bubble beneath it, so
		zoom only has to touch this one selector. Bubble heights change with the
		font, so widths/scroll are refreshed on the next ticks.
		"""
		self.setStyleSheet(
			f"QTextBrowser#AssistBubbleText {{ font-size: {self._bubble_font_pt}px; }}"
		)
		for delay in (0, 30):
			QTimer.singleShot(delay, self._update_bubble_widths)
		self.schedule_scroll_to_bottom()

	def zoom_chat_text(self, step: int) -> None:
		"""Grow/shrink the chat answer text by ``step`` points, clamped."""
		new_pt = max(self._MIN_FONT_PT, min(self._MAX_FONT_PT, self._bubble_font_pt + step))
		if new_pt == self._bubble_font_pt:
			return
		self._bubble_font_pt = new_pt
		self._apply_bubble_font()

	def reset_chat_zoom(self) -> None:
		if self._bubble_font_pt == self._BASE_FONT_PT:
			return
		self._bubble_font_pt = self._BASE_FONT_PT
		self._apply_bubble_font()

	def add_message(
		self,
		sender: str,
		message: str,
		is_user: bool,
		*,
		rendered_html: str | None = None,
	) -> ChatMessageBubble:
		self.empty.hide()
		bubble = ChatMessageBubble(sender, message, is_user, rendered_html=rendered_html)
		bubble.set_bubble_width(self._bubble_width())
		self._bubbles.append(bubble)

		row = QHBoxLayout()
		row.setContentsMargins(0, 0, 0, 0)
		# Both user and assistant bubbles span the full panel width so the chat
		# fills the screen horizontally; sender is distinguished by colour.
		row.addWidget(bubble, 1)

		insert_at = max(0, self.layout.count() - 1)
		self.layout.insertLayout(insert_at, row)
		self.schedule_scroll_to_bottom()
		return bubble

	def start_streaming_assistant(self, sender: str = "VERITAS") -> ChatMessageBubble:
		# Empty body + animated typing dots until the first token streams in.
		bubble = self.add_message(sender, "", False)
		bubble.set_typing(True)
		self._streaming_bubble = bubble
		self._streaming_chunks = []
		return bubble

	def _live_streaming_bubble(self) -> ChatMessageBubble | None:
		"""Return the streaming bubble only if its C++ object still exists.

		``clear_messages()`` / workspace switches can delete the bubble while a
		streamed answer is still arriving; a stale reference here would crash
		on the next chunk.
		"""
		bubble = getattr(self, "_streaming_bubble", None)
		if bubble is None or not _qt_is_alive(bubble):
			self._streaming_bubble = None
			return None
		return bubble

	def append_streaming_chunk(self, chunk: str) -> None:
		bubble = self._live_streaming_bubble()
		if bubble is None:
			return
		self._streaming_chunks.append(chunk)
		bubble.append_text(chunk)
		self.schedule_scroll_to_bottom()

	def finalize_streaming_assistant(self, text: str | None = None) -> None:
		bubble = self._live_streaming_bubble()
		if bubble is None:
			self._streaming_bubble = None
			self._streaming_chunks = []
			return
		final_text = text if text is not None else "".join(self._streaming_chunks)
		bubble.set_markdown(final_text or "")
		self._streaming_bubble = None
		self._streaming_chunks = []
		self.schedule_scroll_to_bottom()

	def cancel_streaming_assistant(self, error_text: str) -> None:
		bubble = self._live_streaming_bubble()
		if bubble is None:
			self._streaming_bubble = None
			self._streaming_chunks = []
			return
		current = "".join(self._streaming_chunks)
		display = f"{current}\n\n[오류] {error_text}" if current else f"[오류] {error_text}"
		bubble.set_markdown(display)
		self._streaming_bubble = None
		self._streaming_chunks = []
		self.schedule_scroll_to_bottom()

	def clear_messages(self) -> None:
		# Drop any in-flight streaming reference *first*: the bubble it points
		# at is about to be deleted, and a late chunk must not touch it.
		self._streaming_bubble = None
		self._streaming_chunks = []
		while self.layout.count():
			item = self.layout.takeAt(0)
			self._dispose_layout_item(item)
		self._bubbles.clear()
		self.layout.addWidget(self.empty)
		self.layout.addStretch(1)
		self.empty.show()

	def _dispose_layout_item(self, item) -> None:
		widget = item.widget()
		if widget is not None and widget is not self.empty:
			widget.setParent(None)
			widget.deleteLater()
			return
		layout = item.layout()
		if layout is not None:
			while layout.count():
				self._dispose_layout_item(layout.takeAt(0))

	def schedule_scroll_to_bottom(self) -> None:
		# Throttle: while it is already armed, extra chunks are no-ops, so a
		# burst coalesces into a single scroll instead of three timers per token.
		if not self._scroll_timer.isActive():
			self._scroll_timer.start()

	def _scroll_to_bottom(self) -> None:
		# While a stream is live only the streaming bubble grows, and it keeps
		# its own height current via contentsChanged → _sync_height; re-flowing
		# every other (unchanged) bubble each tick would be wasted work.
		if self._live_streaming_bubble() is None:
			self._update_bubble_widths()
		self.container.adjustSize()
		bar = self.scroll.verticalScrollBar()
		bar.setValue(bar.maximum())

	def force_scroll_bottom(self) -> None:
		"""Pin to the newest message across the next few layout passes.

		A single scroll often lands short because bubble heights settle a tick
		or two later (markdown re-flow / width measurement). This also covers the
		case where the panel just became visible — e.g. the editor's 대화 tab was
		hidden while its history loaded, so the viewport had no width to measure
		against until now.
		"""
		for delay in (0, 60, 160):
			QTimer.singleShot(delay, self._scroll_to_bottom)

	def showEvent(self, event) -> None:  # type: ignore[override]
		super().showEvent(event)
		# Now that the panel has a real viewport, re-flow the bubbles and pin to
		# the latest message so switching to the chat always lands at the bottom.
		self._update_bubble_widths()
		self.force_scroll_bottom()

	def resizeEvent(self, event) -> None:  # type: ignore[override]
		super().resizeEvent(event)
		# Coalesce a drag-resize burst: re-measuring every bubble's wrapped
		# height on every resize frame is wasted work. The throttle still
		# re-flows roughly every 60ms during the drag and once when it settles,
		# so the answers keep tracking the window width without the per-frame
		# cost over the whole bubble list.
		if not self._resize_timer.isActive():
			self._resize_timer.start()

	def _bubble_width(self) -> int:
		viewport_width = max(280, self.scroll.viewport().width())
		return max(220, viewport_width - 2)

	def _update_bubble_widths(self) -> None:
		width = self._bubble_width()
		for bubble in self._bubbles:
			bubble.set_bubble_width(width)


class ChatInputBar(QFrame):
	sendRequested = Signal(str)
	modeChanged = Signal(str)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("AssistInputBar")
		# RAG ("채팅") is the default chat mode; "조사" (research) is the opt-in
		# mode. The mode button shows the active one and toggles between them.
		self._mode = "rag"

		layout = QHBoxLayout(self)
		layout.setContentsMargins(10, 8, 10, 8)
		layout.setSpacing(8)

		# A rounded-rectangle toggle: it shows the active mode ("채팅" by default)
		# and clicking it flips straight to "조사" — research mode — turning navy
		# to signal it is on.
		self.mode_button = QPushButton("채팅")
		self.mode_button.setObjectName("AssistModeButton")
		self.mode_button.setCursor(Qt.PointingHandCursor)
		self.mode_button.setFixedSize(60, 46)
		self.mode_button.clicked.connect(self._toggle_mode)

		self.input = ChatInputEdit()
		self.input.setObjectName("AssistChatInput")
		self.input.setPlaceholderText("문서에 대해 질문하기...")
		self.input.setFixedHeight(46)
		self.input.sendRequested.connect(self._emit_send)

		self.send_button = QPushButton("전송")
		self.send_button.setObjectName("AssistSendButton")
		self.send_button.setCursor(Qt.PointingHandCursor)
		self.send_button.setFixedSize(68, 46)
		self.send_button.clicked.connect(self._emit_send)

		layout.addWidget(self.mode_button)
		layout.addWidget(self.input, 1)
		layout.addWidget(self.send_button)
		self.set_mode(self._mode, emit=False)

	def _toggle_mode(self) -> None:
		"""Flip between the default RAG ("채팅") and research ("조사") modes."""
		self.set_mode("rag" if self._mode == "research" else "research")

	def set_mode(self, mode: str, emit: bool = True) -> None:
		self._mode = "research" if mode == "research" else "rag"
		is_research = self._mode == "research"
		# The label doubles as the state read-out; the navy "active" styling is
		# driven by a dynamic property + repolish.
		self.mode_button.setText("조사" if is_research else "채팅")
		self.mode_button.setProperty("researchActive", is_research)
		self.mode_button.style().unpolish(self.mode_button)
		self.mode_button.style().polish(self.mode_button)
		if is_research:
			self.mode_button.setToolTip("자료조사 모드 사용 중 (눌러서 끄기)")
			self.input.setPlaceholderText("자료조사 모드로 질문하기...")
		else:
			self.mode_button.setToolTip("자료조사 모드 켜기")
			self.input.setPlaceholderText("문서에 대해 질문하기...")
		if emit:
			self.modeChanged.emit(self._mode)

	def mode(self) -> str:
		return self._mode

	def _emit_send(self) -> None:
		message = self.input.toPlainText().strip()
		if not message:
			return
		self.sendRequested.emit(message)
		self.input.clear()


class DocumentAssistWindow(QWidget):
	messageSubmitted = Signal(str)
	visibilityChanged = Signal(bool)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setWindowTitle("Veritas Assist")
		# No Qt.Tool: tool windows cannot be minimised on Windows and get no
		# taskbar button. As a plain top-level (stays-on-top) frameless window the
		# '-' button minimises it independently and it can be restored from the
		# taskbar.
		self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
		# Wider default/min footprint — the previous 560/380 px made the chat
		# answers feel cramped horizontally.
		self.resize(680, 820)
		self.setMinimumSize(470, 580)
		self.setAttribute(Qt.WA_TranslucentBackground, True)
		self.setMouseTracking(True)
		self._resize_margin = 8
		self._resize_edges: set[str] = set()
		self._resize_origin: QPoint | None = None
		self._resize_geometry = None
		self._history_hydrated = False

		self._build_ui()
		self._apply_stylesheet()
		self._install_chat_zoom_shortcuts()
		# screen-monitoring 이벤트 broker 연결 — 보조창과 DocumentAssistPage가 같은 데이터 공유.
		self._screen_store = get_screen_event_store()
		self._screen_store.eventsAppended.connect(self._on_screen_events_appended)
		# 워크스페이스 전환 시 store.clear() → cleared emit → hydrate로 위젯 reset.
		self._screen_store.cleared.connect(self._hydrate_screen_suggestions)
		self._hydrate_screen_suggestions()

	def _install_chat_zoom_shortcuts(self) -> None:
		"""Ctrl +/- (and Ctrl+0) resize the chat answer text.

		`QShortcut` with the default WindowShortcut context fires no matter
		which child widget holds focus — including the chat input — so the
		zoom works while the user is typing.
		"""
		bindings = (
			("Ctrl++", lambda: self.chat_panel.zoom_chat_text(1)),
			("Ctrl+=", lambda: self.chat_panel.zoom_chat_text(1)),
			("Ctrl+-", lambda: self.chat_panel.zoom_chat_text(-1)),
			("Ctrl+0", self.chat_panel.reset_chat_zoom),
		)
		self._chat_zoom_shortcuts: list[QShortcut] = []
		for sequence, handler in bindings:
			shortcut = QShortcut(QKeySequence(sequence), self)
			shortcut.activated.connect(handler)
			self._chat_zoom_shortcuts.append(shortcut)

	def _build_ui(self) -> None:
		root = QVBoxLayout(self)
		root.setContentsMargins(10, 10, 10, 10)
		root.setSpacing(0)

		self.panel = QFrame()
		self.panel.setObjectName("AssistPanel")
		shadow = QGraphicsDropShadowEffect(self.panel)
		shadow.setBlurRadius(28)
		shadow.setXOffset(0)
		shadow.setYOffset(10)
		shadow.setColor(QColor(15, 23, 42, 45))
		self.panel.setGraphicsEffect(shadow)

		panel_layout = QVBoxLayout(self.panel)
		panel_layout.setContentsMargins(0, 0, 0, 0)
		panel_layout.setSpacing(0)

		self.title_bar = VeritasTitleBar(self, subtitle=True, status=True, on_close=self.hide)

		content = QFrame()
		content.setObjectName("AssistContent")
		content_layout = QVBoxLayout(content)
		content_layout.setContentsMargins(12, 10, 12, 8)
		content_layout.setSpacing(10)

		# A draggable divider lets the user trade height between the suggestion
		# list and the chat, so neither caps the other at a fixed ratio.
		self.suggestion_list = SuggestionList()
		self.suggestion_list.setMinimumHeight(120)
		self.chat_panel = ChatPanel()
		self.chat_panel.setMinimumHeight(160)
		self.input_bar = ChatInputBar()
		self.input_bar.sendRequested.connect(self.on_message_submitted)
		self.input_bar.modeChanged.connect(self._on_mode_changed)

		self.content_split = QSplitter(Qt.Vertical)
		self.content_split.setObjectName("AssistContentSplit")
		self.content_split.setChildrenCollapsible(False)
		self.content_split.setHandleWidth(10)
		self.content_split.addWidget(self.suggestion_list)
		self.content_split.addWidget(self.chat_panel)
		self.content_split.setStretchFactor(0, 0)
		self.content_split.setStretchFactor(1, 1)
		self.content_split.setSizes([220, 380])

		content_layout.addWidget(self.content_split, 1)
		content_layout.addWidget(self.input_bar)

		grip_row = QHBoxLayout()
		grip_row.setContentsMargins(0, 0, 0, 0)
		grip_row.addStretch(1)
		grip_row.addWidget(QSizeGrip(self), 0, Qt.AlignRight | Qt.AlignBottom)
		content_layout.addLayout(grip_row)

		panel_layout.addWidget(self.title_bar)
		panel_layout.addWidget(content, 1)
		root.addWidget(self.panel)

	def _hydrate_screen_suggestions(self) -> None:
		"""store history 전체로 suggestion_list 재구성. show 시점마다 호출되어 양쪽 위젯 동기화 보장."""
		history = self._screen_store.get_history()
		suggestions: list[dict[str, str]] = []
		for item in history:
			formatted = format_screen_event(item)
			if formatted is None:
				continue
			category, text, tone = formatted
			suggestions.append({"id": str(item.get("eventId") or ""), "category": category, "text": text, "tone": tone})
		self.suggestion_list.set_suggestions(suggestions)

	def _on_screen_events_appended(self, items: list) -> None:
		"""실시간 append — 보이는 동안만 add_suggestion, 숨김 동안은 다음 hydrate에서 일괄 보충."""
		if not self.isVisible():
			return
		for item in items:
			if not isinstance(item, dict):
				continue
			formatted = format_screen_event(item)
			if formatted is None:
				continue
			category, text, tone = formatted
			self.suggestion_list.upsert_suggestion(str(item.get("eventId") or ""), category, text, tone)

	def _apply_stylesheet(self) -> None:
		self.setStyleSheet(
			"""
			QWidget {
				color: #111827;
				font-family: 'Segoe UI Variable', 'Segoe UI', 'Malgun Gothic', 'Noto Sans KR', sans-serif;
				font-size: 13px;
			}
			QFrame#AssistPanel {
				background-color: #F8FAFC;
				border: 1px solid #E5E7EB;
				border-radius: 16px;
			}
			QFrame#AssistTitleBar {
				background-color: #FFFFFF;
				border-top-left-radius: 16px;
				border-top-right-radius: 16px;
				border-bottom: 1px solid #E5E7EB;
			}
			QFrame#AssistContent {
				background-color: #F8FAFC;
				border-bottom-left-radius: 16px;
				border-bottom-right-radius: 16px;
			}
			QSplitter#AssistContentSplit {
				background-color: transparent;
			}
			QSplitter#AssistContentSplit::handle:vertical {
				background-color: #E5E7EB;
				height: 3px;
				margin: 3px 40px;
				border-radius: 1px;
			}
			QSplitter#AssistContentSplit::handle:vertical:hover {
				background-color: #94A3B8;
			}
			QLabel#AssistWindowTitle {
				color: #111827;
				font-size: 13px;
				font-weight: 850;
			}
			QLabel#AssistTitleContext {
				color: #6B7280;
				font-size: 11px;
				font-weight: 650;
			}
			QPushButton#AssistMinimizeButton,
			QPushButton#AssistCloseButton {
				background-color: transparent;
				color: #6B7280;
				border: none;
				border-radius: 9px;
				font-size: 16px;
				font-weight: 700;
				padding: 0px;
			}
			QPushButton#AssistMinimizeButton:hover {
				background-color: #F3F4F6;
				color: #111827;
			}
			QPushButton#AssistCloseButton:hover {
				background-color: #FEE2E2;
				color: #DC2626;
			}
			QFrame#AssistSectionCard {
				background-color: #FFFFFF;
				border: 1px solid #E5E7EB;
				border-radius: 13px;
			}
			QLabel#AssistSubText {
				color: #6B7280;
				font-size: 12px;
				font-weight: 600;
			}
			QLabel#AssistSectionTitle {
				color: #111827;
				font-size: 13px;
				font-weight: 850;
			}
			QScrollArea#AssistScrollArea {
				background-color: transparent;
				border: none;
			}
			QFrame#SuggestionCard {
				background-color: #FFFFFF;
				border: 1px solid #E5E7EB;
				border-radius: 12px;
			}
			QLabel#SuggestionText {
				color: #1F2937;
				font-size: 13px;
				font-weight: 650;
				line-height: 1.5;
			}
			QLabel#AssistEmptyState {
				background-color: #F8FAFC;
				border: 1px dashed #CBD5E1;
				border-radius: 12px;
				color: #6B7280;
				padding: 18px 14px;
				font-weight: 650;
			}
			QPushButton#AssistCopyButton {
				background-color: #FFFFFF;
				color: #4B5563;
				border: 1px solid #D1D5DB;
				border-radius: 8px;
				padding: 5px 8px;
				font-size: 11px;
				font-weight: 800;
			}
			QPushButton#AssistCopyButton:hover {
				background-color: #F3F4F6;
				color: #111827;
			}
			QFrame#AssistUserBubble {
				background-color: #DBEAFE;
				border: 1px solid #BFDBFE;
				border-radius: 13px;
				border-top-right-radius: 4px;
			}
			QFrame#AssistAiBubble {
				background-color: #FFFFFF;
				border: 1px solid #E5E7EB;
				border-radius: 13px;
				border-top-left-radius: 4px;
			}
			QLabel#AssistBubbleMeta {
				color: #6B7280;
				font-size: 11px;
				font-weight: 800;
			}
			QTextBrowser#AssistBubbleText {
				color: #1F2937;
				font-size: 14px;
				font-weight: 600;
				background: transparent;
				border: none;
			}
			QFrame#AssistInputBar {
				background-color: #FFFFFF;
				border: 1px solid #E5E7EB;
				border-radius: 14px;
			}
			QTextEdit#AssistChatInput {
				background-color: #F8FAFC;
				border: 1px solid #E5E7EB;
				border-radius: 11px;
				padding: 8px 10px;
				color: #111827;
				font-size: 13px;
				selection-background-color: #BFDBFE;
				selection-color: #111827;
			}
			QTextEdit#AssistChatInput:focus {
				background-color: #FFFFFF;
				border: 1px solid #3B82F6;
			}
			QPushButton#AssistSendButton {
				background-color: #3B82F6;
				border: 1px solid #2563EB;
				border-radius: 11px;
				color: #FFFFFF;
				font-weight: 850;
			}
			QPushButton#AssistSendButton:hover {
				background-color: #2563EB;
			}
			QPushButton#AssistModeButton {
				background-color: #F1F5F9;
				color: #475569;
				border: 1px solid #D1D5DB;
				border-radius: 11px;
				padding: 0px;
				font-size: 13px;
				font-weight: 800;
			}
			QPushButton#AssistModeButton:hover {
				background-color: #E0E7FF;
				border-color: #818CF8;
				color: #3730A3;
			}
			QPushButton#AssistModeButton[researchActive="true"] {
				background-color: #1E3A8A;
				border-color: #1E3A8A;
				color: #FFFFFF;
			}
			QPushButton#AssistModeButton[researchActive="true"]:hover {
				background-color: #1E40AF;
				border-color: #1E40AF;
				color: #FFFFFF;
			}
			QMenu {
				background-color: #FFFFFF;
				border: 1px solid #CBD5E1;
				border-radius: 8px;
				padding: 6px;
			}
			QMenu::item {
				color: #111827;
				padding: 8px 28px 8px 12px;
				border-radius: 6px;
			}
			QMenu::item:selected {
				background-color: #EEF2FF;
				color: #3730A3;
			}
			QScrollBar:vertical {
				background: transparent;
				width: 8px;
				margin: 2px 0 2px 0;
			}
			QScrollBar::handle:vertical {
				background: #CBD5E1;
				border-radius: 4px;
				min-height: 26px;
			}
			QScrollBar::add-line:vertical,
			QScrollBar::sub-line:vertical {
				height: 0px;
			}
			QScrollBar::add-page:vertical,
			QScrollBar::sub-page:vertical {
				background: transparent;
			}
			"""
			+ VERITAS_TITLEBAR_QSS
			+ CHAT_QSS
		)

	def update_assist_text(self, text: str) -> None:
		self.suggestion_list.set_suggestions([{"category": "수정", "text": text, "tone": "working"}])

	def append_assist_text(self, text: str) -> None:
		self.suggestion_list.add_suggestion("수정", text, "idle")

	def add_chat_message(self, sender: str, message: str) -> None:
		normalized_sender = "나" if sender in {"사용자", "User", "user", "나"} else sender
		is_user = normalized_sender == "나"
		self.chat_panel.add_message(normalized_sender, message, is_user)

	def hydrate_history(self, history: list[dict]) -> None:
		"""Replace the chat panel with the canonical workspace history so the
		assist window mirrors the main chat page.

		``history`` is a list of pre-rendered bubble specs from
		:func:`render_history_html` — the per-message markdown parse already
		happened on a worker thread, so this just builds bubbles.
		"""
		self.chat_panel.clear_messages()
		if not history:
			self.chat_panel.add_message(
				"VERITAS",
				"문서에 대해 질문하면 이 대화가 메인 채팅 창과 동기화됩니다.",
				False,
			)
			self._history_hydrated = True
			return
		for spec in history:
			if not isinstance(spec, dict):
				continue
			text = str(spec.get("text") or "")
			if not text:
				continue
			is_user = bool(spec.get("is_user"))
			sender = "나" if is_user else "VERITAS"
			self.chat_panel.add_message(
				sender, text, is_user, rendered_html=spec.get("html") or None
			)
		self._history_hydrated = True

	def get_current_chat_input(self) -> str:
		return self.input_bar.input.toPlainText().strip()

	def clear_chat_input(self) -> None:
		self.input_bar.input.clear()

	def _on_mode_changed(self, mode: str) -> None:
		# RAG is the implicit default, so it gets no badge; only the opt-in
		# 자료조사 mode is surfaced. Switching back to RAG restores the
		# default status text.
		if mode == "research":
			self.title_bar.status.setText("자료조사")
		else:
			self.title_bar.status.setText("● 분석 중")

	def on_message_submitted(self, message: str) -> None:
		# The user bubble is drawn by ChatBus subscribers (MainWindow + WritePage)
		# so it appears in both views at the same time.
		self.messageSubmitted.emit(message)

	def on_send_clicked(self) -> None:
		self.input_bar._emit_send()

	def closeEvent(self, event: QCloseEvent) -> None:
		event.ignore()
		self.hide()

	def hideEvent(self, event) -> None:  # type: ignore[override]
		super().hideEvent(event)
		self.visibilityChanged.emit(False)

	def showEvent(self, event) -> None:  # type: ignore[override]
		super().showEvent(event)
		self._hydrate_screen_suggestions()
		self.visibilityChanged.emit(True)

	def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
		# Ctrl + wheel mirrors the Ctrl +/- shortcut for chat answer zoom.
		if event.modifiers() & Qt.ControlModifier:
			delta = event.angleDelta().y()
			if delta > 0:
				self.chat_panel.zoom_chat_text(1)
			elif delta < 0:
				self.chat_panel.zoom_chat_text(-1)
			event.accept()
			return
		super().wheelEvent(event)

	def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
		if event.button() == Qt.LeftButton:
			edges = self._hit_resize_edges(event.position().toPoint())
			if edges:
				self._resize_edges = edges
				self._resize_origin = event.globalPosition().toPoint()
				self._resize_geometry = self.geometry()
				event.accept()
				return
		super().mousePressEvent(event)

	def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
		if self._resize_origin is not None and self._resize_geometry is not None:
			self._resize_to(event.globalPosition().toPoint())
			event.accept()
			return

		self._update_resize_cursor(event.position().toPoint())
		super().mouseMoveEvent(event)

	def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
		self._resize_edges = set()
		self._resize_origin = None
		self._resize_geometry = None
		self.unsetCursor()
		super().mouseReleaseEvent(event)

	def leaveEvent(self, event) -> None:  # type: ignore[override]
		if self._resize_origin is None:
			self.unsetCursor()
		super().leaveEvent(event)

	def _hit_resize_edges(self, pos: QPoint) -> set[str]:
		margin = self._resize_margin
		edges: set[str] = set()
		if pos.x() <= margin:
			edges.add("left")
		elif pos.x() >= self.width() - margin:
			edges.add("right")
		if pos.y() <= margin:
			edges.add("top")
		elif pos.y() >= self.height() - margin:
			edges.add("bottom")
		return edges

	def _update_resize_cursor(self, pos: QPoint) -> None:
		edges = self._hit_resize_edges(pos)
		if {"left", "top"} <= edges or {"right", "bottom"} <= edges:
			self.setCursor(Qt.SizeFDiagCursor)
		elif {"right", "top"} <= edges or {"left", "bottom"} <= edges:
			self.setCursor(Qt.SizeBDiagCursor)
		elif "left" in edges or "right" in edges:
			self.setCursor(Qt.SizeHorCursor)
		elif "top" in edges or "bottom" in edges:
			self.setCursor(Qt.SizeVerCursor)
		else:
			self.unsetCursor()

	def _resize_to(self, global_pos: QPoint) -> None:
		if self._resize_origin is None or self._resize_geometry is None:
			return

		delta = global_pos - self._resize_origin
		geometry = self._resize_geometry
		x = geometry.x()
		y = geometry.y()
		width = geometry.width()
		height = geometry.height()
		min_width = self.minimumWidth()
		min_height = self.minimumHeight()

		if "left" in self._resize_edges:
			new_width = max(min_width, width - delta.x())
			x = geometry.right() - new_width + 1
			width = new_width
		if "right" in self._resize_edges:
			width = max(min_width, width + delta.x())
		if "top" in self._resize_edges:
			new_height = max(min_height, height - delta.y())
			y = geometry.bottom() - new_height + 1
			height = new_height
		if "bottom" in self._resize_edges:
			height = max(min_height, height + delta.y())

		self.setGeometry(x, y, width, height)


if __name__ == "__main__":
	app = QApplication([])
	window = DocumentAssistWindow()
	window.show()
	app.exec()
