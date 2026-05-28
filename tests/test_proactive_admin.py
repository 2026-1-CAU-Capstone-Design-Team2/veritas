"""Tests for the proactive admin levers added to un-stick a learned policy.

Covers:

1. ``VERITAS_PROACTIVE_PI_MIN`` / ``..._PI_MAX`` / ``..._DISCOUNT`` env vars
   are honored both on fresh init AND on load (the "I want to un-stick an
   already-trained policy" path).
2. ``orchestrator.reset()`` wipes learned θ_hat and resets pi_min/pi_max to
   whatever the env says.
3. ``orchestrator.snapshot()`` returns the operator-facing summary with
   theta_hat + counts.
"""
from __future__ import annotations

import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.proactive.generator import ProactiveGenerator
from services.proactive.orchestrator import ProactiveOrchestrator


def _gen():
    def ghost(p, s="", *, max_tokens=64, use_workspace=True):
        yield "x"

    def assist(a, t, *, max_tokens=400, use_workspace=True):
        yield "x"

    return ProactiveGenerator(
        ghostwrite_iter=ghost,
        editor_assist_iter=assist,
        workspace_is_active=lambda _w: True,
    )


def _build(tmp: Path) -> ProactiveOrchestrator:
    return ProactiveOrchestrator(
        output_root=tmp,
        workspace_id="admin_ws",
        generator=_gen(),
        rng=random.Random(3),
    )


class AdminLeversTests(unittest.TestCase):
    def test_env_overrides_apply_on_fresh_init(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"VERITAS_PROACTIVE_PI_MIN": "0.20", "VERITAS_PROACTIVE_DISCOUNT": "0.95"},
        ):
            with tempfile.TemporaryDirectory() as tmp:
                orch = _build(Path(tmp))
                try:
                    eng = orch.store.engage_policy
                    self.assertAlmostEqual(eng.pi_min, 0.20)
                    self.assertAlmostEqual(eng.discount, 0.95)
                finally:
                    orch.close()

    def test_env_overrides_apply_on_load(self) -> None:
        # First boot a workspace with default params and save.
        with tempfile.TemporaryDirectory() as tmp:
            orch1 = _build(Path(tmp))
            orch1.store.save()
            orch1.close()
            # Reopen with override env — the loaded state must take the env.
            with mock.patch.dict(os.environ, {"VERITAS_PROACTIVE_PI_MIN": "0.30"}):
                orch2 = _build(Path(tmp))
                try:
                    self.assertAlmostEqual(orch2.store.engage_policy.pi_min, 0.30)
                finally:
                    orch2.close()

    def test_reset_drops_learned_theta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build(Path(tmp))
            try:
                # Drive a few negative updates to nudge theta_hat away from 0.
                for _ in range(10):
                    orch.store.engage_policy.update(
                        x=[1.0] * 9,
                        engage_action="intervene",
                        pi_t=0.4,
                        reward=-1.0,
                    )
                theta_before = list(orch.store.engage_policy.theta_hat)
                self.assertFalse(all(abs(v) < 1e-9 for v in theta_before))
                result = orch.reset()
                theta_after = list(orch.store.engage_policy.theta_hat)
                # After reset, theta_hat is exactly the zero vector.
                self.assertTrue(all(abs(v) < 1e-9 for v in theta_after))
                self.assertIn("engage", result)
            finally:
                orch.close()

    def test_snapshot_returns_diagnostic_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build(Path(tmp))
            try:
                snap = orch.snapshot()
                self.assertIn("engage", snap)
                self.assertIn("theta_hat", snap["engage"])
                self.assertIn("pi_min", snap["engage"])
                self.assertIn("suggestion", snap)
                self.assertIn("user_stats", snap)
                self.assertIn("pending_timeouts", snap)
            finally:
                orch.close()


if __name__ == "__main__":
    unittest.main()
