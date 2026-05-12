from __future__ import annotations

from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from ...api_common import ApiError, current_workspace_id
from ...controllers import AgentController
from ..windows.document_assist_window import ChatInputBar, ChatPanel


class WritePage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._mode = "research"
		self._workspace_id = current_workspace_id()
		self._controller = AgentController()
		self._build_ui()
		self.chat_panel.add_message(
			"VERITAS",
			"메시지를 입력하면 backend agent 응답이 표시됩니다.",
			False,
		)

	def _build_ui(self) -> None:
		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(0)

		panel = QFrame()
		panel.setObjectName("AssistPagePanel")
		panel_layout = QVBoxLayout(panel)
		panel_layout.setContentsMargins(12, 12, 12, 12)
		panel_layout.setSpacing(10)

		self.chat_panel = ChatPanel("문서 채팅")
		self.input_bar = ChatInputBar()
		self.input_bar.modeChanged.connect(self._set_mode)
		self.input_bar.sendRequested.connect(self._send_message)

		panel_layout.addWidget(self.chat_panel, 1)
		panel_layout.addWidget(self.input_bar)
		root.addWidget(panel, 1)

	def _set_mode(self, mode: str) -> None:
		self._mode = "rag" if mode == "rag" else "research"

	def _send_message(self, message: str) -> None:
		text = message.rstrip("\n")
		if not text.strip():
			return

		self.chat_panel.add_message("사용자", text, True)
		try:
			reply = self._controller.send_chat_message(self._workspace_id, text, self._mode)
		except ApiError as e:
			reply = f"API 요청 실패: {e}"
		self.chat_panel.add_message("VERITAS", reply, False)
