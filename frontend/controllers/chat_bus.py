from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from ..api_common import ApiError, api_client


class ChatStreamWorker(QThread):
	"""Runs one streaming chat request on a background thread.

	Emits chunks for live UI updates and a `finished` signal when complete.
	"""

	started_stream = Signal(str)
	delta = Signal(str)
	completed = Signal(str)
	failed = Signal(str)

	def __init__(
		self,
		workspace_id: str,
		message: str,
		mode: str,
		parent: QObject | None = None,
	) -> None:
		super().__init__(parent)
		self._workspace_id = workspace_id
		self._message = message
		self._mode = mode

	def run(self) -> None:  # type: ignore[override]
		buffer: list[str] = []
		try:
			stream = api_client.stream_post_sse(
				"/api/v1/chat/messages/stream",
				{
					"workspaceId": self._workspace_id,
					"message": self._message,
					"mode": self._mode,
				},
			)
			for event_name, data in stream:
				if event_name == "start":
					self.started_stream.emit(str(data.get("messageId") or ""))
				elif event_name == "delta":
					chunk = str(data.get("text") or "")
					if chunk:
						buffer.append(chunk)
						self.delta.emit(chunk)
				elif event_name == "done":
					final_text = str(data.get("assistant") or "".join(buffer))
					self.completed.emit(final_text)
					return
				elif event_name == "error":
					self.failed.emit(str(data.get("error") or "stream error"))
					return
		except ApiError as e:
			self.failed.emit(str(e))
			return
		except Exception as e:
			self.failed.emit(f"{type(e).__name__}: {e}")
			return
		# Stream closed without explicit done; surface what we have.
		if buffer:
			self.completed.emit("".join(buffer))
		else:
			self.failed.emit("empty response")


class ChatBus(QObject):
	"""Application-wide chat coordinator.

	Routes every user-sent message through one streaming worker and broadcasts
	the resulting bubble updates to every subscribed chat panel. This is how
	the main "AI 채팅" page and the floating "보조 창" stay in sync.
	"""

	userMessageQueued = Signal(str, str)  # workspace_id, text
	assistantStreamStarted = Signal()
	assistantChunk = Signal(str)
	assistantCompleted = Signal(str)
	assistantFailed = Signal(str)

	_instance: "ChatBus | None" = None

	def __init__(self, parent: QObject | None = None) -> None:
		super().__init__(parent)
		self._active_worker: ChatStreamWorker | None = None

	@classmethod
	def instance(cls) -> "ChatBus":
		if cls._instance is None:
			cls._instance = ChatBus()
		return cls._instance

	def is_busy(self) -> bool:
		return self._active_worker is not None and self._active_worker.isRunning()

	def send(self, workspace_id: str, message: str, mode: str) -> bool:
		"""Begin a streaming chat turn. Returns False if a turn is already in flight."""
		text = (message or "").strip()
		if not text:
			return False
		if self.is_busy():
			return False

		self.userMessageQueued.emit(workspace_id, text)
		self.assistantStreamStarted.emit()

		worker = ChatStreamWorker(workspace_id, text, mode)
		worker.delta.connect(self._on_delta)
		worker.completed.connect(self._on_completed)
		worker.failed.connect(self._on_failed)
		worker.finished.connect(self._clear_worker)
		self._active_worker = worker
		worker.start()
		return True

	def _on_delta(self, chunk: str) -> None:
		if chunk:
			self.assistantChunk.emit(chunk)

	def _on_completed(self, text: str) -> None:
		self.assistantCompleted.emit(text)

	def _on_failed(self, error: str) -> None:
		self.assistantFailed.emit(error)

	def _clear_worker(self) -> None:
		worker = self._active_worker
		self._active_worker = None
		if worker is not None:
			worker.deleteLater()


def get_chat_bus() -> ChatBus:
	return ChatBus.instance()
