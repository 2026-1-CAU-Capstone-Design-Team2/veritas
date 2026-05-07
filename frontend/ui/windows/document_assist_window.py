from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
	QHBoxLayout,
	QLabel,
	QPushButton,
	QTextEdit,
	QVBoxLayout,
	QWidget,
)


class ChatInputEdit(QTextEdit):
	sendRequested = Signal()

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setAcceptRichText(False)

	def keyPressEvent(self, event: QKeyEvent) -> None:
		if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
			event.accept()
			self.sendRequested.emit()
			return
		super().keyPressEvent(event)


class DocumentAssistWindow(QWidget):
	messageSubmitted = Signal(str)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setWindowTitle("AI 문서 작성 보조")
		self.setWindowFlags(Qt.Window | Qt.Tool | Qt.WindowStaysOnTopHint)
		self.resize(360, 520)
		self.setMinimumSize(320, 440)

		self._build_ui()
		self._apply_stylesheet()

	def _build_ui(self) -> None:
		root = QVBoxLayout(self)
		root.setContentsMargins(10, 10, 10, 10)
		root.setSpacing(8)

		header = QLabel("AI 문서 작성 보조")
		header.setObjectName("AssistHeader")
		root.addWidget(header)

		assist_title = QLabel("실시간 문서 작성 보조")
		assist_title.setObjectName("SectionTitle")
		root.addWidget(assist_title)

		self.assist_text_edit = QTextEdit()
		self.assist_text_edit.setObjectName("AssistTextEdit")
		self.assist_text_edit.setReadOnly(True)
		self.assist_text_edit.setPlaceholderText("분석 결과, 추천 문장, 경고, 수정 제안이 여기에 표시됩니다.")
		root.addWidget(self.assist_text_edit, 1)

		chat_title = QLabel("문서 채팅")
		chat_title.setObjectName("SectionTitle")
		root.addWidget(chat_title)

		self.chat_log_edit = QTextEdit()
		self.chat_log_edit.setObjectName("ChatLogEdit")
		self.chat_log_edit.setReadOnly(True)
		self.chat_log_edit.setPlaceholderText("질문과 답변 로그가 여기에 표시됩니다.")
		root.addWidget(self.chat_log_edit, 1)

		input_row = QHBoxLayout()
		input_row.setContentsMargins(0, 0, 0, 0)
		input_row.setSpacing(6)

		self.chat_input_edit = ChatInputEdit()
		self.chat_input_edit.setObjectName("ChatInputEdit")
		self.chat_input_edit.setPlaceholderText("질문을 입력하세요... (Enter: 전송, Shift+Enter: 줄바꿈)")
		self.chat_input_edit.setFixedHeight(44)
		self.chat_input_edit.sendRequested.connect(self.on_send_clicked)

		self.send_button = QPushButton("전송")
		self.send_button.setObjectName("SendButton")
		self.send_button.setCursor(Qt.PointingHandCursor)
		self.send_button.clicked.connect(self.on_send_clicked)

		input_row.addWidget(self.chat_input_edit, 1)
		input_row.addWidget(self.send_button)
		root.addLayout(input_row)

	def _apply_stylesheet(self) -> None:
		self.setStyleSheet(
			"""
			QWidget {
				background-color: #F5F5F5;
				color: #333333;
				font-family: 'Segoe UI Variable', 'Segoe UI', 'Malgun Gothic', 'Noto Sans KR', sans-serif;
				font-size: 12px;
			}
			QLabel#AssistHeader {
				font-size: 14px;
				font-weight: 600;
				color: #333333;
				padding: 2px 2px 4px 2px;
			}
			QLabel#SectionTitle {
				font-size: 11px;
				font-weight: 600;
				color: #5A9EFF;
			}
			QTextEdit#AssistTextEdit,
			QTextEdit#ChatLogEdit,
			QTextEdit#ChatInputEdit {
				background-color: #FFFFFF;
				border: 1px solid #E0E0E0;
				border-radius: 6px;
				padding: 8px;
				color: #333333;
				selection-background-color: #E3F2FD;
				selection-color: #333333;
			}
			QPushButton#SendButton {
				background-color: #5A9EFF;
				border: none;
				border-radius: 6px;
				padding: 8px 14px;
				font-weight: 600;
				color: #FFFFFF;
				min-width: 50px;
			}
			QPushButton#SendButton:hover {
				background-color: #3A8DE6;
			}
			"""
		)

	def update_assist_text(self, text: str) -> None:
		self.assist_text_edit.setPlainText(text)

	def append_assist_text(self, text: str) -> None:
		if self.assist_text_edit.toPlainText():
			self.assist_text_edit.append("")
		self.assist_text_edit.append(text)

	def add_chat_message(self, sender: str, message: str) -> None:
		self.chat_log_edit.append(f"[{sender}] {message}")
		self.chat_log_edit.verticalScrollBar().setValue(self.chat_log_edit.verticalScrollBar().maximum())

	def get_current_chat_input(self) -> str:
		return self.chat_input_edit.toPlainText().strip()

	def clear_chat_input(self) -> None:
		self.chat_input_edit.clear()

	def on_send_clicked(self) -> None:
		message = self.get_current_chat_input()
		if not message:
			return

		self.add_chat_message("사용자", message)
		self.messageSubmitted.emit(message)
		self.clear_chat_input()

		# TODO: BEserver/LLMserver 응답 연결 시 이 더미 응답 로직을 교체하세요.
		self.add_chat_message("VERITAS", "현재는 더미 응답입니다. 추후 서버 연동이 연결되면 실제 답변이 표시됩니다.")

	def closeEvent(self, event: QCloseEvent) -> None:
		# 메인 앱 종료를 유발하지 않고 보조 창만 숨깁니다.
		event.ignore()
		self.hide()


if __name__ == "__main__":
	from PySide6.QtWidgets import QApplication

	app = QApplication([])
	window = DocumentAssistWindow()
	window.show()

	window.update_assist_text("- 문서 분석 결과: 서론이 다소 길고 핵심 주장 위치가 모호합니다.")
	window.append_assist_text("- 추천: 2문단 첫 문장을 결론형으로 재작성해보세요.")

	app.exec()
