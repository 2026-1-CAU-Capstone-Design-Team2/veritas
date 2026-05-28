"""UserAdaptationMemory — lightweight feedback-driven thresholds and cooldowns.

This module replaces the bandit's online parameter learning. Instead of
updating a θ̂ vector, every feedback updates:

- **Global EMA stats** (accept/reject/retry/timeout rates)
- **Per-task-type counts + suppression-until timestamps**
- **Per-(anchor, task) cooldown timestamps**
- **A global ``threshold_offset`` that nudges the show-threshold up/down**

Persistence: atomic JSON write to
``runs/<workspace_id>/proactive_policy/user_adaptation.json``. The shape is
designed so it can be read by hand — no matrix dumps, no payload schemas.

Feedback rules (§7.1) are intentionally asymmetric:

- ``accept`` mildly lowers threshold, clears the anchor/task cooldown.
- ``reject`` raises threshold, sets a per-(anchor, task) cooldown, and
  ratchets up a per-task-type counter. After ``N_REJECTS_FOR_SUPPRESSION``
  rejects within the rolling window the task type is suppressed for a
  multi-minute window.
- ``retry`` updates a retry EMA and a prompt_style flag — does *not*
  penalize the task type. The user wanted the help, just not the form.
- ``timeout`` mildly nudges threshold but doesn't set anchor cooldowns
  (the user might come back).
- ``wrong_anchor`` blames the *anchor extraction*, not the user's
  preference. Logged separately and does NOT touch task_type stats.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


ADAPTATION_FILE = "user_adaptation.json"
ADAPTATION_VERSION = 1


# Feedback shaping constants — kept here so an operator can read them at a
# glance and tune via the env overrides below if needed.
EMA_ALPHA: float = 0.20
THRESHOLD_OFFSET_MIN: float = -0.10
THRESHOLD_OFFSET_MAX: float = +0.20
THRESHOLD_DELTA_ACCEPT: float = -0.015
THRESHOLD_DELTA_REJECT: float = +0.030
THRESHOLD_DELTA_TIMEOUT: float = +0.010
THRESHOLD_DELTA_WRONG_ANCHOR: float = +0.005

ANCHOR_COOLDOWN_REJECT_SECONDS: float = 180.0       # 3 minutes
ANCHOR_COOLDOWN_TIMEOUT_SECONDS: float = 60.0        # 1 minute
TASK_TYPE_REJECTS_FOR_SUPPRESSION: int = 3
TASK_TYPE_SUPPRESSION_SECONDS: float = 600.0         # 10 minutes
TASK_TYPE_REJECT_WINDOW_SECONDS: float = 900.0       # 15 minutes — rolling


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat().replace("+00:00", "Z")


def _iso_plus(seconds: float) -> str:
    return (_now() + timedelta(seconds=float(seconds))).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


# ----------------------------------------------------------- data shapes


@dataclass
class FeedbackStats:
    """Rolling EMA of canonical feedback rates. ``recent_negative_rate`` is
    the operator-facing "how cranky has the user been lately?" number."""

    accept_ema: float = 0.0
    reject_ema: float = 0.0
    retry_ema: float = 0.0
    timeout_ema: float = 0.0
    recent_negative_rate: float = 0.0


@dataclass
class TaskTypeStats:
    """Per-task-type bookkeeping. ``recent_reject_iso`` is a small ring of
    timestamps (kept as a list of ISO strings to stay JSON-friendly) used to
    decide suppression — once N of them fall inside the rolling window the
    type is suppressed."""

    accept: int = 0
    reject: int = 0
    retry: int = 0
    timeout: int = 0
    wrong_anchor: int = 0
    recent_reject_iso: list[str] = field(default_factory=list)
    suppressed_until: Optional[str] = None


@dataclass
class AnchorCooldown:
    """Suppression of a single (anchor_id, task_type) pair until a time."""

    cooldown_until: str
    reason: str


@dataclass
class UserAdaptationState:
    """The full persisted state. Atomic write target."""

    workspace_id: str = ""
    version: int = ADAPTATION_VERSION
    updated_at: str = ""
    global_stats: FeedbackStats = field(default_factory=FeedbackStats)
    task_type_stats: dict[str, TaskTypeStats] = field(default_factory=dict)
    anchor_cooldowns: dict[str, AnchorCooldown] = field(default_factory=dict)
    threshold_offset: float = 0.0
    last_intervention_at: Optional[str] = None
    last_feedback_at: Optional[str] = None
    # Free-form prompt style flags driven by `retry` feedback. The generator
    # may consult these to bias toward shorter / preserve-original outputs.
    prompt_style_flags: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------- (de)serialize


def _state_to_dict(state: UserAdaptationState) -> dict[str, Any]:
    d = asdict(state)
    # asdict recurses into dataclasses; nothing else to do.
    return d


def _state_from_dict(data: dict[str, Any]) -> UserAdaptationState:
    gs = data.get("global_stats") or {}
    if not isinstance(gs, dict):
        gs = {}
    tts_raw = data.get("task_type_stats") or {}
    tts: dict[str, TaskTypeStats] = {}
    if isinstance(tts_raw, dict):
        for k, v in tts_raw.items():
            if isinstance(v, dict):
                tts[k] = TaskTypeStats(
                    accept=int(v.get("accept", 0)),
                    reject=int(v.get("reject", 0)),
                    retry=int(v.get("retry", 0)),
                    timeout=int(v.get("timeout", 0)),
                    wrong_anchor=int(v.get("wrong_anchor", 0)),
                    recent_reject_iso=list(v.get("recent_reject_iso", []) or []),
                    suppressed_until=v.get("suppressed_until") or None,
                )
    cd_raw = data.get("anchor_cooldowns") or {}
    cooldowns: dict[str, AnchorCooldown] = {}
    if isinstance(cd_raw, dict):
        for k, v in cd_raw.items():
            if isinstance(v, dict) and v.get("cooldown_until"):
                cooldowns[k] = AnchorCooldown(
                    cooldown_until=str(v.get("cooldown_until") or ""),
                    reason=str(v.get("reason") or ""),
                )
    return UserAdaptationState(
        workspace_id=str(data.get("workspace_id") or ""),
        version=int(data.get("version", ADAPTATION_VERSION)),
        updated_at=str(data.get("updated_at") or ""),
        global_stats=FeedbackStats(
            accept_ema=float(gs.get("accept_ema", 0.0)),
            reject_ema=float(gs.get("reject_ema", 0.0)),
            retry_ema=float(gs.get("retry_ema", 0.0)),
            timeout_ema=float(gs.get("timeout_ema", 0.0)),
            recent_negative_rate=float(gs.get("recent_negative_rate", 0.0)),
        ),
        task_type_stats=tts,
        anchor_cooldowns=cooldowns,
        threshold_offset=float(data.get("threshold_offset", 0.0)),
        last_intervention_at=data.get("last_intervention_at") or None,
        last_feedback_at=data.get("last_feedback_at") or None,
        prompt_style_flags=dict(data.get("prompt_style_flags") or {}),
    )


# ----------------------------------------------------------- IO


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".adaptation-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ----------------------------------------------------------- memory


class UserAdaptationMemory:
    """Per-workspace adaptation store.

    Constructed by the orchestrator at workspace bind. Thread-safe via a
    single ``RLock``.
    """

    def __init__(self, *, workspace_dir: Path, workspace_id: str) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.workspace_id = workspace_id
        self.path = self.workspace_dir / "proactive_policy" / ADAPTATION_FILE
        self._lock = threading.RLock()
        self._state: UserAdaptationState
        self._load_or_init()

    # ---------------------------------------------------- accessors

    @property
    def state(self) -> UserAdaptationState:
        with self._lock:
            return self._state

    def get_state_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return _state_to_dict(self._state)

    # ---------------------------------------------------- load/save

    def _load_or_init(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._state = UserAdaptationState(workspace_id=self.workspace_id)
                self._save_locked()
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                broken = self.path.with_suffix(".broken.json")
                try:
                    self.path.replace(broken)
                except OSError:
                    pass
                self._state = UserAdaptationState(workspace_id=self.workspace_id)
                self._save_locked()
                return
            self._state = _state_from_dict(data)
            self._state.workspace_id = self.workspace_id

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        self._state.updated_at = _now_iso()
        _atomic_write_json(self.path, _state_to_dict(self._state))

    def reset(self) -> dict[str, Any]:
        """Wipe learned adaptation but keep history (decisions.jsonl etc.)."""
        with self._lock:
            self._state = UserAdaptationState(workspace_id=self.workspace_id)
            self._save_locked()
            return _state_to_dict(self._state)

    # ---------------------------------------------------- feedback

    def apply_feedback(
        self,
        *,
        canonical: str,
        task_type: Optional[str],
        anchor_id: Optional[str],
    ) -> dict[str, Any]:
        """Single entry point for canonical feedback. Returns a dict
        summarizing what changed — used by the orchestrator's update log."""
        with self._lock:
            changes: dict[str, Any] = {
                "canonical": canonical,
                "task_type": task_type,
                "anchor_id": anchor_id,
                "threshold_offset_before": self._state.threshold_offset,
            }

            # EMA update is uniform across all canonical values.
            self._update_ema(canonical)
            self._state.last_feedback_at = _now_iso()

            if canonical == "accept":
                self._apply_accept(task_type, anchor_id, changes)
            elif canonical == "reject":
                self._apply_reject(task_type, anchor_id, changes)
            elif canonical == "retry":
                self._apply_retry(task_type, changes)
            elif canonical == "timeout":
                self._apply_timeout(task_type, anchor_id, changes)
            elif canonical == "wrong_anchor":
                self._apply_wrong_anchor(task_type, changes)
            elif canonical == "cancelled":
                # No state change beyond EMA — the user simply moved on.
                changes["note"] = "cancelled: no adaptation update"
            else:
                changes["note"] = f"unhandled canonical={canonical}"

            self._clamp_threshold_offset()
            changes["threshold_offset_after"] = self._state.threshold_offset
            self._save_locked()
            return changes

    def mark_intervention_shown(self, *, when_iso: Optional[str] = None) -> None:
        with self._lock:
            self._state.last_intervention_at = when_iso or _now_iso()
            self._save_locked()

    # ---------------------------------------------------- per-canonical

    def _ensure_task_stats(self, task_type: str) -> TaskTypeStats:
        if task_type not in self._state.task_type_stats:
            self._state.task_type_stats[task_type] = TaskTypeStats()
        return self._state.task_type_stats[task_type]

    def _apply_accept(
        self,
        task_type: Optional[str],
        anchor_id: Optional[str],
        changes: dict[str, Any],
    ) -> None:
        if task_type:
            self._ensure_task_stats(task_type).accept += 1
        self._state.threshold_offset += THRESHOLD_DELTA_ACCEPT
        changes["threshold_delta"] = THRESHOLD_DELTA_ACCEPT
        # Clear the matching anchor/task cooldown if any.
        if anchor_id and task_type:
            key = f"{anchor_id}|{task_type}"
            if key in self._state.anchor_cooldowns:
                del self._state.anchor_cooldowns[key]
                changes["cleared_cooldown"] = key

    def _apply_reject(
        self,
        task_type: Optional[str],
        anchor_id: Optional[str],
        changes: dict[str, Any],
    ) -> None:
        if task_type:
            stats = self._ensure_task_stats(task_type)
            stats.reject += 1
            stats.recent_reject_iso.append(_now_iso())
            self._gc_recent_rejects(stats)
            if len(stats.recent_reject_iso) >= TASK_TYPE_REJECTS_FOR_SUPPRESSION:
                stats.suppressed_until = _iso_plus(TASK_TYPE_SUPPRESSION_SECONDS)
                changes["task_type_suppressed_until"] = stats.suppressed_until

        if anchor_id and task_type:
            key = f"{anchor_id}|{task_type}"
            self._state.anchor_cooldowns[key] = AnchorCooldown(
                cooldown_until=_iso_plus(ANCHOR_COOLDOWN_REJECT_SECONDS),
                reason="reject",
            )
            changes["anchor_cooldown_set"] = key

        self._state.threshold_offset += THRESHOLD_DELTA_REJECT
        changes["threshold_delta"] = THRESHOLD_DELTA_REJECT

    def _apply_retry(
        self,
        task_type: Optional[str],
        changes: dict[str, Any],
    ) -> None:
        if task_type:
            self._ensure_task_stats(task_type).retry += 1
        # Bias the next prompt toward shorter / preserve original meaning.
        self._state.prompt_style_flags.setdefault("recent_retry_count", 0)
        self._state.prompt_style_flags["recent_retry_count"] = (
            int(self._state.prompt_style_flags["recent_retry_count"]) + 1
        )
        self._state.prompt_style_flags["prefer_shorter"] = True
        self._state.prompt_style_flags["preserve_original_meaning"] = True
        changes["prompt_style_flags"] = dict(self._state.prompt_style_flags)
        # Deliberately NO threshold change — the user wanted help, just not
        # this rendition.

    def _apply_timeout(
        self,
        task_type: Optional[str],
        anchor_id: Optional[str],
        changes: dict[str, Any],
    ) -> None:
        if task_type:
            self._ensure_task_stats(task_type).timeout += 1
        if anchor_id and task_type:
            key = f"{anchor_id}|{task_type}"
            # Shorter cooldown than reject — user might come back.
            self._state.anchor_cooldowns[key] = AnchorCooldown(
                cooldown_until=_iso_plus(ANCHOR_COOLDOWN_TIMEOUT_SECONDS),
                reason="timeout",
            )
            changes["anchor_cooldown_set"] = key
        self._state.threshold_offset += THRESHOLD_DELTA_TIMEOUT
        changes["threshold_delta"] = THRESHOLD_DELTA_TIMEOUT

    def _apply_wrong_anchor(
        self,
        task_type: Optional[str],
        changes: dict[str, Any],
    ) -> None:
        # Critical: do NOT update task_type accept/reject stats here. The
        # anchor extraction is at fault, not the user's preference for this
        # task type.
        if task_type:
            self._ensure_task_stats(task_type).wrong_anchor += 1
        self._state.threshold_offset += THRESHOLD_DELTA_WRONG_ANCHOR
        changes["threshold_delta"] = THRESHOLD_DELTA_WRONG_ANCHOR
        changes["note"] = "wrong_anchor: not counted as task_type rejection"

    # ---------------------------------------------------- helpers

    def _update_ema(self, canonical: str) -> None:
        gs = self._state.global_stats
        a = float(EMA_ALPHA)
        accept = 1.0 if canonical == "accept" else 0.0
        reject = 1.0 if canonical == "reject" else 0.0
        retry = 1.0 if canonical == "retry" else 0.0
        timeout = 1.0 if canonical == "timeout" else 0.0
        gs.accept_ema = (1 - a) * gs.accept_ema + a * accept
        gs.reject_ema = (1 - a) * gs.reject_ema + a * reject
        gs.retry_ema = (1 - a) * gs.retry_ema + a * retry
        gs.timeout_ema = (1 - a) * gs.timeout_ema + a * timeout
        # recent_negative_rate is a smoothed combo of reject + timeout.
        neg_sample = 1.0 if canonical in ("reject", "timeout", "wrong_anchor") else 0.0
        gs.recent_negative_rate = (1 - a) * gs.recent_negative_rate + a * neg_sample

    def _gc_recent_rejects(self, stats: TaskTypeStats) -> None:
        cutoff = _now() - timedelta(seconds=TASK_TYPE_REJECT_WINDOW_SECONDS)
        kept: list[str] = []
        for iso in stats.recent_reject_iso:
            when = _parse_iso(iso)
            if when is not None and when >= cutoff:
                kept.append(iso)
        stats.recent_reject_iso = kept

    def _clamp_threshold_offset(self) -> None:
        self._state.threshold_offset = max(
            THRESHOLD_OFFSET_MIN, min(THRESHOLD_OFFSET_MAX, self._state.threshold_offset)
        )

    # ---------------------------------------------------- gc

    def garbage_collect(self) -> int:
        """Drop expired anchor cooldowns and stale recent_reject_iso entries.
        Returns the number of entries removed (operator diagnostic).
        Called from the orchestrator periodically (cheap, holds the lock
        briefly)."""
        with self._lock:
            removed = 0
            now = _now()
            expired = [
                k for k, v in self._state.anchor_cooldowns.items()
                if (when := _parse_iso(v.cooldown_until)) is None or when < now
            ]
            for k in expired:
                del self._state.anchor_cooldowns[k]
                removed += 1
            for stats in self._state.task_type_stats.values():
                before = len(stats.recent_reject_iso)
                self._gc_recent_rejects(stats)
                removed += before - len(stats.recent_reject_iso)
                # Clear expired suppression flags
                until = _parse_iso(stats.suppressed_until)
                if until is not None and until < now:
                    stats.suppressed_until = None
                    removed += 1
            if removed:
                self._save_locked()
            return removed
