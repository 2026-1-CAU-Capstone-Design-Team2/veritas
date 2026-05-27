"""Per-scenario preference weights learned from user feedback.

A non-contextual EMA on rewards from intervention_feedback.jsonl: each
reaction (paste / like / dislike) pulls the chosen scenario's weight toward
the reward target. Weights persist across sessions in a single JSON file
and clip to [WEIGHT_CLIP_MIN, WEIGHT_CLIP_MAX] so a few bad ratings cannot
bury a scenario forever (cold-start guard).

Reward scale (callers MUST follow):
    1.0 = neutral (no movement when weight is already 1.0)
    > 1.0 = positive (pulls weight up; reward 2.0 saturates at clip_max)
    < 1.0 = negative (pulls weight down; reward 0.0 saturates at clip_min)
With this scale the EMA converges to the user's average reward for the
scenario, so the weight has a clean operational meaning ("how much this
user likes this intervention type") rather than a drifting counter.

The combination of weight × LLM router confidence happens in
intervention_detector._route_with_llm — this module owns only the weight
itself, not the combination policy. Reward mapping (reaction → float) lives
at the call site so it can be swapped without touching the store.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from ..core.store import ScreenContextStore


DEFAULT_ALPHA = 0.2
DEFAULT_INITIAL_WEIGHT = 1.0
WEIGHT_CLIP_MIN = 0.5
WEIGHT_CLIP_MAX = 1.5


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


class PreferenceStore:
    """EMA weight per scenario name, persisted as a single JSON snapshot.

    Thread-safe (RLock). Updates are O(1); flush writes the whole snapshot
    atomically via ScreenContextStore.save_preference_state.
    """

    def __init__(
        self,
        store: ScreenContextStore,
        *,
        alpha: float | None = None,
        initial_weight: float = DEFAULT_INITIAL_WEIGHT,
        clip_min: float = WEIGHT_CLIP_MIN,
        clip_max: float = WEIGHT_CLIP_MAX,
    ) -> None:
        if clip_min > clip_max:
            raise ValueError("clip_min must be <= clip_max")
        resolved_alpha = alpha if alpha is not None else _env_float("VERITAS_PREFERENCE_ALPHA", DEFAULT_ALPHA)
        if not 0.0 < resolved_alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self.store = store
        self.alpha = resolved_alpha
        self.initial_weight = initial_weight
        self.clip_min = clip_min
        self.clip_max = clip_max
        self._weights: dict[str, float] = {}
        self._update_count: dict[str, int] = {}
        self._last_updated_at: float = 0.0
        self._lock = threading.RLock()
        self._load()

    def weight(self, scenario: str) -> float:
        """Current weight for ``scenario``. Returns initial_weight for unseen names."""
        with self._lock:
            return self._weights.get(scenario, self.initial_weight)

    def update(self, scenario: str, reward: float) -> float:
        """Apply one EMA step: weight = (1-α) * weight + α * reward, then clip.

        Returns the post-update weight. Caller maps reaction → reward upstream.
        """
        with self._lock:
            current = self._weights.get(scenario, self.initial_weight)
            updated = (1.0 - self.alpha) * current + self.alpha * float(reward)
            if updated < self.clip_min:
                updated = self.clip_min
            elif updated > self.clip_max:
                updated = self.clip_max
            self._weights[scenario] = updated
            self._update_count[scenario] = self._update_count.get(scenario, 0) + 1
            self._last_updated_at = time.time()
            return updated

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "weights": dict(self._weights),
                "update_count": dict(self._update_count),
                "alpha": self.alpha,
                "initial_weight": self.initial_weight,
                "clip_min": self.clip_min,
                "clip_max": self.clip_max,
                "last_updated_at": self._last_updated_at,
            }

    def flush(self) -> None:
        """Persist snapshot to disk via the store."""
        self.store.save_preference_state(self.snapshot())

    def reset(self) -> None:
        """Forget all learned weights — every scenario reverts to initial_weight."""
        with self._lock:
            self._weights.clear()
            self._update_count.clear()
            self._last_updated_at = time.time()

    def replay_from_feedback(self, *, reward_for) -> int:
        """Rebuild weights from intervention_feedback.jsonl from scratch.

        ``reward_for`` is a callable ``record -> float | None``; returning
        None skips the record (e.g. unknown reaction, missing scenario).
        Returns the number of update steps applied. Does not flush — caller
        decides whether to persist the rebuilt state.
        """
        applied = 0
        self.reset()
        for record in self.store.iter_intervention_feedback():
            scenario = record.get("intervention_type")
            if not isinstance(scenario, str) or not scenario:
                continue
            reward = reward_for(record)
            if reward is None:
                continue
            self.update(scenario, float(reward))
            applied += 1
        return applied

    def _load(self) -> None:
        payload = self.store.load_preference_state()
        if not payload:
            return
        weights = payload.get("weights") or {}
        counts = payload.get("update_count") or {}
        with self._lock:
            if isinstance(weights, dict):
                for name, value in weights.items():
                    try:
                        self._weights[str(name)] = float(value)
                    except (TypeError, ValueError):
                        continue
            if isinstance(counts, dict):
                for name, value in counts.items():
                    try:
                        self._update_count[str(name)] = int(value)
                    except (TypeError, ValueError):
                        continue
            try:
                self._last_updated_at = float(payload.get("last_updated_at") or 0.0)
            except (TypeError, ValueError):
                self._last_updated_at = 0.0
