from __future__ import annotations

from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from ...api_common import ApiError, current_workspace_id
from ...controllers import AgentController, get_chat_bus
from ..windows.document_assist_window import ChatInputBar, ChatPanel


class WritePage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._mode = "research"
		self._workspace_id = current_workspace_id()
		self._controller = AgentController()
		self._bus = get_chat_bus()
		self._streaming = False
		self._build_ui()
		self._connect_bus()
		self.refresh()

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

	def _connect_bus(self) -> None:
		self._bus.userMessageQueued.connect(self._on_user_message_queued)
		self._bus.assistantStreamStarted.connect(self._on_stream_started)
		self._bus.assistantChunk.connect(self._on_stream_chunk)
		self._bus.assistantCompleted.connect(self._on_stream_completed)
		self._bus.assistantFailed.connect(self._on_stream_failed)

	def _set_mode(self, mode: str) -> None:
		self._mode = "rag" if mode == "rag" else "research"

	def set_workspace_by_name(self, _workspace_name: str) -> None:
		self.refresh()

	def refresh(self) -> None:
		self._workspace_id = current_workspace_id()
		self.chat_panel.clear_messages()
		try:
			history = self._controller.get_chat_history(self._workspace_id)
		except ApiError:
			history = []

		if not history:
			self.chat_panel.add_message(
				"VERITAS",
				"메시지를 입력하면 선택한 워크스페이스의 지식베이스로 답변합니다.",
				False,
			)
			return

		for item in history:
			if not isinstance(item, dict):
				continue
			role = str(item.get("role") or "")
			text = str(item.get("text") or "")
			if not text:
				continue
			self.chat_panel.add_message("사용자" if role == "user" else "VERITAS", text, role == "user")

	def _send_message(self, message: str) -> None:
		text = message.rstrip("\n").strip()
		if not text:
			return

		self._workspace_id = current_workspace_id()
		if not self._bus.send(self._workspace_id, text, self._mode):
			# A turn is already in flight; surface a hint without blocking.
			self.chat_panel.add_message("VERITAS", "이미 답변을 생성하고 있어요. 잠시만 기다려 주세요.", False)

	def _on_user_message_queued(self, _workspace_id: str, text: str) -> None:
		self.chat_panel.add_message("사용자", text, True)

	def _on_stream_started(self) -> None:
		self._streaming = True
		self.input_bar.setEnabled(False)
		self.chat_panel.start_streaming_assistant("VERITAS")

	def _on_stream_chunk(self, chunk: str) -> None:
		if not self._streaming:
			return
		self.chat_panel.append_streaming_chunk(chunk)

	def _on_stream_completed(self, text: str) -> None:
		if not self._streaming:
			return
		self._streaming = False
		self.chat_panel.finalize_streaming_assistant(text)
		self.input_bar.setEnabled(True)

	def _on_stream_failed(self, error: str) -> None:
		if not self._streaming:
			return
		self._streaming = False
		self.chat_panel.cancel_streaming_assistant(error)
		self.input_bar.setEnabled(True)
