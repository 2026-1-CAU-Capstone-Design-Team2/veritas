"""Per-workspace JSONL logging + adaptation glue.

Renamed in spirit from the bandit-era "policy state" store — the only
*learned* state now lives in :mod:`adaptation.UserAdaptationMemory`. This
module is reduced to the append-only logs and the directory layout:

    runs/<workspace_id>/proactive_policy/
        user_adaptation.json     ← managed by adaptation.py (atomic write)
        decisions.jsonl          ← every observe() result, append-only
        feedback.jsonl           ← every record_feedback() call
        updates.jsonl            ← every adaptation change applied
        null_outcomes.jsonl      ← null_outcome_monitor's classifications
        pending_timeouts.jsonl   ← timeout monitor working set
        proactive.log            ← telemetry timeline (managed by telemetry.py)

Invariants (unchanged across the pivot):

1. ``decisions.jsonl`` carries the gate reasons / evaluator score / char
   counts but **never** raw text.
2. All writes are crash-safe (append-flush for JSONL; the adaptation file
   uses its own atomic write in :mod:`adaptation`).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adaptation import UserAdaptationMemory


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def _safe_workspace_id(workspace_id: str) -> str:
    ws = str(workspace_id or "").strip()
    return ws or "default"


class ProactiveStore:
    """Owns the per-workspace logging directory + the adaptation memory.

    Constructed by the orchestrator at workspace bind. Thread-safe via the
    adaptation memory's own RLock for adaptation writes; the JSONL append
    operations are append-flush (POSIX guarantees atomic short writes).
    """

    def __init__(self, *, output_root: Path, workspace_id: str) -> None:
        self.output_root = Path(output_root)
        self.workspace_id = _safe_workspace_id(workspace_id)
        if self.workspace_id == "default":
            self.workspace_dir = self.output_root / "api"
        else:
            self.workspace_dir = self.output_root / self.workspace_id
        self.policy_dir = self.workspace_dir / "proactive_policy"
        self.policy_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self.adaptation = UserAdaptationMemory(
            workspace_dir=self.workspace_dir,
            workspace_id=self.workspace_id,
        )

    # ----------------------------------------------------------- paths

    @property
    def decisions_path(self) -> Path:
        return self.policy_dir / "decisions.jsonl"

    @property
    def feedback_path(self) -> Path:
        return self.policy_dir / "feedback.jsonl"

    @property
    def updates_path(self) -> Path:
        return self.policy_dir / "updates.jsonl"

    @property
    def null_outcomes_path(self) -> Path:
        return self.policy_dir / "null_outcomes.jsonl"

    @property
    def pending_timeouts_path(self) -> Path:
        return self.policy_dir / "pending_timeouts.jsonl"

    # ----------------------------------------------------------- JSONL

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=_to_jsonable)
        with self._lock, path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")
            fh.flush()

    def log_decision(self, record: dict[str, Any]) -> None:
        self._append_jsonl(self.decisions_path, record)

    def log_feedback(self, record: dict[str, Any]) -> None:
        self._append_jsonl(self.feedback_path, record)

    def log_update(self, record: dict[str, Any]) -> None:
        self._append_jsonl(self.updates_path, record)

    def log_null_outcome(self, record: dict[str, Any]) -> None:
        self._append_jsonl(self.null_outcomes_path, record)

    # ----------------------------------------------------------- pending

    def write_pending_timeouts(self, records: list[dict[str, Any]]) -> None:
        with self._lock:
            self.pending_timeouts_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.pending_timeouts_path.with_suffix(".jsonl.tmp")
            with tmp_path.open("w", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(json.dumps(rec, ensure_ascii=False, default=_to_jsonable))
                    fh.write("\n")
                try:
                    fh.flush()
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, self.pending_timeouts_path)

    def read_pending_timeouts(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.pending_timeouts_path.exists():
                return []
            out: list[dict[str, Any]] = []
            with self.pending_timeouts_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
            return out

    # ----------------------------------------------------------- admin

    def reset(self) -> dict[str, Any]:
        """Clear adaptation state. JSONL audit logs are preserved."""
        with self._lock:
            result = self.adaptation.reset()
            self.write_pending_timeouts([])
            return result

    def snapshot(self) -> dict[str, Any]:
        """Compact human-readable summary the operator can read at a glance."""
        return {
            "workspaceId": self.workspace_id,
            "adaptation": self.adaptation.get_state_snapshot(),
            "pending_timeouts": self.read_pending_timeouts(),
        }
