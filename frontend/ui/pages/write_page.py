from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QScrollArea, QVBoxLayout, QWidget

from ...components.buttons import AppButton


class ComposerEdit(QPlainTextEdit):
	sendRequested = Signal()

	def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
		if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ControlModifier:
			self.sendRequested.emit()
			return

		super().keyPressEvent(event)


class WriteChatBubble(QFrame):
	def __init__(self, text: str, is_user: bool, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("UserBubble" if is_user else "AIBubble")

		layout = QVBoxLayout(self)
		layout.setContentsMargins(14, 11, 14, 11)
		layout.setSpacing(4)

		meta = QLabel("나" if is_user else "VERITAS")
		meta.setObjectName("BubbleMeta")
		meta.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)

		text_label = QLabel(text)
		text_label.setObjectName("BubbleText")
		text_label.setWordWrap(True)
		text_label.setTextFormat(Qt.PlainText)
		text_label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)

		if is_user:
			layout.addWidget(meta, 0, Qt.AlignRight)
			layout.addWidget(text_label)
		else:
			layout.addWidget(meta, 0, Qt.AlignLeft)
			layout.addWidget(text_label)


class WritePage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)

		self._max_line_chars = 34
		self._welcome_message = "메시지를 입력하면 워크스페이스 기반 AI 답변을 제공합니다."

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(8)

		chat_panel = QFrame()
		chat_panel.setObjectName("ChatPanel")
		chat_panel_layout = QVBoxLayout(chat_panel)
		chat_panel_layout.setContentsMargins(12, 12, 12, 12)
		chat_panel_layout.setSpacing(8)

		self.chat_scroll = QScrollArea()
		self.chat_scroll.setObjectName("ChatScroll")
		self.chat_scroll.setWidgetResizable(True)
		self.chat_scroll.setFrameShape(QFrame.NoFrame)

		self.chat_container = QWidget()
		self.chat_layout = QVBoxLayout(self.chat_container)
		self.chat_layout.setContentsMargins(4, 4, 4, 4)
		self.chat_layout.setSpacing(8)
		self.chat_layout.addStretch(1)
		self.chat_scroll.setWidget(self.chat_container)

		composer_card = QFrame()
		composer_card.setObjectName("ComposerCard")
		composer_layout = QVBoxLayout(composer_card)
		composer_layout.setContentsMargins(8, 8, 8, 8)
		composer_layout.setSpacing(6)

		self.input = ComposerEdit()
		self.input.setObjectName("ChatInput")
		self.input.setPlaceholderText("메시지를 입력하세요")
		self.input.setFixedHeight(84)
		self.input.sendRequested.connect(self._send_message)

		button_row = QHBoxLayout()
		button_row.setContentsMargins(0, 0, 0, 0)
		button_row.setSpacing(8)

		send_btn = AppButton("전송", variant="send")
		send_btn.clicked.connect(self._send_message)

		button_row.addStretch(1)
		button_row.addWidget(send_btn)

		composer_layout.addWidget(self.input)
		composer_layout.addLayout(button_row)

		chat_panel_layout.addWidget(self.chat_scroll, 1)
		chat_panel_layout.addWidget(composer_card)
		root.addWidget(chat_panel, 1)

		self._append_message(self._welcome_message, is_user=False)

	def _append_message(self, text: str, is_user: bool) -> None:
		bubble = WriteChatBubble(self._wrap_message_text(text), is_user)
		row = QHBoxLayout()
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(0)

		if is_user:
			row.addStretch(1)
			row.addWidget(bubble, 0, Qt.AlignRight)
		else:
			row.addWidget(bubble, 0, Qt.AlignLeft)
			row.addStretch(1)

		insert_at = max(0, self.chat_layout.count() - 1)
		self.chat_layout.insertLayout(insert_at, row)
		self._scroll_to_bottom()

	def _wrap_message_text(self, text: str) -> str:
		lines = text.splitlines()
		if not lines:
			return text

		wrapped: list[str] = []
		for line in lines:
			if not line:
				wrapped.append("")
				continue

			start = 0
			while start < len(line):
				wrapped.append(line[start : start + self._max_line_chars])
				start += self._max_line_chars

		return "\n".join(wrapped)

	def _scroll_to_bottom(self) -> None:
		bar = self.chat_scroll.verticalScrollBar()
		bar.setValue(bar.maximum())

	def _assistant_reply(self, text: str) -> str:
		lowered = text.lower()
		if "보고" in lowered or "브리프" in lowered:
			return "경영진 보고용으로 개요, 핵심 리스크, 실행 권고 순서의 초안을 정리하겠습니다."
		if "메일" in lowered or "안내" in lowered or "공지" in lowered:
			return "수신자 중심의 짧고 명확한 문장으로 바로 보낼 수 있는 초안을 작성하겠습니다."
		if "3문단" in lowered or "문단" in lowered or "초안" in lowered:
			return "요청한 길이에 맞춰 서론, 본문, 마무리 구조로 다듬겠습니다."
		return "선택한 검증 완료 워크스페이스의 맥락을 반영해 자연스럽게 이어서 작성하겠습니다."

	def _send_message(self) -> None:
		raw_text = self.input.toPlainText()
		if not raw_text.strip():
			return

		text = raw_text.rstrip("\n")

		self.input.clear()
		self._append_message(text, is_user=True)
		self._append_message(self._assistant_reply(text), is_user=False)
