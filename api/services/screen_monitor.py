"""Screen-intervention event stream + monitoring lifecycle.

Owned by :class:`AgentRuntime`. The actual screen polling thread lives
inside :class:`agent.ChatAgent` (it's the one with the OCR / UIA capture
loop); this controller owns the *runtime-side state* around it:

* the ring buffer of intervention answers the frontend polls for;
* the "monitoring started at" timestamp the status endpoint reports;
* the lifecycle locks that keep workspace-switch races safe.

The ``chat_agent`` and the tool ``registry`` are rebound on every
workspace switch (:meth:`bind` is called from
``AgentRuntime._configure_workspace_runtime``) so the controller always
talks to the *current* workspace's agent without holding a stale
reference.

This file is a state-holder. The thin route-handlers that turn HTTP into
controller calls live next door in
``api/services/screen_monitoring_service.py`` and still go through
``AgentRuntime`` so the rest of the API surface stays unchanged.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import HTTPException


SCREEN_EVENT_BUFFER_MAX = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ScreenMonitor:
    """Holds the screen-intervention event ring buffer and lifecycle state.

    Two layers of state live here, with different lock scopes:

    * ``_event_lock`` guards the ring buffer (``_events`` deque + ``_seq``).
      Multiple producer threads (the chat agent's screen poller) and one
      consumer thread (the frontend poller) hit it concurrently.
    * The lifecycle methods (:meth:`start`, :meth:`stop`) use the
      caller-provided ``workspace_lock`` so a workspace switch can't
      sneak in between "check chat_agent" and "call chat_agent.start".
    """

    def __init__(self, *, workspace_lock: threading.RLock) -> None:
        self._workspace_lock = workspace_lock
        self._events: deque[dict[str, Any]] = deque(maxlen=SCREEN_EVENT_BUFFER_MAX)
        self._seq = 0
        self._event_lock = threading.Lock()
        self._started_at: str | None = None
        # Bound late so the same controller instance survives across
        # workspace switches (which rebuild ``chat_agent`` / ``registry``).
        self._chat_agent: Any | None = None
        self._registry: Any | None = None

    def bind(self, *, chat_agent: Any, registry: Any) -> None:
        """Refresh the chat_agent / registry references after a workspace swap.

        Called from :meth:`AgentRuntime._configure_workspace_runtime`. The
        controller deliberately does NOT keep these as constructor args
        because a stale reference would survive a workspace switch and
        emit events against the wrong agent.
        """
        self._chat_agent = chat_agent
        self._registry = registry

    @property
    def started_at(self) -> str | None:
        return self._started_at

    # ----------------------------------------------------------- lifecycle

    def start(self, *, on_answer: Callable[[str, dict[str, Any]], None]) -> dict[str, Any]:
        """Start the underlying ``chat_agent``'s screen poller.

        ``on_answer`` is the callback the agent invokes when an
        intervention produces an assistant answer; the runtime usually
        wires it to :meth:`record_assist_answer` so the answer lands in
        this controller's event buffer.
        """
        with self._workspace_lock:
            if self._chat_agent is None or not self._chat_agent.has_screen_context():
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "screen_context tool is not registered. Enable "
                        "VERITAS_ENABLE_SCREEN_CONTEXT before starting the API."
                    ),
                )
            started = self._chat_agent.start_screen_monitoring(
                on_answer=on_answer,
                stream=True,
            )
            if not started:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Failed to start screen monitoring. Check screen_context tool "
                        "status and capture logs."
                    ),
                )
            if self._started_at is None:
                self._started_at = _now_iso()
            # status() reads chat_agent again — caller wires workspace_id in.
            return self._status_locked()

    def stop(self) -> dict[str, Any]:
        with self._workspace_lock:
            if self._chat_agent is not None and self._chat_agent.has_screen_context():
                self._chat_agent.stop_screen_monitoring()
            self._started_at = None
            return self._status_locked()

    def is_running(self) -> bool:
        """True when a screen-monitor thread is alive on the bound agent."""
        agent = self._chat_agent
        if agent is None:
            return False
        thread = getattr(agent, "_screen_monitor_thread", None)
        return bool(self._started_at and thread and thread.is_alive())

    def stop_for_workspace_switch(self) -> bool:
        """Stop the poller while preserving ``started_at`` so the caller can
        re-start after rebinding to the new workspace.

        Returns ``True`` if the poller was running and was stopped, ``False``
        if there was nothing to stop. Mirrors the
        ``was_monitoring`` / ``start_screen_monitoring()`` dance that
        :meth:`AgentRuntime.set_workspace` used to do inline.
        """
        was_running = self.is_running()
        if was_running and self._chat_agent is not None:
            try:
                self._chat_agent.stop_screen_monitoring()
            except Exception as exc:  # noqa: BLE001 — best-effort during switch
                # Match the pre-extraction warning so the existing log format
                # keeps working with grep / dashboards.
                print(f"[screen_monitoring][warn] stop on workspace switch failed: {exc}")
        return was_running

    # --------------------------------------------------------------- status

    def status(self, *, workspace_id: str) -> dict[str, Any]:
        """Public read-only status payload for the status endpoint."""
        with self._workspace_lock:
            return self._status_locked(workspace_id=workspace_id)

    def _status_locked(self, *, workspace_id: str | None = None) -> dict[str, Any]:
        registered = bool(
            self._registry is not None and self._registry.has("screen_context")
        )
        polling = False
        last_poll_error: str | None = None
        latest_event_id: str | None = None
        latest_captured_at: str | None = None
        latest_diagnostics: dict[str, Any] = {}
        pending_intervention_count = 0
        capture_log_path: str | None = None
        if registered and self._registry is not None:
            try:
                result = self._registry.call("screen_context", action="status")
            except Exception as exc:  # noqa: BLE001
                last_poll_error = f"status call failed: {exc}"
                result = None
            if result is not None and getattr(result, "success", False):
                data = result.data if isinstance(result.data, dict) else {}
                polling = bool(data.get("polling"))
                last_poll_error = data.get("last_poll_error")
                latest_event_id = data.get("latest_event_id")
                latest_captured_at = data.get("latest_captured_at")
                diagnostics = data.get("latest_diagnostics") or {}
                if isinstance(diagnostics, dict):
                    latest_diagnostics = diagnostics
                pending_intervention_count = int(
                    data.get("pending_intervention_count") or 0
                )
                capture_log_path = data.get("capture_log_path")
            elif result is not None:
                last_poll_error = getattr(result, "error", None) or last_poll_error

        with self._event_lock:
            latest_seq = self._seq
            event_buffer_size = len(self._events)

        return {
            "registered": registered,
            "polling": polling,
            "monitoringStartedAt": self._started_at,
            "workspaceId": workspace_id,
            "lastPollError": last_poll_error,
            "latestCaptureEventId": latest_event_id,
            "latestCapturedAt": latest_captured_at,
            "latestDiagnostics": latest_diagnostics,
            "pendingInterventionCount": pending_intervention_count,
            "captureLogPath": capture_log_path,
            "eventBufferSize": event_buffer_size,
            "latestEventSeq": latest_seq,
        }

    # ---------------------------------------------------------- event flow

    def record_feedback(
        self, *, event_id: str, intervention_type: str, action: str, reward: float
    ) -> dict[str, Any]:
        """Persist one user reaction to a shown intervention via the screen tool.

        The reward shaping itself lives at the API service boundary; here we only
        forward the resolved record to the tool/store so the reward log stays the
        single source of truth for a future selection policy."""
        if self._registry is None or not self._registry.has("screen_context"):
            return {"ok": False, "error": "screen_context tool is not registered"}
        result = self._registry.call(
            "screen_context",
            action="record_feedback",
            event_id=event_id,
            intervention_type=intervention_type,
            feedback_action=action,
            reward=reward,
        )
        if not getattr(result, "success", False):
            return {"ok": False, "error": getattr(result, "error", "record_feedback failed")}
        return {"ok": True, "record": result.data}

    def record_assist_answer(
        self,
        answer: str,
        intervention: dict[str, Any],
        *,
        workspace_id: str,
        done: bool = True,
    ) -> None:
        """Append one assistant answer to the buffer (called by chat agent's
        on-answer callback).

        Mirrors the original :meth:`AgentRuntime._on_screen_assist_answer`
        verbatim so the polled event payload is byte-compatible with what
        the frontend was already consuming.
        """
        text = str(answer or "").strip()
        if not text:
            return
        event_id = ""
        if isinstance(intervention, dict):
            event_id = str(intervention.get("event_id") or "").strip()
        # Without a stable event_id, partial updates cannot be matched to a
        # single card, so skip mid-stream calls and only record the final answer.
        if not event_id and not done:
            return
        writing_context = (
            intervention.get("writing_context")
            if isinstance(intervention, dict)
            else {}
        )
        if not isinstance(writing_context, dict):
            writing_context = {}
        app_context = (
            intervention.get("app_context")
            if isinstance(intervention, dict)
            else {}
        )
        if not isinstance(app_context, dict):
            app_context = {}
        focused = " ".join(
            str(writing_context.get("focused_sentence") or "").split()
        ).strip()
        recent = " ".join(
            str(writing_context.get("recent_sentences") or "").split()
        ).strip()
        trigger_text = focused or recent
        with self._event_lock:
            self._seq += 1
            seq = self._seq
            existing = None
            if event_id:
                for candidate in self._events:
                    if candidate.get("eventId") == event_id:
                        existing = candidate
                        break
            # Mid-stream update: refresh the same event's text in place and
            # bump its seq so the cursor poller re-delivers the growing answer.
            if existing is not None:
                existing["answer"] = text
                existing["partial"] = not done
                existing["seq"] = seq
                return
            self._events.append({
                "seq": seq,
                "eventId": event_id or f"proactive_{seq}",
                "workspaceId": workspace_id,
                "answer": text,
                "partial": not done,
                "category": "proactive",
                "interventionType": (
                    str(intervention.get("intervention_type") or "none").strip()
                    if isinstance(intervention, dict)
                    else "none"
                ),
                "tone": "working",
                "createdAt": _now_iso(),
                "capturedAt": intervention.get("captured_at"),
                "triggerText": trigger_text,
                "appContext": {
                    "title": app_context.get("title") or app_context.get("window_title"),
                    "processName": app_context.get("process_name"),
                    "activeAppType": app_context.get("active_app_type")
                    or writing_context.get("active_app_type"),
                },
                "writingContext": {
                    "focusedSentence": focused,
                    "recentSentences": recent,
                    "paragraphSource": writing_context.get("paragraph_source"),
                    "fullTextChars": writing_context.get("full_text_chars"),
                    "confidence": writing_context.get("confidence"),
                },
            })

    def get_events_since(
        self,
        *,
        since: int,
        limit: int,
        workspace_id: str,
    ) -> dict[str, Any]:
        """Cursor-style read of the intervention event ring buffer.

        Scoped to ``workspace_id``: the ring buffer is shared across workspaces
        (one ScreenMonitor for the whole runtime), and every poller restart —
        including the one a workspace switch triggers — begins at cursor 0. Without
        this filter a switch to a new workspace would re-deliver the previous
        workspace's buffered assist answers, so they'd reappear in the assist
        window's suggestion list. Each event is tagged with its origin workspace
        at record time, so we return only the active workspace's events.
        """
        if limit <= 0:
            limit = 20
        limit = min(limit, SCREEN_EVENT_BUFFER_MAX)
        with self._event_lock:
            latest_seq = self._seq
            events = [
                event
                for event in self._events
                if int(event.get("seq", 0)) > since
                and (not workspace_id or event.get("workspaceId") == workspace_id)
            ]
        events.sort(key=lambda item: int(item.get("seq", 0)))
        events = events[:limit]
        next_cursor = events[-1]["seq"] if events else since
        return {
            "items": events,
            "nextCursor": next_cursor,
            "latestSeq": latest_seq,
            "workspaceId": workspace_id,
        }


__all__ = ["ScreenMonitor", "SCREEN_EVENT_BUFFER_MAX"]
