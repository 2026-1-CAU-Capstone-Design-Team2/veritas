from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal

from ..api_common import ApiError, api_client


class EditorSuggestWorker(QThread):
    """Runs one inline ghost-writing request on a background thread.

    A near-clone of :class:`~frontend.controllers.chat_bus.ChatStreamWorker`,
    pointed at ``POST /api/v1/editor/suggest``. It emits each streamed chunk for
    a live preview of the suggestion and a ``completed`` signal with the full
    text when the stream closes.

    Unlike the chat worker this does *not* touch the JobManager: ghost
    suggestions re-trigger on every keystroke, so the editor window simply keeps
    a single live worker and gates new triggers on ``is_blocked(EDITOR)`` (which
    is what suppresses suggestions while AutoSurvey runs).
    """

    started_stream = Signal(str)  # suggestionId
    delta = Signal(str)
    completed = Signal(str)  # full suggestion text
    failed = Signal(str)

    def __init__(
        self,
        workspace_id: str,
        prefix: str,
        suffix: str,
        max_tokens: int = 64,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._workspace_id = workspace_id
        self._prefix = prefix
        self._suffix = suffix
        self._max_tokens = max_tokens

    def run(self) -> None:  # type: ignore[override]
        buffer: list[str] = []
        try:
            stream = api_client.stream_post_sse(
                "/api/v1/editor/suggest",
                {
                    "workspaceId": self._workspace_id,
                    "prefix": self._prefix,
                    "suffix": self._suffix,
                    "maxTokens": self._max_tokens,
                },
            )
            for event_name, data in stream:
                if event_name == "start":
                    self.started_stream.emit(str(data.get("suggestionId") or ""))
                elif event_name == "delta":
                    chunk = str(data.get("text") or "")
                    if chunk:
                        buffer.append(chunk)
                        self.delta.emit(chunk)
                elif event_name == "done":
                    final_text = str(data.get("text") or "".join(buffer))
                    self.completed.emit(final_text)
                    return
                elif event_name == "error":
                    self.failed.emit(str(data.get("error") or "stream error"))
                    return
        except ApiError as e:
            self.failed.emit(str(e))
            return
        except Exception as e:  # noqa: BLE001 — surfaced to the UI as a failure
            self.failed.emit(f"{type(e).__name__}: {e}")
            return
        # Stream closed without an explicit done — surface what we have.
        self.completed.emit("".join(buffer))
