from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout, QWidget

from ...api_common import ApiError, current_workspace_id
from ...components.buttons import AppButton
from ...components.cards import CardWidget
from ...controllers import AgentController


class DraftPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._workspace_id = current_workspace_id()
		self._controller = AgentController()

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(12)

		header = CardWidget("초안 생성")
		subtitle = QLabel("요청을 입력하면 backend agent가 초안을 생성합니다.")
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		header.layout.addWidget(subtitle)

		self.workspace_label = QLabel(f"현재 워크스페이스: {self._workspace_id}")
		self.workspace_label.setObjectName("DraftWorkspaceSummary")
		self.workspace_label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
		header.layout.addWidget(self.workspace_label)
		root.addWidget(header)

		input_card = CardWidget("요청 입력")
		input_hint = QLabel("작성할 초안의 목적, 독자, 형식, 포함할 내용을 입력하세요.")
		input_hint.setObjectName("PageSubtitle")
		input_hint.setWordWrap(True)

		self.prompt_edit = QPlainTextEdit()
		self.prompt_edit.setPlaceholderText("예: 고객 보고서 3문단 초안을 작성해줘.")
		self.prompt_edit.setMinimumHeight(160)
		self.prompt_edit.setObjectName("DraftPrompt")

		action_row = QHBoxLayout()
		action_row.addStretch(1)

		self.generate_button = AppButton("초안 생성")
		self.generate_button.clicked.connect(self._generate_draft)
		action_row.addWidget(self.generate_button)

		input_card.layout.addWidget(input_hint)
		input_card.layout.addWidget(self.prompt_edit)
		input_card.layout.addLayout(action_row)
		root.addWidget(input_card)

		output_card = CardWidget("초안 결과")
		output_hint = QLabel("agent가 반환한 실제 응답이 표시됩니다.")
		output_hint.setObjectName("PageSubtitle")
		output_hint.setWordWrap(True)

		self.output = QPlainTextEdit()
		self.output.setReadOnly(True)
		self.output.setMinimumHeight(320)
		self.output.setObjectName("DraftOutput")

		output_actions = QHBoxLayout()
		output_actions.addStretch(1)

		copy_button = AppButton("초안 복사", variant="ghost")
		copy_button.clicked.connect(self._copy_output)
		output_actions.addWidget(copy_button)

		output_card.layout.addWidget(output_hint)
		output_card.layout.addWidget(self.output)
		output_card.layout.addLayout(output_actions)
		root.addWidget(output_card, 1)

	def set_workspace_by_name(self, workspace_name: str) -> None:
		self.workspace_label.setText(f"현재 워크스페이스: {workspace_name or self._workspace_id}")

	def _copy_output(self) -> None:
		self.output.selectAll()
		self.output.copy()
		cursor = self.output.textCursor()
		cursor.clearSelection()
		self.output.setTextCursor(cursor)

	def _generate_draft(self) -> None:
		prompt = self.prompt_edit.toPlainText().strip()
		if not prompt:
			self.output.setPlainText("초안 생성을 위해 요청을 입력하세요.")
			return

		self.generate_button.setEnabled(False)
		self.output.setPlainText("agent가 초안을 생성하는 중입니다...")
		try:
			response = self._controller.generate_draft(self._workspace_id, prompt)
			self.output.setPlainText(str(response.get("content") or ""))
		except ApiError as e:
			self.output.setPlainText(f"API 요청 실패: {e}")
		finally:
			self.generate_button.setEnabled(True)
