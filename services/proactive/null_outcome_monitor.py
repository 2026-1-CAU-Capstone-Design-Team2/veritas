"""Null-outcome classification: TN proxy / FN proxy / unknown.

When ``observe()`` returns a ``NullPrediction`` we record a pending entry
with a 30~60s horizon. Once the horizon elapses, the monitor compares
subsequent telemetry against the user's behavior:

- ``tn_proxy``  — user kept writing naturally; staying silent was correct.
- ``fn_proxy``  — user got stuck (idle / churn) or asked for help in another
                  channel; we should have intervened.
- ``unknown``   — insufficient data (app switched, document lost, etc).

This replaces the bandit-era "no-op reward update". We don't feed these into
any learned model; they're logged for the dashboard and for later threshold
tuning (operator-facing).

Like ``timeout_monitor``, this is a daemon-thread sweeper decoupled via
callbacks from the orchestrator.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)


# Spec §8 — 30s default. The orchestrator can override per-entry by setting
# a custom expires_at when registering.
NULL_OUTCOME_HORIZON_SECONDS: float = 30.0


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return None


def now_plus(seconds: float) -> str:
    when = datetime.now(timezone.utc) + timedelta(seconds=float(seconds))
    return when.isoformat().replace("+00:00", "Z")


def classify_null_outcome(
    *,
    edit_volume_since: float,
    churn_since: float,
    idle_since: float,
    user_invoked_help: bool = False,
    app_switched: bool = False,
    document_lost: bool = False,
) -> str:
    """Spec §8 heuristic. Returns ``"tn_proxy"`` / ``"fn_proxy"`` / ``"unknown"``.

    The thresholds are deliberately conservative — heuristics this noisy
    should be biased toward ``unknown`` rather than committing to a label.
    """
    if app_switched or document_lost:
        return "unknown"

    # FN proxy: user got stuck OR explicitly asked for help.
    if user_invoked_help:
        return "fn_proxy"
    if idle_since >= 25.0:
        return "fn_proxy"
    if churn_since >= 0.60:
        return "fn_proxy"

    # TN proxy: continued writing naturally.
    if edit_volume_since >= 40.0 and churn_since < 0.50:
        return "tn_proxy"

    return "unknown"


class NullOutcomeMonitor:
    """Daemon thread that resolves pending null-outcome entries."""

    def __init__(
        self,
        *,
        on_resolve: Callable[[dict[str, Any]], None],
        get_pending: Callable[[], list[dict[str, Any]]],
        set_pending: Callable[[list[dict[str, Any]]], None],
        tick_seconds: float = 5.0,
    ) -> None:
        self._on_resolve = on_resolve
        self._get_pending = get_pending
        self._set_pending = set_pending
        self._tick_seconds = max(1.0, float(tick_seconds))
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
                name="proactive-null-outcome",
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
            except Exception as e:  # noqa: BLE001
                log.warning("[proactive][null_outcome] sweep failed: %s", e)
            self._stop.wait(self._tick_seconds)

    def _sweep_once(self) -> None:
        pending = self._get_pending() or []
        if not pending:
            return
        now = datetime.now(timezone.utc)
        keep: list[dict[str, Any]] = []
        for entry in pending:
            if str(entry.get("kind") or "") != "null_outcome":
                keep.append(entry)
                continue
            expires_at = _parse_iso(str(entry.get("expires_at") or ""))
            if expires_at is None:
                continue
            if now < expires_at:
                keep.append(entry)
                continue
            try:
                self._on_resolve(entry)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "[proactive][null_outcome] resolve for %s failed: %s",
                    entry.get("decision_id"),
                    e,
                )
        self._set_pending(keep)
