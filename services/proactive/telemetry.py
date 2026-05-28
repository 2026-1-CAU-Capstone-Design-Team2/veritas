"""Structured console + file telemetry for the proactive bandit.

The user wants two things when they run with ``--console-logs``:

1. **A single readable line per decision** in the API process's console so they
   can watch the bandit's behavior in real time without tailing JSONL files.
2. **A per-workspace timeline file** (``proactive.log``) under the policy
   store dir so they can review the day's decisions even after restart.

The launcher already pipes API-child stdout to the console when
``--console-logs`` (or ``VERITAS_LOG_MODE``) is set. We honor the same toggles
plus an explicit ``VERITAS_PROACTIVE_LOG`` override for users who want
proactive telemetry on without enabling every other tag.

Format (one line per event):

    [proactive][decision] pd_abcd1234 native_editor engage=intervene π=0.42 \\
        candidate=paragraph_rewrite mean=+0.18 std=0.32 gate=- recent_neg=0.05 \\
        idle=4.2s mask=[next_sentence,paragraph_rewrite,local_copyedit]
    [proactive][feedback] pd_abcd1234 native_editor accept r_engage=+1.0 \\
        r_suggest=+1.0 took=2.3s
    [proactive][update]   pd_abcd1234 engage_residual=+0.58 suggest_arm=paragraph_rewrite
    [proactive][noop_out] pd_abcd1234 noop_positive vol=85 churn=0.12 idle=8.3s
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOGGER_NAME = "proactive"


def _console_enabled() -> bool:
    """True when the user wants proactive telemetry on the console.

    Mirrors launcher.console_logs_enabled() — we do NOT import the launcher
    here because the API child process doesn't always have it on sys.path and
    we want this module to stay importable from anywhere.
    """
    if os.getenv("VERITAS_PROACTIVE_LOG", "").strip().lower() in {"0", "false", "off"}:
        return False
    if os.getenv("VERITAS_PROACTIVE_LOG", "").strip().lower() in {"1", "true", "on"}:
        return True
    # Fall through to the global console-logs toggle.
    if "--console-logs" in sys.argv:
        return True
    return os.getenv("VERITAS_LOG_MODE", "").strip().lower() in {
        "console",
        "stdout",
        "terminal",
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _ProactiveTelemetry:
    """Per-workspace logger. Constructed by ``get_telemetry(workspace_id)``."""

    def __init__(self, *, workspace_id: str, log_dir: Path) -> None:
        self.workspace_id = workspace_id
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / "proactive.log"
        self._lock = threading.Lock()
        self._console = _console_enabled()
        # The logger is shared across telemetry instances (Python's name-keyed
        # registry handles that for us); we attach the file handler exactly
        # once per workspace path so a workspace switch doesn't double-log.
        self._logger = logging.getLogger(f"{_LOGGER_NAME}.{workspace_id}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            fh = logging.FileHandler(self.log_path, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self._logger.addHandler(fh)

    # ----------------------------------------------------------- emit

    def _emit(self, line: str) -> None:
        with self._lock:
            self._logger.info(line)
            if self._console:
                # Plain print so it shows up in the launcher's API-child pipe
                # without going through Python logging's stderr routing. The
                # launcher's API child calls ``force_utf8_stdio`` at startup, but
                # if this is exercised under a cp949 stdout (e.g. a one-off
                # script) we still want a readable line, not a UnicodeEncodeError.
                try:
                    print(line, flush=True)
                except UnicodeEncodeError:
                    enc = (getattr(sys.stdout, "encoding", None) or "ascii")
                    fallback = line.encode(enc, errors="replace").decode(enc, errors="replace")
                    print(fallback, flush=True)

    # ----------------------------------------------------------- events

    def decision(
        self,
        *,
        decision_id: str,
        surface: str,
        engage_action: str,
        intervention_probability: float,
        candidate: str | None,
        suggestion_type: str | None,
        render_mode: str,
        engage_info: dict[str, Any],
        primitive: dict[str, Any],
        available: list[str],
        suggest_scores: dict[str, float] | None = None,
    ) -> None:
        gate = str(engage_info.get("gate_reason") or "-") or "-"
        mean = float(engage_info.get("mean") or 0.0)
        std = float(engage_info.get("std") or 0.0)
        idle = float(primitive.get("idle_sec") or 0.0)
        recent_neg = float(primitive.get("recent_negative_rate") or 0.0)
        mask_str = ",".join(available) if available else "-"
        chosen = suggestion_type or candidate or "-"
        # Warmup progress is the operator's eye-level signal for "is the
        # policy still in forced-exploration?" — surface it inline.
        warmup_active = bool(engage_info.get("warmup_active"))
        warmup_remaining = int(engage_info.get("warmup_remaining") or 0)
        warmup_str = (
            f" warmup={warmup_remaining}left"
            if warmup_active
            else ""
        )
        # Top-3 UCB scores: operator's "which arms were close runners-up?"
        # signal. Empty when only one arm was in the mask.
        scores_str = ""
        if suggest_scores:
            ranked = sorted(
                suggest_scores.items(), key=lambda kv: kv[1], reverse=True
            )[:3]
            if ranked:
                scores_str = " ucb=[" + ",".join(
                    f"{arm}:{score:+.2f}" for arm, score in ranked
                ) + "]"
        line = (
            f"[proactive][decision] {decision_id} {surface} "
            f"engage={engage_action} π={intervention_probability:.3f} "
            f"candidate={candidate or '-'} chosen={chosen} render={render_mode} "
            f"mean={mean:+.3f} std={std:.3f} gate={gate} "
            f"recent_neg={recent_neg:.2f} idle={idle:.1f}s{warmup_str}{scores_str} "
            f"mask=[{mask_str}]"
        )
        self._emit(line)

    def feedback(
        self,
        *,
        decision_id: str,
        surface: str,
        canonical: str,
        engage_reward: float | None,
        suggestion_reward: float | None,
        decision_created_at: str | None = None,
    ) -> None:
        took = ""
        if decision_created_at:
            try:
                start = datetime.fromisoformat(
                    decision_created_at.replace("Z", "+00:00")
                )
                took = f" took={(datetime.now(timezone.utc) - start).total_seconds():.1f}s"
            except Exception:
                took = ""
        er = f"{engage_reward:+.2f}" if engage_reward is not None else "-"
        sr = f"{suggestion_reward:+.2f}" if suggestion_reward is not None else "-"
        line = (
            f"[proactive][feedback] {decision_id} {surface} {canonical} "
            f"r_engage={er} r_suggest={sr}{took}"
        )
        self._emit(line)

    def update(
        self,
        *,
        decision_id: str,
        engage_update: dict[str, Any] | None,
        suggest_update: dict[str, Any] | None,
    ) -> None:
        eng = engage_update or {}
        sug = suggest_update or {}
        parts = [f"[proactive][update]  {decision_id}"]
        if eng.get("updated"):
            parts.append(
                f"engage_residual={float(eng.get('residual') or 0.0):+.3f}"
                f" var_factor={float(eng.get('variance_factor') or 0.0):.3f}"
            )
        if sug.get("updated"):
            parts.append(
                f"suggest_arm={sug.get('action')} reward={float(sug.get('reward') or 0.0):+.2f}"
            )
        if len(parts) == 1:
            parts.append("skipped")
        self._emit(" ".join(parts))

    def noop_outcome(
        self,
        *,
        decision_id: str,
        outcome: str,
        edit_volume: float,
        churn: float,
        idle: float,
    ) -> None:
        self._emit(
            f"[proactive][noop_out] {decision_id} {outcome} "
            f"vol={edit_volume:.0f} churn={churn:.2f} idle={idle:.1f}s"
        )

    def custom(self, tag: str, message: str) -> None:
        """Escape hatch for one-off lines (e.g. wrapper diagnostics)."""
        self._emit(f"[proactive][{tag}] {message}")

    def close(self) -> None:
        """Release the file handle so a Windows-side directory cleanup
        (workspace switch / test teardown) doesn't trip on a held handle."""
        with self._lock:
            for handler in list(self._logger.handlers):
                try:
                    handler.flush()
                    handler.close()
                except Exception:
                    pass
                self._logger.removeHandler(handler)


# Cache one logger per workspace so the file handler isn't reopened on every
# observe(). Keyed by the absolute policy_dir path to survive workspace renames.
_telemetry_cache: dict[str, _ProactiveTelemetry] = {}
_cache_lock = threading.Lock()


def get_telemetry(*, workspace_id: str, log_dir: Path) -> _ProactiveTelemetry:
    key = str(Path(log_dir).resolve())
    with _cache_lock:
        existing = _telemetry_cache.get(key)
        if existing is not None:
            return existing
        instance = _ProactiveTelemetry(workspace_id=workspace_id, log_dir=Path(log_dir))
        _telemetry_cache[key] = instance
        return instance


def release_telemetry(log_dir: Path) -> None:
    """Drop the cached telemetry for ``log_dir`` and close its file handler."""
    key = str(Path(log_dir).resolve())
    with _cache_lock:
        instance = _telemetry_cache.pop(key, None)
    if instance is not None:
        instance.close()


def console_enabled() -> bool:
    """Public re-export so tests / callers can short-circuit expensive log
    builds when nothing is listening."""
    return _console_enabled()
