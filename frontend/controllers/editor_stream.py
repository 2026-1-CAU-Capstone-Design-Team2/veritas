from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from ..api_common import ApiError, api_client


class _EditorStreamWorker(QThread):
    """Base SSE worker for the editor's streaming endpoints.

    All editor streams share the chat SSE shape (start / delta / done / error),
    so one base consumes the stream and re-emits Qt signals; subclasses only
    supply the path + JSON payload. Unlike ``ChatStreamWorker`` this does not
    touch the JobManager — editor streams re-trigger freely and the window keeps
    a single live worker per surface.
    """

    started_stream = Signal(str)  # start-event id (suggestionId / assistId / chatId)
    delta = Signal(str)
    completed = Signal(str)  # full text
    failed = Signal(str)
    # Proactive bandit start-event extras: emitted exactly once per stream when
    # the backend's /editor/suggest wrapper carries a decisionId. Subscribers
    # store it and use it for /api/v1/proactive/feedback when the user
    # accepts/rejects/retries the suggestion. ``should_intervene=False`` means
    # the bandit chose no_op — the legacy slot still emits ``completed("")``
    # so existing on_completed handlers keep working.
    proactive_started = Signal(dict)  # {"decisionId": str, "shouldIntervene": bool, ...}

    _START_ID_KEYS = ("suggestionId", "assistId", "chatId")

    def __init__(self, path: str, payload: dict[str, Any], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._path = path
        self._payload = payload

    def run(self) -> None:  # type: ignore[override]
        buffer: list[str] = []
        try:
            stream = api_client.stream_post_sse(self._path, self._payload)
            for event_name, data in stream:
                if event_name == "start":
                    start_id = ""
                    for key in self._START_ID_KEYS:
                        if data.get(key):
                            start_id = str(data.get(key))
                            break
                    self.started_stream.emit(start_id)
                    # Forward the proactive-only fields without polluting the
                    # legacy start-id slot. Empty dict for non-proactive routes.
                    if data.get("decisionId"):
                        self.proactive_started.emit(
                            {
                                "decisionId": str(data.get("decisionId") or ""),
                                "shouldIntervene": bool(data.get("shouldIntervene")),
                                "suggestionType": data.get("suggestionType"),
                                "renderMode": data.get("renderMode"),
                            }
                        )
                elif event_name == "delta":
                    chunk = str(data.get("text") or "")
                    if chunk:
                        buffer.append(chunk)
                        self.delta.emit(chunk)
                elif event_name == "done":
                    self.completed.emit(str(data.get("text") or "".join(buffer)))
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
        self.completed.emit("".join(buffer))


class EditorSuggestWorker(_EditorStreamWorker):
    """Inline ghost-writing continuation (``POST /api/v1/editor/suggest``).

    ``use_workspace`` asks the backend to ground the suggestion in the active
    workspace's RAG index (embedding retrieval); it falls back to ungrounded
    continuation when grounding is unavailable.
    """

    def __init__(
        self,
        workspace_id: str,
        prefix: str,
        suffix: str,
        max_tokens: int = 64,
        use_workspace: bool = True,
        parent: QObject | None = None,
        *,
        cursor: int = 0,
    ) -> None:
        super().__init__(
            "/api/v1/editor/suggest",
            {
                "workspaceId": workspace_id,
                "prefix": prefix,
                "suffix": suffix,
                "maxTokens": max_tokens,
                "useWorkspace": use_workspace,
                # True caret offset in the whole document (NOT len(prefix)) so the
                # backend reject-ladder localizes a 3-reject cooldown to this spot
                # instead of the whole document. See editor_window._fire_suggestion.
                "cursor": int(cursor),
            },
            parent,
        )


class EditorAssistWorker(_EditorStreamWorker):
    """Quick-action transform (``POST /api/v1/editor/assist``)."""

    def __init__(
        self,
        workspace_id: str,
        action: str,
        text: str,
        max_tokens: int = 400,
        use_workspace: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(
            "/api/v1/editor/assist",
            {
                "workspaceId": workspace_id,
                "action": action,
                "text": text,
                "maxTokens": max_tokens,
                "useWorkspace": use_workspace,
            },
            parent,
        )
