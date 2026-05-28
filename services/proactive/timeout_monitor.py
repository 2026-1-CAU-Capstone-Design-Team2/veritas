"""Background timeout sweeper for rendered tasks.

After the bandit pivot this module is smaller: it only resolves
*render* timeouts (the user never interacted with a shown suggestion).
*No-op outcome* observation moved to :mod:`null_outcome_monitor` because the
heuristic now feeds a TN/FN proxy log rather than a no-op reward update.

Lifecycle:
  - Orchestrator constructs one ``TimeoutMonitor`` per workspace bind and
    starts it on ``__init__``.
  - On workspace switch, ``stop()`` joins the daemon thread (best-effort,
    2s ceiling) before the new orchestrator's monitor takes over.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)


# Spec §11.3 render timeouts (unchanged).
RENDER_TIMEOUT_SECONDS: dict[str, float] = {
    "native_ghost": 20.0,
    "native_inline_diff": 30.0,
    "native_inline_marker": 30.0,
    "external_card_blue": 45.0,
    "external_card_orange": 45.0,
    "external_card_red": 45.0,
    "external_card_green": 45.0,
    "external_card_gray": 45.0,
    "none": 30.0,
}


def render_timeout_seconds(render_mode: str) -> float:
    return RENDER_TIMEOUT_SECONDS.get(render_mode, RENDER_TIMEOUT_SECONDS["none"])


def now_plus(seconds: float) -> str:
    when = datetime.now(timezone.utc) + timedelta(seconds=float(seconds))
    return when.isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return None


class TimeoutMonitor:
    """Daemon thread that polls pending render timeouts.

    Decoupled from the orchestrator via callbacks so the orchestrator owns
    the actual update logic.
    """

    def __init__(
        self,
        *,
        on_render_timeout: Callable[[dict[str, Any]], None],
        get_pending: Callable[[], list[dict[str, Any]]],
        set_pending: Callable[[list[dict[str, Any]]], None],
        tick_seconds: float = 2.0,
    ) -> None:
        self._on_render_timeout = on_render_timeout
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
            self._thread = threading.Thread(
                target=self._run,
                name="proactive-render-timeout",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            t = self._thread
            self._thread = None
        if t is not None and t.is_alive():
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
            if str(entry.get("kind") or "render_timeout") != "render_timeout":
                keep.append(entry)  # null_outcome_monitor handles these
                continue
            expires_at = _parse_iso(str(entry.get("expires_at") or ""))
            if expires_at is None:
                continue
            if now < expires_at:
                keep.append(entry)
                continue
            try:
                self._on_render_timeout(entry)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "[proactive][timeout] resolve render_timeout for %s failed: %s",
                    entry.get("decision_id"),
                    e,
                )
        self._set_pending(keep)
