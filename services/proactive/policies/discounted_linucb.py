"""Disjoint Discounted LinUCB for picking the suggestion-type candidate.

"Disjoint" = each action keeps its own (A_a, b_a) — the actions don't share a
parameter. "Discounted" = old observations decay via ``A_a = γ·A_a + xxᵀ`` so
the policy tracks non-stationary user preferences (e.g. the user is on a new
document type) instead of averaging forever.

Numpy-only, no scipy: ``A`` is small (~d×d where d=10 for the suggest vector)
so a per-action ``np.linalg.solve`` is cheap and stable. We never invert ``A``
directly; the spec writes ``inv(A) @ b`` for clarity, but the implementation
uses a linear solve so numerically-singular ``A`` doesn't poison the score.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np


class DisjointDiscountedLinUCB:
    """Per-arm LinUCB with a discount factor on the precision matrix.

    State per action ``a``:
        ``A_a``  : d×d ridge precision (init = ridge·I)
        ``b_a``  : d   reward-weighted feature sum

    Score:
        ``θ_a = A_a⁻¹ b_a``
        ``UCB_a(x) = θ_a·x + α · √(xᵀ A_a⁻¹ x)``

    Update (after observing reward ``r`` for action ``a`` at feature ``x``):
        ``A_a ← γ·A_a + x·xᵀ``
        ``b_a ← γ·b_a + r·x``
    """

    def __init__(
        self,
        actions: list[str],
        feature_names: list[str],
        *,
        alpha: float = 0.5,
        discount: float = 0.99,
        ridge: float = 1.0,
    ) -> None:
        if not actions:
            raise ValueError("actions must be non-empty")
        if not feature_names:
            raise ValueError("feature_names must be non-empty")
        self.actions: list[str] = list(actions)
        self.feature_names: list[str] = list(feature_names)
        self.dim: int = len(feature_names)
        self.alpha: float = float(alpha)
        self.discount: float = float(discount)
        self.ridge: float = float(ridge)
        self._A: dict[str, np.ndarray] = {
            a: np.eye(self.dim, dtype=np.float64) * self.ridge for a in self.actions
        }
        self._b: dict[str, np.ndarray] = {
            a: np.zeros(self.dim, dtype=np.float64) for a in self.actions
        }
        # Diagnostic: how many updates each arm has seen (decayed). Saved with
        # the payload so the operator can audit which arms are under-explored.
        self._counts: dict[str, float] = {a: 0.0 for a in self.actions}

    # ------------------------------------------------------------- select

    def _score(self, action: str, x_arr: np.ndarray) -> tuple[float, float, float]:
        A = self._A[action]
        b = self._b[action]
        try:
            theta = np.linalg.solve(A, b)
            ainv_x = np.linalg.solve(A, x_arr)
        except np.linalg.LinAlgError:
            # Fall back to pseudo-inverse if the matrix went singular — should
            # only happen with ridge=0 + a feature column that is always zero.
            ainv = np.linalg.pinv(A)
            theta = ainv @ b
            ainv_x = ainv @ x_arr
        mean = float(np.dot(theta, x_arr))
        variance = float(np.dot(x_arr, ainv_x))
        variance = max(variance, 0.0)
        ucb_bonus = self.alpha * math.sqrt(variance)
        return mean + ucb_bonus, mean, variance

    def select(
        self,
        x: list[float],
        available_actions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Pick the arm with the highest UCB score over ``available_actions``.

        ``available_actions`` is the capability mask from
        :func:`services.proactive.action_space.build_suggestion_action_mask`.
        Unknown actions in the mask are silently dropped (the policy may
        have been trained on a stricter action set than the current mask).
        """
        if len(x) != self.dim:
            raise ValueError(
                f"feature dim mismatch: expected {self.dim}, got {len(x)}"
            )
        candidates = list(available_actions) if available_actions else list(self.actions)
        candidates = [a for a in candidates if a in self._A]
        if not candidates:
            # No usable action — caller (orchestrator) treats this as "skip".
            return {
                "selected": None,
                "scores": {},
                "mean": {},
                "variance": {},
                "available": [],
            }

        x_arr = np.asarray(x, dtype=np.float64)
        scores: dict[str, float] = {}
        means: dict[str, float] = {}
        variances: dict[str, float] = {}
        for action in candidates:
            ucb, mean, variance = self._score(action, x_arr)
            scores[action] = ucb
            means[action] = mean
            variances[action] = variance

        selected = max(candidates, key=lambda a: scores[a])
        return {
            "selected": selected,
            "scores": scores,
            "mean": means,
            "variance": variances,
            "available": candidates,
        }

    # ------------------------------------------------------------- update

    def update(self, action: str, x: list[float], reward: float) -> None:
        if action not in self._A:
            # Unknown action — silently no-op; saves the orchestrator from
            # having to gate on whether an arm was added since the last save.
            return
        if len(x) != self.dim:
            raise ValueError(
                f"feature dim mismatch: expected {self.dim}, got {len(x)}"
            )
        x_arr = np.asarray(x, dtype=np.float64)
        self._A[action] = self.discount * self._A[action] + np.outer(x_arr, x_arr)
        self._b[action] = self.discount * self._b[action] + float(reward) * x_arr
        self._counts[action] = self.discount * self._counts[action] + 1.0

    # --------------------------------------------------------- (de)serialize

    def to_payload(self) -> dict[str, Any]:
        return {
            "algorithm": "disjoint_discounted_linucb",
            "actions": list(self.actions),
            "feature_names": list(self.feature_names),
            "alpha": self.alpha,
            "discount": self.discount,
            "ridge": self.ridge,
            "states": {
                action: {
                    "A": self._A[action].tolist(),
                    "b": self._b[action].tolist(),
                    "count": float(self._counts[action]),
                }
                for action in self.actions
            },
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DisjointDiscountedLinUCB":
        policy = cls(
            actions=list(payload.get("actions") or []),
            feature_names=list(payload.get("feature_names") or []),
            alpha=float(payload.get("alpha", 0.5)),
            discount=float(payload.get("discount", 0.99)),
            ridge=float(payload.get("ridge", 1.0)),
        )
        states = payload.get("states") or {}
        for action, state in states.items():
            if action not in policy._A:
                continue
            A = np.asarray(state.get("A"), dtype=np.float64)
            b = np.asarray(state.get("b"), dtype=np.float64)
            if A.shape == (policy.dim, policy.dim):
                policy._A[action] = A
            if b.shape == (policy.dim,):
                policy._b[action] = b
            policy._counts[action] = float(state.get("count", 0.0))
        return policy
