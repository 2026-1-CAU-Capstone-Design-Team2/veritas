"""Structured console + file telemetry for the rule-based proactive system.

Reads the same toggles as before:
- ``--proactive-debug`` (launcher) or ``VERITAS_PROACTIVE_LOG=1``
- ``VERITAS_LOG_MODE=console`` as the global fallback

But every emitted line now describes the rule-based pipeline:
``[proactive][decision]`` carries evaluator_score / threshold / gate_reasons,
``[proactive][feedback]`` carries canonical + adaptation deltas,
``[proactive][null_outcome]`` carries TN/FN proxy classifications.

Lines no longer reference θ̂, UCB, residuals, or warmup — those were
bandit-era concepts.
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
    raw = os.getenv("VERITAS_PROACTIVE_LOG", "").strip().lower()
    if raw in {"0", "false", "off"}:
        return False
    if raw in {"1", "true", "on"}:
        return True
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
    def __init__(self, *, workspace_id: str, log_dir: Path) -> None:
        self.workspace_id = workspace_id
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / "proactive.log"
        self._lock = threading.Lock()
        self._console = _console_enabled()
        self._logger = logging.getLogger(f"{_LOGGER_NAME}.{workspace_id}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            fh = logging.FileHandler(self.log_path, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self._logger.addHandler(fh)

    def _emit(self, line: str) -> None:
        with self._lock:
            self._logger.info(line)
            if self._console:
                try:
                    print(line, flush=True)
                except UnicodeEncodeError:
                    enc = (getattr(sys.stdout, "encoding", None) or "ascii")
                    fallback = line.encode(enc, errors="replace").decode(enc, errors="replace")
                    print(fallback, flush=True)

    def close(self) -> None:
        with self._lock:
            for handler in list(self._logger.handlers):
                try:
                    handler.flush()
                    handler.close()
                except Exception:
                    pass
                self._logger.removeHandler(handler)

    # ----------------------------------------------------------- events

    def decision_task(
        self,
        *,
        decision_id: str,
        surface: str,
        task_type: str,
        anchor_id: str,
        anchor_confidence: float,
        context_scope: str,
        render_mode: str,
        evaluator_score: float,
        threshold: float,
        candidate_count: int,
        primitive: dict[str, Any],
    ) -> None:
        idle = float(primitive.get("idle_sec", 0.0) or 0.0)
        churn = float(primitive.get("churn_score", 0.0) or 0.0)
        recent_neg = float(primitive.get("recent_negative_rate", 0.0) or 0.0)
        line = (
            f"[proactive][decision] {decision_id} {surface} task={task_type} "
            f"anchor={anchor_id} conf={anchor_confidence:.2f} "
            f"scope={context_scope} render={render_mode} "
            f"score={evaluator_score:.3f} threshold={threshold:.3f} "
            f"candidates={candidate_count} "
            f"idle={idle:.1f}s churn={churn:.2f} recent_neg={recent_neg:.2f}"
        )
        self._emit(line)

    def decision_null(
        self,
        *,
        decision_id: str,
        surface: str,
        reason: str,
        candidate_count: int,
        gate_reasons: list[str],
        best_score: float | None = None,
        threshold: float | None = None,
    ) -> None:
        gates = ",".join(gate_reasons) if gate_reasons else "-"
        score = f" best_score={best_score:.3f}" if best_score is not None else ""
        thr = f" threshold={threshold:.3f}" if threshold is not None else ""
        line = (
            f"[proactive][decision] {decision_id} {surface} prediction=null "
            f"reason={reason} candidates={candidate_count} gates=[{gates}]{score}{thr}"
        )
        self._emit(line)

    def feedback(
        self,
        *,
        decision_id: str,
        surface: str,
        canonical: str,
        task_type: str | None,
        anchor_id: str | None,
        adaptation_changes: dict[str, Any],
    ) -> None:
        ttype = task_type or "-"
        threshold_delta = adaptation_changes.get("threshold_delta")
        td_str = f" Δthr={float(threshold_delta):+.3f}" if isinstance(threshold_delta, (int, float)) else ""
        suppressed = adaptation_changes.get("task_type_suppressed_until")
        sup_str = f" suppressed_until={suppressed}" if suppressed else ""
        cooldown = adaptation_changes.get("anchor_cooldown_set")
        cd_str = f" cooldown_set={cooldown}" if cooldown else ""
        line = (
            f"[proactive][feedback] {decision_id} {surface} {canonical} "
            f"task={ttype} anchor={anchor_id or '-'}{td_str}{cd_str}{sup_str}"
        )
        self._emit(line)

    def null_outcome(
        self,
        *,
        decision_id: str,
        outcome: str,
        edit_volume: float,
        churn: float,
        idle: float,
    ) -> None:
        line = (
            f"[proactive][null_outcome] {decision_id} {outcome} "
            f"vol={edit_volume:.0f} churn={churn:.2f} idle={idle:.1f}s"
        )
        self._emit(line)

    def custom(self, tag: str, message: str) -> None:
        self._emit(f"[proactive][{tag}] {message}")


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
    key = str(Path(log_dir).resolve())
    with _cache_lock:
        instance = _telemetry_cache.pop(key, None)
    if instance is not None:
        instance.close()


def console_enabled() -> bool:
    return _console_enabled()
