from __future__ import annotations

from PySide6.QtWidgets import QLabel, QTextEdit, QVBoxLayout, QWidget

from ...components.cards import CardWidget


class DocumentAssistPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("DocumentAssistPage")
		self._build_ui()
		self._apply_stylesheet()

	def _build_ui(self) -> None:
		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(12)

		header_card = CardWidget("문서 보조")
		subtitle = QLabel("실시간 보조 내용을 확인하는 전용 화면입니다.")
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		header_card.layout.addWidget(subtitle)
		root.addWidget(header_card)

		assist_card = CardWidget("실시간 문서 보조")
		assist_hint = QLabel("문서 분석 텍스트, 추천 문장, 경고, 수정 제안을 아래 영역에 표시합니다.")
		assist_hint.setObjectName("CardSecondary")
		assist_hint.setWordWrap(True)
		assist_card.layout.addWidget(assist_hint)

		self.assist_text_edit = QTextEdit()
		self.assist_text_edit.setObjectName("AssistTextEdit")
		self.assist_text_edit.setReadOnly(True)
		self.assist_text_edit.setPlaceholderText("분석 결과, 추천 문장, 경고, 수정 제안이 여기에 표시됩니다.")
		self.assist_text_edit.setMinimumHeight(300)
		assist_card.layout.addWidget(self.assist_text_edit)
		root.addWidget(assist_card)

	def _apply_stylesheet(self) -> None:
		self.setStyleSheet(
			"""
			QWidget#DocumentAssistPage {
				background-color: transparent;
				color: #0F172A;
				font-family: 'Segoe UI Variable', 'Segoe UI', 'Malgun Gothic', 'Noto Sans KR', sans-serif;
				font-size: 12px;
			}
			QTextEdit#AssistTextEdit {
				background-color: #FFFFFF;
				border: 1px solid #D7E2F0;
				border-radius: 8px;
				padding: 6px;
				selection-background-color: #BFDBFE;
				selection-color: #0F172A;
			}
			"""
		)

	def update_assist_text(self, text: str) -> None:
		self.assist_text_edit.setPlainText(text)

	def append_assist_text(self, text: str) -> None:
		if self.assist_text_edit.toPlainText():
			self.assist_text_edit.append("")
		self.assist_text_edit.append(text)
