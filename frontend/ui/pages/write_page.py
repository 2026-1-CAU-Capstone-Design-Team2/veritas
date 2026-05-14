from __future__ import annotations

from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from ...api_common import current_workspace_id
from ...controllers import AgentController, JobCategory, get_chat_bus, get_job_manager
from ..windows.document_assist_window import ChatInputBar, ChatPanel, render_history_html


class WritePage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		# RAG is the implicit default chat mode; 자료조사 is opt-in.
		self._mode = "rag"
		self._workspace_id = current_workspace_id()
		self._controller = AgentController()
		self._bus = get_chat_bus()
		self._streaming = False
		# Monotonic guard so an out-of-order history fetch can't overwrite a
		# newer refresh (rapid page switches / workspace changes).
		self._history_token = 0
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
		# Global busy state drives the input enable/disable: while research is
		# running or another chat is mid-stream, the chat input is locked.
		get_job_manager().busy_changed.connect(self._sync_busy_state)
		self._sync_busy_state()

	def _sync_busy_state(self) -> None:
		blocked = get_job_manager().is_blocked(JobCategory.CHAT)
		self.input_bar.setEnabled(not blocked)
		if blocked:
			self.input_bar.input.setPlaceholderText(
				"다른 작업이 진행 중입니다. 잠시만 기다려 주세요..."
			)
		else:
			# Restore mode-appropriate placeholder.
			self.input_bar.set_mode(self._mode, emit=False)

	def _set_mode(self, mode: str) -> None:
		self._mode = "rag" if mode == "rag" else "research"

	def set_workspace_by_name(self, _workspace_name: str) -> None:
		self.refresh()

	def refresh(self) -> None:
		self._workspace_id = current_workspace_id()
		# Clearing the panel deletes any in-flight streaming bubble; drop the
		# streaming flag too so late chunks from the previous workspace's turn
		# are ignored instead of writing into a freed widget.
		self._streaming = False
		self.chat_panel.clear_messages()
		self.chat_panel.add_message("VERITAS", "채팅 기록을 불러오는 중입니다...", False)

		# get_chat_history is a blocking HTTP call — run it off the UI thread so
		# navigating to this page never freezes. The token guards against an
		# out-of-order completion overwriting a newer refresh.
		self._history_token += 1
		token = self._history_token
		workspace_id = self._workspace_id
		controller = self._controller

		def _load() -> list:
			# Fetch AND render the markdown off the UI thread — render_history_html
			# is pure, so the per-message parse never touches the main thread.
			history = controller.get_chat_history(workspace_id)
			return render_history_html(history if isinstance(history, list) else [])

		def _apply(prepared: object) -> None:
			if token != self._history_token:
				return
			self._render_history(prepared if isinstance(prepared, list) else [])

		def _failed(_message: str) -> None:
			if token != self._history_token:
				return
			self._render_history([])

		get_job_manager().run_detached(_load, on_success=_apply, on_error=_failed)

	def _render_history(self, prepared: list) -> None:
		"""Build the chat panel from pre-rendered workspace history specs."""
		self.chat_panel.clear_messages()
		if not prepared:
			self.chat_panel.add_message(
				"VERITAS",
				"메시지를 입력하면 선택한 워크스페이스의 지식베이스로 답변합니다.",
				False,
			)
			return

		for spec in prepared:
			if not isinstance(spec, dict):
				continue
			text = str(spec.get("text") or "")
			if not text:
				continue
			is_user = bool(spec.get("is_user"))
			self.chat_panel.add_message(
				"사용자" if is_user else "VERITAS",
				text,
				is_user,
				rendered_html=spec.get("html") or None,
			)

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
		# Input enable/disable is driven by JobManager.busy_changed, not here.
		self._streaming = True
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

	def _on_stream_failed(self, error: str) -> None:
		if not self._streaming:
			return
		self._streaming = False
		self.chat_panel.cancel_streaming_assistant(error)
