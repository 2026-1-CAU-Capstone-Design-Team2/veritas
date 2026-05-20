"""Per-stream progress event buffers.

Both research (AutoSurvey) and verify run as long-lived background jobs that
surface stage progress to the polling frontend. The contract is identical:

* a thread-safe ring buffer of ``(seq, stage, message, detail, timestamp)``
  events;
* a cursor (``since``) the poller advances each tick so it only sees new
  events;
* an in-memory ``active_job`` snapshot so the UI can show an "in flight"
  indicator even when no events have arrived yet.

Originally these lived as two parallel sets of fields directly on
:class:`AgentRuntime` (``_research_progress``, ``_research_progress_seq``,
``_research_progress_lock``, ``_research_active_job`` — duplicated for
verify) with three pairs of helper methods (``_reset_*`` / ``_emit_*`` /
``get_*_progress``). One class replaces all of that.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any


# Matches the ring-buffer cap that the verify-routes ``Query(..., le=500)``
# guard already enforces on the poller side.
BUFFER_DEFAULT_MAX = 500


def _now_iso() -> str:
    """ISO-8601 UTC timestamp with the trailing ``Z`` the frontend expects."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ProgressBuffer:
    """Thread-safe ring-buffered event stream with a cursor-pollable read API.

    One instance per logical job stream (research, verify, …). The buffer
    keeps the last ``maxlen`` events; the poller advances via the ``since``
    cursor on each call and :meth:`get_since` skips already-seen seqs.
    :meth:`reset` starts a fresh stream and records an active-job snapshot
    (``startedAt`` and ``status='running'`` are filled in automatically;
    every other field is caller-supplied via ``**job_fields`` so the same
    class serves both research's ``instruction``-bearing snapshots and
    verify's minimal ``workspaceId``-only ones).
    """

    def __init__(self, *, maxlen: int = BUFFER_DEFAULT_MAX) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()
        self._active_job: dict[str, Any] | None = None
        self._maxlen = maxlen

    def reset(self, **job_fields: Any) -> None:
        """Clear the buffer and start a fresh active-job snapshot.

        ``startedAt`` and ``status="running"`` are filled in automatically.
        Callers pass any additional descriptive fields (``jobId``,
        ``workspaceId``, ``instruction``, …) as kwargs; they land in the
        snapshot verbatim so the API can echo them back to the poller.
        """
        with self._lock:
            self._events.clear()
            self._seq = 0
            self._active_job = {
                **job_fields,
                "startedAt": _now_iso(),
                "status": "running",
            }

    def emit(
        self,
        stage: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        final: bool = False,
    ) -> None:
        """Append one event to the buffer.

        Marks the active job as ``"completed"`` or ``"failed"`` when ``final``
        is true — the latter is selected by the caller passing
        ``stage="failed"`` so the frontend can distinguish a graceful run
        from a thrown exception that emitted a final event.
        """
        message_text = " ".join(str(message or "").split()).strip()[:280]
        with self._lock:
            self._seq += 1
            self._events.append(
                {
                    "seq": self._seq,
                    "stage": str(stage or "").strip() or "info",
                    "message": message_text,
                    "detail": detail or {},
                    "timestamp": _now_iso(),
                }
            )
            if self._active_job is not None and final:
                self._active_job["status"] = (
                    "failed" if str(stage) == "failed" else "completed"
                )

    def get_since(self, *, since: int, limit: int) -> dict[str, Any]:
        """Return events whose ``seq > since`` plus the active-job snapshot.

        The returned payload matches the contract the frontend pollers
        expect: ``items`` (ordered by seq), ``nextCursor`` (advance the
        poller), ``latestSeq`` (so the poller can detect a reset), and
        ``activeJob`` (or ``None`` when no job has started).
        """
        if limit <= 0:
            limit = 50
        limit = min(limit, self._maxlen)
        with self._lock:
            latest_seq = self._seq
            events = [
                event
                for event in self._events
                if int(event.get("seq", 0)) > since
            ]
            job_snapshot = dict(self._active_job or {})
        events.sort(key=lambda item: int(item.get("seq", 0)))
        events = events[:limit]
        next_cursor = events[-1]["seq"] if events else since
        return {
            "items": events,
            "nextCursor": next_cursor,
            "latestSeq": latest_seq,
            "activeJob": job_snapshot or None,
        }


__all__ = ["ProgressBuffer", "BUFFER_DEFAULT_MAX"]
