from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
	QButtonGroup,
	QFrame,
	QHBoxLayout,
	QLabel,
	QPlainTextEdit,
	QScrollArea,
	QSizePolicy,
	QVBoxLayout,
	QWidget,
)

from ...components.buttons import AppButton


class ComposerEdit(QPlainTextEdit):
	sendRequested = Signal()

	def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
		if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ControlModifier:
			self.sendRequested.emit()
			return

		super().keyPressEvent(event)


class WriteChatBubble(QFrame):
	def __init__(self, text: str, is_user: bool, max_width: int, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("UserBubble" if is_user else "AIBubble")
		self.setMaximumWidth(max_width)
		self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)

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
		self._assistant_max_line_chars = 160
		self._mode = "research"
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

		self.mode_group = QButtonGroup(self)
		self.mode_group.setExclusive(True)

		self.research_mode_btn = AppButton("자료조사", variant="filter")
		self.research_mode_btn.setCheckable(True)
		self.research_mode_btn.setChecked(True)
		self.research_mode_btn.clicked.connect(lambda: self._set_mode("research"))

		self.rag_mode_btn = AppButton("RAG", variant="filter")
		self.rag_mode_btn.setCheckable(True)
		self.rag_mode_btn.clicked.connect(lambda: self._set_mode("rag"))

		self.mode_group.addButton(self.research_mode_btn)
		self.mode_group.addButton(self.rag_mode_btn)

		send_btn = AppButton("전송", variant="send")
		send_btn.clicked.connect(self._send_message)

		button_row.addWidget(self.research_mode_btn)
		button_row.addWidget(self.rag_mode_btn)
		button_row.addStretch(1)
		button_row.addWidget(send_btn)

		composer_layout.addWidget(self.input)
		composer_layout.addLayout(button_row)

		chat_panel_layout.addWidget(self.chat_scroll, 1)
		chat_panel_layout.addWidget(composer_card)
		root.addWidget(chat_panel, 1)

		self._append_message(self._welcome_message, is_user=False)

	def _append_message(self, text: str, is_user: bool) -> None:
		max_width = 1520 if is_user else 1800
		bubble = WriteChatBubble(self._wrap_message_text(text, is_user), is_user, max_width)
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

	def _wrap_message_text(self, text: str, is_user: bool) -> str:
		lines = text.splitlines()
		if not lines:
			return text

		max_line_chars = self._max_line_chars if is_user else self._assistant_max_line_chars
		wrapped: list[str] = []
		for line in lines:
			if not line:
				wrapped.append("")
				continue

			start = 0
			while start < len(line):
				wrapped.append(line[start : start + max_line_chars])
				start += max_line_chars

		return "\n".join(wrapped)

	def _scroll_to_bottom(self) -> None:
		bar = self.chat_scroll.verticalScrollBar()
		bar.setValue(bar.maximum())

	def _set_mode(self, mode: str) -> None:
		self._mode = mode
		if mode == "rag":
			self.input.setPlaceholderText("RAG 모드로 질문하세요")
		else:
			self.input.setPlaceholderText("자료조사 모드로 질문하세요")

	def _assistant_reply(self, text: str) -> str:
		lowered = text.lower()
		if self._mode == "rag":
			if "보고" in lowered or "브리프" in lowered:
				return "RAG 모드: 현재 워크스페이스에 저장된 요약본과 스크랩 합본을 기준으로 개요, 핵심 리스크, 실행 권고 순서로 답변하겠습니다."
			if "출처" in lowered or "근거" in lowered:
				return "RAG 모드: 저장된 문서 근거를 우선 확인하고, 답변에 사용할 근거 문장과 검토가 필요한 부분을 함께 정리하겠습니다."
			return "RAG 모드: 선택한 워크스페이스의 기존 문서와 검증 결과를 바탕으로 답변하겠습니다."

		if "보고" in lowered or "브리프" in lowered:
			return "자료조사 모드: 새로 조사할 쟁점과 확인할 출처 후보를 먼저 정리한 뒤, 경영진 보고용 구조로 답변하겠습니다."
		if "메일" in lowered or "안내" in lowered or "공지" in lowered:
			return "자료조사 모드: 필요한 배경 자료와 확인 포인트를 정리한 뒤, 수신자 중심의 짧고 명확한 문장으로 초안을 작성하겠습니다."
		if "3문단" in lowered or "문단" in lowered or "초안" in lowered:
			return "자료조사 모드: 요청한 길이에 맞춰 추가 조사가 필요한 항목과 바로 쓸 수 있는 초안 구조를 함께 제안하겠습니다."
		return "자료조사 모드: 질문에 맞는 조사 방향, 확인할 레퍼런스, 결과 정리 방식을 제안하겠습니다."

	def _send_message(self) -> None:
		raw_text = self.input.toPlainText()
		if not raw_text.strip():
			return

		text = raw_text.rstrip("\n")

		self.input.clear()
		self._append_message(text, is_user=True)
		self._append_message(self._assistant_reply(text), is_user=False)
