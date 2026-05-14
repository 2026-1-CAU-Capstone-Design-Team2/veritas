from __future__ import annotations

from PySide6.QtWidgets import QTextEdit, QVBoxLayout, QWidget

from ...api_common import current_workspace_id
from ...components.badges import Badge
from ...components.cards import CardWidget
from ...controllers import AgentController, get_job_manager
from ..markdown_view import apply_markdown


class DocumentPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._workspace_id = current_workspace_id()
		self._controller = AgentController()
		# Monotonic guard so an out-of-order summary fetch can't overwrite a
		# newer refresh (rapid page switches / workspace changes).
		self._summary_token = 0

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
		self.summary_text.setPlainText("요약을 불러오는 중입니다...")

		# get_document_summary is a blocking HTTP call — run it off the UI
		# thread so navigating to this page never freezes. The token guards
		# against an out-of-order completion overwriting a newer refresh.
		self._summary_token += 1
		token = self._summary_token
		workspace_id = self._workspace_id
		controller = self._controller

		def _load() -> str:
			return controller.get_document_summary(workspace_id)

		def _apply(summary: object) -> None:
			if token != self._summary_token:
				return
			text = str(summary or "")
			if text.strip():
				apply_markdown(self.summary_text, text)
			else:
				self.summary_text.setPlainText(
					"아직 표시할 final.md가 없습니다. 조사 섹션에서 AutoSurvey를 먼저 실행하세요."
				)

		def _failed(message: str) -> None:
			if token != self._summary_token:
				return
			self.summary_text.setPlainText(f"API 요청 실패: {message}")

		get_job_manager().run_detached(_load, on_success=_apply, on_error=_failed)
