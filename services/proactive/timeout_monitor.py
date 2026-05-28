"""Backend timeout sweeper for rendered interventions and no-op outcomes.

Two reasons we don't rely on the frontend's expire timer alone:

1. The frontend can be killed (window closed, OS sleep) while the backend
   keeps running — without a server-side sweep, those decisions never get a
   ``timeout`` reward and the engage policy permanently undercounts negative
   signal.

2. ``no_op`` decisions don't render anything, so the frontend has no
   trigger for them. The "did writing resume in 30s?" heuristic must run
   server-side.

The monitor is a daemon thread that polls the policy store's
``pending_timeouts.jsonl`` every ``tick_seconds`` and calls back into the
orchestrator's ``_resolve_timeout`` / ``_resolve_noop_outcome`` for any
expired entry. Polling (rather than per-decision timers) is intentional: it
keeps the thread count bounded and survives clock jumps.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        # Normalize trailing Z (we write Z, not +00:00, throughout).
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return None


class TimeoutMonitor:
    """Periodic sweeper coupled to a single workspace's policy store.

    The orchestrator owns one of these per workspace bind; ``rebind`` lets the
    runtime swap workspaces without tearing the thread down.
    """

    def __init__(
        self,
        *,
        on_render_timeout: Callable[[dict[str, Any]], None],
        on_noop_outcome: Callable[[dict[str, Any]], None],
        get_pending: Callable[[], list[dict[str, Any]]],
        set_pending: Callable[[list[dict[str, Any]]], None],
        tick_seconds: float = 2.0,
    ) -> None:
        self._on_render_timeout = on_render_timeout
        self._on_noop_outcome = on_noop_outcome
        self._get_pending = get_pending
        self._set_pending = set_pending
        self._tick_seconds = max(0.5, float(tick_seconds))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            t = threading.Thread(
                target=self._run,
                name="proactive-timeout-monitor",
                daemon=True,
            )
            self._thread = t
            t.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            t = self._thread
            self._thread = None
        if t is not None and t.is_alive():
            # Don't join from inside the GIL-holding scheduler; daemon thread
            # will exit when the interpreter does. Just wait briefly.
            t.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sweep_once()
            except Exception as e:  # noqa: BLE001 — keep the thread alive
                log.warning("[proactive][timeout] sweep failed: %s", e)
            self._stop.wait(self._tick_seconds)

    def _sweep_once(self) -> None:
        pending = self._get_pending() or []
        if not pending:
            return
        now = datetime.now(timezone.utc)
        keep: list[dict[str, Any]] = []
        for entry in pending:
            kind = str(entry.get("kind") or "render_timeout")
            expires_at = _parse_iso(str(entry.get("expires_at") or ""))
            if expires_at is None:
                # Malformed entry — drop it to avoid an infinite loop.
                continue
            if now < expires_at:
                keep.append(entry)
                continue
            # Expired. Hand off to the orchestrator.
            try:
                if kind == "noop_outcome":
                    self._on_noop_outcome(entry)
                else:
                    self._on_render_timeout(entry)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "[proactive][timeout] resolve %s for %s failed: %s",
                    kind,
                    entry.get("decision_id"),
                    e,
                )
        self._set_pending(keep)


# Horizons declared here (rather than inside the orchestrator) so they're easy
# to override in tests and the spec's §11.3 numbers live in one place.
RENDER_TIMEOUT_SECONDS: dict[str, float] = {
    "native_ghost": 20.0,
    "native_inline_diff": 30.0,
    "external_card": 45.0,
    "none": 30.0,
}


# No-op outcome observation horizon. The spec (§10.5) uses 30s.
NOOP_OUTCOME_HORIZON_SECONDS: float = 30.0


def render_timeout_seconds(render_mode: str) -> float:
    return RENDER_TIMEOUT_SECONDS.get(render_mode, RENDER_TIMEOUT_SECONDS["none"])


def now_plus(seconds: float) -> str:
    from datetime import timedelta

    when = datetime.now(timezone.utc) + timedelta(seconds=float(seconds))
    return when.isoformat().replace("+00:00", "Z")


def _seconds_since(iso: str | None) -> float | None:
    if not iso:
        return None
    when = _parse_iso(iso)
    if when is None:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds()


def classify_noop_outcome(
    *,
    decision_iso: str,
    edit_volume_since: float,
    churn_score_since: float,
    idle_sec_since: float,
) -> str:
    """Decide whether a no-op was retrospectively a good or bad call.

    Heuristic (matches §10.5 of the spec):
      - meaningful writing resumed (edits with low churn) → ``noop_positive``
      - idle persisted *or* churn ballooned                → ``noop_negative``
      - otherwise (not enough signal)                       → ``""`` (skip)
    """
    elapsed = _seconds_since(decision_iso)
    if elapsed is None or elapsed < 1.0:
        return ""
    if edit_volume_since >= 40 and churn_score_since < 0.50:
        return "noop_positive"
    if idle_sec_since >= 25.0 or churn_score_since >= 0.60:
        return "noop_negative"
    return ""
