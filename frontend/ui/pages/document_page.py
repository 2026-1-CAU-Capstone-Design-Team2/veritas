from __future__ import annotations

from PySide6.QtWidgets import QTextEdit, QVBoxLayout, QWidget

from ...api_common import ApiError, current_workspace_id
from ...components.badges import Badge
from ...components.cards import CardWidget
from ...controllers import AgentController
from ..markdown_view import apply_markdown


class DocumentPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._workspace_id = current_workspace_id()
		self._controller = AgentController()

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(12)

		summary_card = CardWidget("요약")

		summary_badge = Badge("요약본", "info")
		summary_card.layout.addWidget(summary_badge)

		self.summary_text = QTextEdit()
		self.summary_text.setObjectName("DocEditor")
		self.summary_text.setReadOnly(True)
		self.summary_text.setMinimumHeight(360)
		# Stretch the editor inside the card, and the card across the page, so
		# the summary fills the whole screen.
		summary_card.layout.addWidget(self.summary_text, 1)
		root.addWidget(summary_card, 1)

		self.refresh()

	def refresh(self) -> None:
		self._workspace_id = current_workspace_id()
		try:
			summary = self._controller.get_document_summary(self._workspace_id)
		except ApiError as e:
			self.summary_text.setPlainText(f"API 요청 실패: {e}")
			return

		if summary.strip():
			apply_markdown(self.summary_text, summary)
		else:
			self.summary_text.setPlainText(
				"아직 표시할 final.md가 없습니다. 조사 섹션에서 AutoSurvey를 먼저 실행하세요."
			)
