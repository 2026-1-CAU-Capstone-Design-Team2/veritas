from __future__ import annotations

import re

from PySide6.QtCore import QPoint, Qt, Signal, QTimer
from PySide6.QtGui import QAction, QColor, QCloseEvent, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
	QApplication,
	QFrame,
	QGraphicsDropShadowEffect,
	QHBoxLayout,
	QLabel,
	QMenu,
	QPushButton,
	QScrollArea,
	QSizeGrip,
	QSizePolicy,
	QTextEdit,
	QToolButton,
	QVBoxLayout,
	QWidget,
)


def add_text_breakpoints(text: str, chunk_size: int = 24) -> str:
	def break_long_token(match: re.Match[str]) -> str:
		token = match.group(0)
		return "\u200b".join(token[index : index + chunk_size] for index in range(0, len(token), chunk_size))

	return re.sub(r"\S{32,}", break_long_token, text)


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


class WindowIconButton(QPushButton):
	def __init__(self, text: str, role: str, parent: QWidget | None = None) -> None:
		super().__init__(text, parent)
		self.setObjectName("AssistCloseButton" if role == "close" else "AssistMinimizeButton")
		self.setFixedSize(32, 32)
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
		self.minimize_button = WindowIconButton("－", "minimize")
		self.close_button = WindowIconButton("×", "close")
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
		copy_button.clicked.connect(lambda: self._copy_text(text))

		header.addWidget(badge, 0, Qt.AlignTop)
		header.addStretch(1)
		header.addWidget(copy_button)

		body = QLabel(add_text_breakpoints(text))
		body.setObjectName("SuggestionText")
		body.setWordWrap(True)
		body.setTextFormat(Qt.PlainText)
		body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
		body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

		layout.addLayout(header)
		layout.addWidget(body)

	def set_card_width(self, width: int) -> None:
		self.setMinimumWidth(width)
		self.setMaximumWidth(width)
		self.updateGeometry()

	def _copy_text(self, text: str) -> None:
		app = QApplication.instance()
		if app is not None:
			app.clipboard().setText(text)


class SuggestionList(QFrame):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("AssistSectionCard")
		self._suggestions: list[dict[str, str]] = []
		self._cards: list[SuggestionCard] = []

		root = QVBoxLayout(self)
		root.setContentsMargins(12, 12, 12, 12)
		root.setSpacing(8)

		header = QHBoxLayout()
		header.setSpacing(8)

		title = QLabel("실시간 수정 결과")
		title.setObjectName("AssistSectionTitle")
		self.count_label = QLabel("0개")
		self.count_label.setObjectName("AssistSubText")

		header.addWidget(title)
		header.addStretch(1)
		header.addWidget(self.count_label)

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
		self.scroll.setWidget(self.container)

		self.empty = QLabel("문서를 작성하면 수정 결과가 여기에 표시됩니다.")
		self.empty.setObjectName("AssistEmptyState")
		self.empty.setAlignment(Qt.AlignCenter)
		self.empty.setWordWrap(True)

		root.addLayout(header)
		root.addWidget(self.scroll, 1)
		self.set_suggestions([])

	def set_suggestions(self, suggestions: list[dict[str, str]]) -> None:
		self._suggestions = [dict(item) for item in suggestions]
		self._clear()
		self._cards = []
		if not self._suggestions:
			self.layout.addWidget(self.empty)
			self.count_label.setText("0개")
			self.layout.addStretch(1)
			self.schedule_width_update()
			return

		for item in self._suggestions:
			card = SuggestionCard(
				item.get("category", "수정"),
				item.get("text", item.get("description", "")),
				item.get("tone", "working"),
			)
			card.set_card_width(self._content_width())
			self._cards.append(card)
			self.layout.addWidget(card, 0)

		self.count_label.setText(f"{len(self._suggestions)}개")
		self.layout.addStretch(1)
		self.schedule_width_update()
		self.schedule_scroll_to_bottom()

	def add_suggestion(self, category: str, text: str, tone: str = "working") -> None:
		items = [dict(item) for item in self._suggestions]
		items.append({"category": category, "text": text, "tone": tone})
		self.set_suggestions(items)

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

	def showEvent(self, event) -> None:  # type: ignore[override]
		super().showEvent(event)
		self.schedule_width_update()

	def _content_width(self) -> int:
		return max(120, self.scroll.viewport().width() - 2)

	def _update_card_widths(self) -> None:
		width = self._content_width()
		self.container.setMinimumWidth(width)
		for card in self._cards:
			card.set_card_width(width)

	def _clear(self) -> None:
		while self.layout.count():
			item = self.layout.takeAt(0)
			widget = item.widget()
			if widget is not None:
				widget.setParent(None)


class ChatMessageBubble(QFrame):
	def __init__(self, sender: str, message: str, is_user: bool, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("AssistUserBubble" if is_user else "AssistAiBubble")
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

		layout = QVBoxLayout(self)
		layout.setContentsMargins(11, 9, 11, 9)
		layout.setSpacing(3)

		meta = QLabel(sender)
		meta.setObjectName("AssistBubbleMeta")
		self.text = QLabel(add_text_breakpoints(message))
		text = self.text
		text.setObjectName("AssistBubbleText")
		text.setWordWrap(True)
		text.setTextFormat(Qt.PlainText)
		text.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
		text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

		layout.addWidget(meta)
		layout.addWidget(text)

	def set_bubble_width(self, max_width: int) -> None:
		self.setMaximumWidth(max_width)
		self.text.setMaximumWidth(max(80, max_width - 22))
		self.text.updateGeometry()


class ChatPanel(QFrame):
	def __init__(self, title_text: str = "문서 채팅", parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("AssistSectionCard")

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

	def add_message(self, sender: str, message: str, is_user: bool) -> None:
		self.empty.hide()
		bubble = ChatMessageBubble(sender, message, is_user)
		bubble.set_bubble_width(self._bubble_width())
		self._bubbles.append(bubble)

		row = QHBoxLayout()
		row.setContentsMargins(0, 0, 0, 0)
		if is_user:
			row.addStretch(1)
			row.addWidget(bubble, 0, Qt.AlignRight)
		else:
			row.addWidget(bubble, 0, Qt.AlignLeft)
			row.addStretch(1)

		insert_at = max(0, self.layout.count() - 1)
		self.layout.insertLayout(insert_at, row)
		self.schedule_scroll_to_bottom()

	def schedule_scroll_to_bottom(self) -> None:
		for delay in (0, 25, 80):
			QTimer.singleShot(delay, self._scroll_to_bottom)

	def _scroll_to_bottom(self) -> None:
		self._update_bubble_widths()
		self.container.adjustSize()
		bar = self.scroll.verticalScrollBar()
		bar.setValue(bar.maximum())

	def resizeEvent(self, event) -> None:  # type: ignore[override]
		super().resizeEvent(event)
		self._update_bubble_widths()

	def _bubble_width(self) -> int:
		viewport_width = max(280, self.scroll.viewport().width())
		return max(220, int(viewport_width * 0.94))

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
		self._mode = "research"

		layout = QHBoxLayout(self)
		layout.setContentsMargins(10, 8, 10, 8)
		layout.setSpacing(8)

		self.mode_button = QToolButton()
		self.mode_button.setObjectName("AssistModeButton")
		self.mode_button.setPopupMode(QToolButton.InstantPopup)
		self.mode_button.setCursor(Qt.PointingHandCursor)
		self.mode_button.setFixedSize(82, 46)

		mode_menu = QMenu(self.mode_button)
		research_action = QAction("자료조사", self)
		research_action.triggered.connect(lambda: self.set_mode("research"))
		rag_action = QAction("RAG", self)
		rag_action.triggered.connect(lambda: self.set_mode("rag"))
		mode_menu.addAction(research_action)
		mode_menu.addAction(rag_action)
		self.mode_button.setMenu(mode_menu)

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

	def set_mode(self, mode: str, emit: bool = True) -> None:
		self._mode = "rag" if mode == "rag" else "research"
		if self._mode == "rag":
			self.mode_button.setText("RAG")
			self.mode_button.setToolTip("RAG 모드")
			self.input.setPlaceholderText("RAG 모드로 질문하기...")
		else:
			self.mode_button.setText("자료조사")
			self.mode_button.setToolTip("자료조사 모드")
			self.input.setPlaceholderText("자료조사 모드로 질문하기...")
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

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setWindowTitle("Veritas Assist")
		self.setWindowFlags(Qt.Window | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
		self.resize(560, 760)
		self.setMinimumSize(380, 540)
		self.setAttribute(Qt.WA_TranslucentBackground, True)
		self.setMouseTracking(True)
		self._resize_margin = 8
		self._resize_edges: set[str] = set()
		self._resize_origin: QPoint | None = None
		self._resize_geometry = None

		self._build_ui()
		self._apply_stylesheet()
		self._load_demo_data()

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

		self.title_bar = CustomTitleBar(self)

		content = QFrame()
		content.setObjectName("AssistContent")
		content_layout = QVBoxLayout(content)
		content_layout.setContentsMargins(12, 10, 12, 8)
		content_layout.setSpacing(10)

		self.suggestion_list = SuggestionList()
		self.chat_panel = ChatPanel()
		self.input_bar = ChatInputBar()
		self.input_bar.sendRequested.connect(self.on_message_submitted)
		self.input_bar.modeChanged.connect(self._on_mode_changed)

		content_layout.addWidget(self.suggestion_list, 2)
		content_layout.addWidget(self.chat_panel, 3)
		content_layout.addWidget(self.input_bar)

		grip_row = QHBoxLayout()
		grip_row.setContentsMargins(0, 0, 0, 0)
		grip_row.addStretch(1)
		grip_row.addWidget(QSizeGrip(self), 0, Qt.AlignRight | Qt.AlignBottom)
		content_layout.addLayout(grip_row)

		panel_layout.addWidget(self.title_bar)
		panel_layout.addWidget(content, 1)
		root.addWidget(self.panel)

	def _load_demo_data(self) -> None:
		self.suggestion_list.set_suggestions(
			[
				{
					"category": "수정",
					"text": "본 보고서는 2026년 AI 규제 변화가 기업 운영에 미치는 영향을 분석하고, 우선 대응이 필요한 리스크를 정리합니다.",
					"tone": "working",
				},
				{
					"category": "근거 보강",
					"text": "효율성 개선 효과는 내부 처리 시간 비교 데이터 또는 외부 벤치마크 수치를 함께 제시하면 더 설득력 있습니다.",
					"tone": "warning",
				},
				{
					"category": "추천 문장",
					"text": "따라서 단기적으로는 고위험 활용 사례를 먼저 식별하고, 검증 로그와 출처 관리 체계를 함께 정비해야 합니다.",
					"tone": "idle",
				},
			]
		)
		self.chat_panel.add_message("VERITAS", "문서 내용에 대해 질문해보세요. 문장 흐름, 근거, 톤을 함께 검토할 수 있습니다.", False)

	def _apply_stylesheet(self) -> None:
		self.setStyleSheet(
			"""
			QWidget {
				color: #111827;
				font-family: 'Segoe UI Variable', 'Segoe UI', 'Malgun Gothic', 'Noto Sans KR', sans-serif;
				font-size: 12px;
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
				font-size: 10px;
				font-weight: 800;
			}
			QLabel#AssistBubbleText {
				color: #1F2937;
				font-size: 12px;
				font-weight: 600;
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
			QToolButton#AssistModeButton {
				background-color: #F8FAFC;
				color: #111827;
				border: 1px solid #D1D5DB;
				border-radius: 11px;
				padding: 0px 8px;
				font-size: 12px;
				font-weight: 850;
			}
			QToolButton#AssistModeButton:hover {
				background-color: #EEF2FF;
				border-color: #818CF8;
				color: #3730A3;
			}
			QToolButton#AssistModeButton::menu-indicator {
				image: none;
				width: 0px;
				height: 0px;
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
		)

	def update_assist_text(self, text: str) -> None:
		self.suggestion_list.set_suggestions([{"category": "수정", "text": text, "tone": "working"}])

	def append_assist_text(self, text: str) -> None:
		self.suggestion_list.add_suggestion("수정", text, "idle")

	def add_chat_message(self, sender: str, message: str) -> None:
		normalized_sender = "나" if sender in {"사용자", "User", "user", "나"} else sender
		is_user = normalized_sender == "나"
		self.chat_panel.add_message(normalized_sender, message, is_user)

	def get_current_chat_input(self) -> str:
		return self.input_bar.input.toPlainText().strip()

	def clear_chat_input(self) -> None:
		self.input_bar.input.clear()

	def _on_mode_changed(self, mode: str) -> None:
		label = "RAG" if mode == "rag" else "자료조사"
		self.title_bar.status.setText(label)

	def on_message_submitted(self, message: str) -> None:
		self.add_chat_message("나", message)
		self.messageSubmitted.emit(message)

	def on_send_clicked(self) -> None:
		self.input_bar._emit_send()

	def closeEvent(self, event: QCloseEvent) -> None:
		event.ignore()
		self.hide()

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
