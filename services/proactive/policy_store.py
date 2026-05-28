"""Per-workspace persistence for the proactive bandit.

Layout (mirrors the existing ``runs/<workspace_id>/`` convention used by the
research / verification / chat pipelines so reset/cleanup logic stays uniform)::

    runs/<workspace_id>/proactive_policy/
        policy_state.json          ← single-document policy state (overwrite)
        decisions.jsonl            ← every observe() result, append-only
        feedback.jsonl             ← every record_feedback() call
        updates.jsonl              ← every applied policy update
        feature_snapshots.jsonl    ← reserved (orchestrator already inlines
                                     features in decisions.jsonl; this file
                                     stays available for debug dumps)
        pending_timeouts.jsonl     ← timeout monitor working set

Invariants enforced here:

1. ``policy_state.json`` never contains raw document text. The orchestrator
   keeps that in an in-memory cache keyed by ``decision_id``.
2. ``decisions.jsonl`` records the feature vector + primitives so a later
   audit can reproduce the policy state from the log alone.
3. All writes are crash-safe: ``policy_state.json`` is written atomically via
   ``write→fsync→rename``; the JSONL files use append-then-flush.
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

from .features import ENGAGE_FEATURE_NAMES, SUGGEST_FEATURE_NAMES
from .action_space import SUGGESTION_ACTIONS
from .policies import ActionCenteredEngagePolicy, DisjointDiscountedLinUCB

POLICY_STATE_VERSION = 2


def _env_float(name: str, default: float, *, lo: float, hi: float) -> float:
    """Read a float env var clamped to [lo, hi]; fall back to default on parse error.

    These overrides let an operator un-stick an over-conservative engage policy
    without editing code. Used at PolicyStore construction time so a workspace
    boot picks them up. Values written through here are stored *with* the
    policy state so subsequent reloads honor them even if the env disappears.
    """
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return max(lo, min(hi, v))


def _engage_overrides() -> dict[str, float]:
    """``VERITAS_PROACTIVE_PI_MIN`` / ``..._PI_MAX`` / ``..._DISCOUNT`` /
    ``..._WARMUP_DECISIONS`` / ``..._WARMUP_FLOOR`` overrides for the engage
    policy. Each is clamped to a sane range so a stray value can't break the
    math (pi_min must be < pi_max, etc.)."""
    pi_min = _env_float("VERITAS_PROACTIVE_PI_MIN", 0.05, lo=0.01, hi=0.50)
    pi_max = _env_float("VERITAS_PROACTIVE_PI_MAX", 0.60, lo=0.10, hi=0.95)
    if pi_max <= pi_min:
        pi_max = min(0.95, pi_min + 0.10)
    warmup_decisions = int(
        _env_float("VERITAS_PROACTIVE_WARMUP_DECISIONS", 20.0, lo=0.0, hi=500.0)
    )
    warmup_floor = _env_float(
        "VERITAS_PROACTIVE_WARMUP_FLOOR", 0.30, lo=0.0, hi=0.95
    )
    return {
        "pi_min": pi_min,
        "pi_max": pi_max,
        "discount": _env_float("VERITAS_PROACTIVE_DISCOUNT", 0.995, lo=0.80, hi=1.0),
        "warmup_decisions": float(warmup_decisions),
        "warmup_pi_floor": warmup_floor,
    }


def _suggest_overrides() -> dict[str, float]:
    return {
        "discount": _env_float(
            "VERITAS_PROACTIVE_SUGGEST_DISCOUNT", 0.99, lo=0.80, hi=1.0
        ),
        "alpha": _env_float("VERITAS_PROACTIVE_ALPHA", 0.5, lo=0.0, hi=5.0),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON to ``path`` atomically.

    Avoids the classic "interpreter crash leaves a half-written file" failure
    mode — important here because ``policy_state.json`` is the *only* place
    the bandit's learned state lives.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".policy_state-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                # Some Windows filesystems refuse fsync on text-mode handles;
                # the os.replace below is still atomic on NTFS.
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _safe_workspace_id(workspace_id: str) -> str:
    ws = str(workspace_id or "").strip()
    return ws or "default"


class PolicyStore:
    """Owns one workspace's bandit state.

    Constructed by the orchestrator at workspace bind time. Thread-safe across
    the orchestrator + timeout monitor sweep: a single ``RLock`` serializes
    all reads and writes to the in-memory state (the I/O itself is fine to
    interleave).
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
        self._engage: ActionCenteredEngagePolicy
        self._suggest: DisjointDiscountedLinUCB
        self._user_stats: dict[str, Any] = {}
        self._load_or_init()

    # ----------------------------------------------------------- paths

    @property
    def state_path(self) -> Path:
        return self.policy_dir / "policy_state.json"

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
    def pending_timeouts_path(self) -> Path:
        return self.policy_dir / "pending_timeouts.jsonl"

    # ----------------------------------------------------------- init/load

    def _default_user_stats(self) -> dict[str, Any]:
        return {
            "recent_negative_rate": 0.0,
            "recent_positive_rate": 0.0,
            "last_intervention_at": None,
            "last_feedback_at": None,
            "counts": {
                "accept": 0,
                "reject": 0,
                "retry": 0,
                "timeout": 0,
                "cancelled": 0,
                "noop_positive": 0,
                "noop_negative": 0,
            },
        }

    def _fresh_policies(self) -> tuple[ActionCenteredEngagePolicy, DisjointDiscountedLinUCB]:
        engage_kwargs = _engage_overrides()
        engage = ActionCenteredEngagePolicy(
            feature_names=list(ENGAGE_FEATURE_NAMES),
            pi_min=float(engage_kwargs["pi_min"]),
            pi_max=float(engage_kwargs["pi_max"]),
            discount=float(engage_kwargs["discount"]),
            warmup_decisions=int(engage_kwargs["warmup_decisions"]),
            warmup_pi_floor=float(engage_kwargs["warmup_pi_floor"]),
        )
        suggest_kwargs = _suggest_overrides()
        suggest = DisjointDiscountedLinUCB(
            actions=list(SUGGESTION_ACTIONS),
            feature_names=list(SUGGEST_FEATURE_NAMES),
            alpha=float(suggest_kwargs["alpha"]),
            discount=float(suggest_kwargs["discount"]),
        )
        return engage, suggest

    def _load_or_init(self) -> None:
        with self._lock:
            if not self.state_path.exists():
                self._engage, self._suggest = self._fresh_policies()
                self._user_stats = self._default_user_stats()
                self._save_locked()
                return
            try:
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                # Corrupt state file — keep a sidecar copy for debugging,
                # then start fresh rather than crashing the runtime.
                broken = self.state_path.with_suffix(".broken.json")
                try:
                    self.state_path.replace(broken)
                except OSError:
                    pass
                self._engage, self._suggest = self._fresh_policies()
                self._user_stats = self._default_user_stats()
                self._save_locked()
                return

            engage_payload = payload.get("engage_policy") or {}
            suggest_payload = payload.get("suggestion_policy") or {}
            self._engage = (
                ActionCenteredEngagePolicy.from_payload(engage_payload)
                if engage_payload.get("feature_names")
                else self._fresh_policies()[0]
            )
            self._suggest = (
                DisjointDiscountedLinUCB.from_payload(suggest_payload)
                if suggest_payload.get("actions")
                else self._fresh_policies()[1]
            )
            # Apply env-var overrides on top of the loaded state. This is the
            # un-stick lever: a workspace that learned to be over-conservative
            # (pi_min=0.05 after a streak of rejects) can have its floor lifted
            # by VERITAS_PROACTIVE_PI_MIN=0.20 without losing the learned θ_hat.
            # Warmup params can also be raised to re-enter forced exploration
            # without resetting the learned state — useful when the operator
            # added new content and wants to see suggestions again.
            engage_kwargs = _engage_overrides()
            self._engage.pi_min = float(engage_kwargs["pi_min"])
            self._engage.pi_max = float(engage_kwargs["pi_max"])
            self._engage.discount = float(engage_kwargs["discount"])
            self._engage.warmup_decisions = int(engage_kwargs["warmup_decisions"])
            self._engage.warmup_pi_floor = float(engage_kwargs["warmup_pi_floor"])
            suggest_kwargs = _suggest_overrides()
            self._suggest.discount = float(suggest_kwargs["discount"])
            self._suggest.alpha = float(suggest_kwargs["alpha"])
            stats = payload.get("user_stats") or {}
            defaults = self._default_user_stats()
            defaults.update(stats)
            # ensure counts dict has every canonical bucket even after a
            # version bump introduced new ones (forward compat).
            count_defaults = self._default_user_stats()["counts"]
            count_defaults.update(stats.get("counts") or {})
            defaults["counts"] = count_defaults
            self._user_stats = defaults

    # ----------------------------------------------------------- accessors

    @property
    def engage_policy(self) -> ActionCenteredEngagePolicy:
        return self._engage

    @property
    def suggestion_policy(self) -> DisjointDiscountedLinUCB:
        return self._suggest

    def get_user_stats(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._user_stats))  # deep copy

    # ----------------------------------------------------------- save

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        payload = {
            "version": POLICY_STATE_VERSION,
            "workspace_id": self.workspace_id,
            "updated_at": _now_iso(),
            "engage_policy": self._engage.to_payload(),
            "suggestion_policy": self._suggest.to_payload(),
            "user_stats": self._user_stats,
        }
        _atomic_write_json(self.state_path, payload)

    # ----------------------------------------------------------- mutation

    def apply_feedback_to_stats(
        self,
        *,
        canonical: str,
        intervention_recorded_at: str | None = None,
        alpha: float = 0.15,
    ) -> None:
        """Update the EMA user stats and bump the bucket counts.

        ``alpha`` is the EMA learning rate — small enough that one outlier
        doesn't dominate, large enough to track a clear behavioral shift
        within a session.
        """
        with self._lock:
            counts = self._user_stats.setdefault(
                "counts", self._default_user_stats()["counts"]
            )
            counts[canonical] = int(counts.get(canonical, 0)) + 1

            pos = 1.0 if canonical in ("accept", "noop_positive") else 0.0
            neg = 1.0 if canonical in ("reject", "timeout", "cancelled", "noop_negative") else 0.0
            self._user_stats["recent_positive_rate"] = (
                (1 - alpha) * float(self._user_stats.get("recent_positive_rate", 0.0))
                + alpha * pos
            )
            self._user_stats["recent_negative_rate"] = (
                (1 - alpha) * float(self._user_stats.get("recent_negative_rate", 0.0))
                + alpha * neg
            )
            self._user_stats["last_feedback_at"] = _now_iso()
            if intervention_recorded_at is not None:
                self._user_stats["last_intervention_at"] = intervention_recorded_at

    def mark_intervention(self, *, when_iso: str | None = None) -> None:
        with self._lock:
            self._user_stats["last_intervention_at"] = when_iso or _now_iso()

    # ----------------------------------------------------------- reset

    def reset(self) -> dict[str, Any]:
        """Drop the learned engage / suggest state and start fresh.

        Keeps the workspace dir intact (decisions.jsonl / feedback.jsonl are
        history and stay around for audit); only ``policy_state.json`` is
        replaced. Use when the bandit has learned to be too conservative and
        the operator wants to give it another chance from a clean prior.
        """
        with self._lock:
            self._engage, self._suggest = self._fresh_policies()
            self._user_stats = self._default_user_stats()
            self._save_locked()
        return {
            "workspaceId": self.workspace_id,
            "engage": {
                "pi_min": self._engage.pi_min,
                "pi_max": self._engage.pi_max,
                "discount": self._engage.discount,
            },
            "suggestion": {
                "discount": self._suggest.discount,
                "alpha": self._suggest.alpha,
            },
        }

    # ----------------------------------------------------------- inspect

    def snapshot(self) -> dict[str, Any]:
        """Compact human-readable summary of where the policy is right now —
        the operator's go-to for "why does it keep no-op-ing on me?" """
        with self._lock:
            engage = self._engage
            suggest = self._suggest
            return {
                "workspaceId": self.workspace_id,
                "engage": {
                    "pi_min": engage.pi_min,
                    "pi_max": engage.pi_max,
                    "discount": engage.discount,
                    "theta_hat": list(engage.theta_hat),
                    "counts": dict(engage._counts),
                    "feature_names": list(engage.feature_names),
                    "warmup_decisions": engage.warmup_decisions,
                    "warmup_pi_floor": engage.warmup_pi_floor,
                    "total_decisions": engage._total_decisions,
                    "warmup_remaining": max(
                        0, engage.warmup_decisions - engage._total_decisions
                    ),
                    "warmup_active": engage._total_decisions < engage.warmup_decisions,
                },
                "suggestion": {
                    "actions": list(suggest.actions),
                    "discount": suggest.discount,
                    "alpha": suggest.alpha,
                    "counts": dict(suggest._counts),
                },
                "user_stats": json.loads(json.dumps(self._user_stats)),
            }

    # ----------------------------------------------------------- JSONL log

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=_to_jsonable)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")
            fh.flush()

    def log_decision(self, record: dict[str, Any]) -> None:
        self._append_jsonl(self.decisions_path, record)

    def log_feedback(self, record: dict[str, Any]) -> None:
        self._append_jsonl(self.feedback_path, record)

    def log_update(self, record: dict[str, Any]) -> None:
        self._append_jsonl(self.updates_path, record)

    # ----------------------------------------------------------- pending

    def write_pending_timeouts(self, records: list[dict[str, Any]]) -> None:
        """Overwrite the working set of pending timeouts.

        The timeout monitor reads/writes the entire file under the
        store lock — there are usually only a handful of pending
        decisions at a time, so a rewrite is simpler than line edits.
        """
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
                        # Skip corrupted line, keep going.
                        continue
            return out
