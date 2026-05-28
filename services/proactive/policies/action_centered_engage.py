"""Action-Centered Contextual Bandit for the engage / no-op decision.

The classical contextual bandit "should I intervene?" learns ``E[r | x,
intervene]`` and compares against a baseline. The problem: when the
*no-op* outcome reward is heuristic (and ours is — see ``timeout_monitor``),
the baseline carries enough noise to swamp the intervene-vs-no-op difference.

Greenewald, Tewari et al.'s action-centering trick fixes this by learning the
*difference* directly. For each decision we randomize intervene with
probability ``π_t``; the estimator uses the residual ``(I_t − π_t)`` so the
expected gradient is zero under the null hypothesis "intervene == no-op".
That's the only reason this is a separate class from the LinUCB above.

The update from the spec (§10.4):
    B     ← γ·B    + π_t·(1 − π_t)·xxᵀ
    b̂     ← γ·b̂    + (I_t − π_t)·r_t·x
    θ̂     = B⁻¹ b̂

Selection (§10.3): the Thompson-style probability that the intervention
effect is positive, clipped to ``[π_min, π_max]`` and then gated by hard
safety rules (recent-negative-rate, cooldown).
"""
from __future__ import annotations

import math
import random as _random
from typing import Any

import numpy as np


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via ``erf`` — cheaper than scipy.stats.norm.cdf
    and avoids the dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class ActionCenteredEngagePolicy:
    """Action-centered bandit for the no_op ↔ intervene decision.

    Stored fields:
        ``B``         : d×d precision matrix
        ``b_hat``     : d residual-weighted feature sum
        ``theta_hat`` : d current effect estimate (cached, recomputed lazily)
    """

    def __init__(
        self,
        feature_names: list[str],
        *,
        pi_min: float = 0.05,
        pi_max: float = 0.60,
        v: float = 0.5,
        ridge: float = 1.0,
        discount: float = 0.995,
        warmup_decisions: int = 20,
        warmup_pi_floor: float = 0.30,
    ) -> None:
        if not feature_names:
            raise ValueError("feature_names must be non-empty")
        self.feature_names: list[str] = list(feature_names)
        self.dim: int = len(feature_names)
        self.pi_min: float = float(pi_min)
        self.pi_max: float = float(pi_max)
        self.v: float = float(v)
        self.ridge: float = float(ridge)
        self.discount: float = float(discount)
        # Cold-start exploration. With ridge=1 and v=0.5, just 3 strong
        # negative rewards collapse p_positive from 0.50 to under pi_min=0.05.
        # That's correct asymptotically but disastrous early on — the user
        # never gets enough exposure for the bandit to *learn* what works.
        # During warmup we keep pi_t at ``warmup_pi_floor`` regardless of the
        # learned mean, then the natural Thompson dynamics take over.
        self.warmup_decisions: int = int(max(0, warmup_decisions))
        self.warmup_pi_floor: float = float(max(0.0, min(1.0, warmup_pi_floor)))
        self._B: np.ndarray = np.eye(self.dim, dtype=np.float64) * self.ridge
        self._b_hat: np.ndarray = np.zeros(self.dim, dtype=np.float64)
        # Diagnostic counters, recorded with each save so the operator can
        # see how exploratory the policy is being. ``_counts`` is decayed
        # by discount; ``_total_decisions`` is a hard count used for the
        # warmup gate and never decays.
        self._counts: dict[str, float] = {
            "intervene": 0.0,
            "no_op": 0.0,
        }
        self._total_decisions: int = 0

    # ------------------------------------------------------------- select

    def select(
        self,
        x: list[float],
        *,
        candidate_suggestion_type: str | None,
        safety_allowed: bool = True,
        time_since_last_intervention: float = 9999.0,
        recent_negative_rate: float = 0.0,
        idle_sec: float = 0.0,
        rng: _random.Random | None = None,
    ) -> dict[str, Any]:
        """Decide ``no_op`` vs ``intervene`` for the candidate suggestion type.

        ``candidate_suggestion_type`` is passed through unmodified so the
        caller can record it on the decision; the engage policy itself does
        not condition on the candidate identity (it sees only ``x``). When
        ``candidate_suggestion_type is None`` (suggestion policy returned
        nothing), the engage policy still computes π_t for telemetry but the
        actual sampled action is forced to ``no_op``.
        """
        if len(x) != self.dim:
            raise ValueError(
                f"feature dim mismatch: expected {self.dim}, got {len(x)}"
            )
        x_arr = np.asarray(x, dtype=np.float64)

        try:
            theta_hat = np.linalg.solve(self._B, self._b_hat)
            b_inv_x = np.linalg.solve(self._B, x_arr)
        except np.linalg.LinAlgError:
            b_inv = np.linalg.pinv(self._B)
            theta_hat = b_inv @ self._b_hat
            b_inv_x = b_inv @ x_arr

        mean = float(np.dot(theta_hat, x_arr))
        variance = max(1e-8, float(self.v * self.v) * float(np.dot(x_arr, b_inv_x)))
        std = math.sqrt(variance)
        p_positive = _normal_cdf(mean / std)

        pi_t = max(self.pi_min, min(self.pi_max, p_positive))

        # Cold-start warmup: while we have fewer than ``warmup_decisions``
        # total observed selections, hold pi_t at ``warmup_pi_floor``. The
        # action-centering update with only 1-3 reward samples is so noisy
        # that the natural pi_t can crash from 0.5 to pi_min after a single
        # bad rollout. Warmup gives the policy a forced-exploration window.
        warmup_active = self._total_decisions < self.warmup_decisions
        warmup_floor_used = False
        if warmup_active and pi_t < self.warmup_pi_floor:
            pi_t = min(self.pi_max, self.warmup_pi_floor)
            warmup_floor_used = True

        gate_reason = ""
        if candidate_suggestion_type is None:
            pi_t = 0.0
            gate_reason = "no_candidate"
        elif not safety_allowed:
            pi_t = 0.0
            gate_reason = "safety_disallowed"
        elif time_since_last_intervention < 5.0:
            pi_t = 0.0
            gate_reason = "cooldown"
        elif recent_negative_rate >= 0.9 and idle_sec < 15.0:
            pi_t = 0.0
            gate_reason = "negative_streak"

        rng = rng or _random
        roll = rng.random()
        if roll < pi_t:
            selected = "intervene"
        else:
            selected = "no_op"

        # Bump the warmup counter even on hard-gated decisions: the gates fire
        # the same regardless of policy state, and ticking through them
        # ensures the warmup window can actually elapse instead of stalling.
        self._total_decisions += 1

        return {
            "selected": selected,
            "pi_t": float(pi_t),
            "p_positive": float(p_positive),
            "mean": float(mean),
            "std": float(std),
            "theta_hat": theta_hat.tolist(),
            "gate_reason": gate_reason,
            "roll": float(roll),
            "warmup_active": warmup_active,
            "warmup_floor_used": warmup_floor_used,
            "total_decisions": self._total_decisions,
            "warmup_remaining": max(0, self.warmup_decisions - self._total_decisions),
        }

    # ------------------------------------------------------------- update

    def update(
        self,
        *,
        x: list[float],
        engage_action: str,
        pi_t: float,
        reward: float,
    ) -> dict[str, Any]:
        """Apply the action-centering update from one observed reward.

        ``engage_action`` is what was *actually* executed (intervene or no_op);
        ``pi_t`` is the probability the policy used at decision time —
        crucially, NOT recomputed from the current B (which has moved on).
        """
        if len(x) != self.dim:
            raise ValueError(
                f"feature dim mismatch: expected {self.dim}, got {len(x)}"
            )
        if reward is None:
            return {"updated": False, "reason": "no_reward"}

        x_arr = np.asarray(x, dtype=np.float64)
        I_t = 1.0 if engage_action == "intervene" else 0.0
        pi_clamped = max(0.0, min(1.0, float(pi_t)))
        variance_factor = pi_clamped * (1.0 - pi_clamped)
        residual = (I_t - pi_clamped) * float(reward)

        # Action-centering update — note variance_factor can be 0 when the
        # decision was hard-gated (pi_t = 0 or 1). In that case B doesn't
        # absorb new mass for this sample (which is correct — the decision
        # was deterministic so it tells us nothing about the effect's
        # variance) but b_hat still moves on the residual (also correct —
        # gated decisions still carry an *observed* outcome).
        self._B = self.discount * self._B + variance_factor * np.outer(x_arr, x_arr)
        self._b_hat = self.discount * self._b_hat + residual * x_arr
        self._counts[engage_action] = self.discount * self._counts.get(engage_action, 0.0) + 1.0
        return {
            "updated": True,
            "engage_action": engage_action,
            "pi_t": pi_clamped,
            "reward": float(reward),
            "residual": float(residual),
            "variance_factor": float(variance_factor),
        }

    # --------------------------------------------------------- (de)serialize

    def to_payload(self) -> dict[str, Any]:
        return {
            "algorithm": "action_centered_contextual_bandit",
            "feature_names": list(self.feature_names),
            "pi_min": self.pi_min,
            "pi_max": self.pi_max,
            "v": self.v,
            "ridge": self.ridge,
            "discount": self.discount,
            "warmup_decisions": self.warmup_decisions,
            "warmup_pi_floor": self.warmup_pi_floor,
            "B": self._B.tolist(),
            "b_hat": self._b_hat.tolist(),
            "counts": dict(self._counts),
            "total_decisions": int(self._total_decisions),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ActionCenteredEngagePolicy":
        policy = cls(
            feature_names=list(payload.get("feature_names") or []),
            pi_min=float(payload.get("pi_min", 0.05)),
            pi_max=float(payload.get("pi_max", 0.60)),
            v=float(payload.get("v", 0.5)),
            ridge=float(payload.get("ridge", 1.0)),
            discount=float(payload.get("discount", 0.995)),
            warmup_decisions=int(payload.get("warmup_decisions", 20)),
            warmup_pi_floor=float(payload.get("warmup_pi_floor", 0.30)),
        )
        B = np.asarray(payload.get("B"), dtype=np.float64)
        b_hat = np.asarray(payload.get("b_hat"), dtype=np.float64)
        if B.shape == (policy.dim, policy.dim):
            policy._B = B
        if b_hat.shape == (policy.dim,):
            policy._b_hat = b_hat
        counts = payload.get("counts") or {}
        for k in ("intervene", "no_op"):
            policy._counts[k] = float(counts.get(k, 0.0))
        policy._total_decisions = int(payload.get("total_decisions", 0))
        return policy

    # --------------------------------------------------------- introspection

    @property
    def theta_hat(self) -> list[float]:
        try:
            return np.linalg.solve(self._B, self._b_hat).tolist()
        except np.linalg.LinAlgError:
            return (np.linalg.pinv(self._B) @ self._b_hat).tolist()
