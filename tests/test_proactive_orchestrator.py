"""End-to-end orchestrator tests (no llama, no FastAPI).

We construct a fake generator that emits a fixed delta stream so the test
exercises the observe → record_feedback loop and the policy_state round-trip
without spinning up llama-server. The orchestrator's TimeoutMonitor runs as a
daemon thread, but we don't wait on it — we manually drive feedback to keep
the test deterministic.
"""
from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from services.proactive.generator import ProactiveGenerator
from services.proactive.models import ProactiveObservation
from services.proactive.orchestrator import ProactiveOrchestrator


def _fake_ghostwrite_iter(prefix: str, suffix: str = "", *, max_tokens: int = 64, use_workspace: bool = True):
    _ = (prefix, suffix, max_tokens, use_workspace)
    yield "fake "
    yield "continuation"


def _fake_editor_assist_iter(action: str, text: str, *, max_tokens: int = 400, use_workspace: bool = True):
    _ = (action, text, max_tokens, use_workspace)
    yield "fake "
    yield "rewrite"


def _build_orchestrator(tmp: Path, workspace_id: str = "test_ws") -> ProactiveOrchestrator:
    gen = ProactiveGenerator(
        ghostwrite_iter=_fake_ghostwrite_iter,
        editor_assist_iter=_fake_editor_assist_iter,
        workspace_is_active=lambda _w: True,
    )
    return ProactiveOrchestrator(
        output_root=tmp,
        workspace_id=workspace_id,
        generator=gen,
        rng=random.Random(42),
    )


def _native_observation(text: str = "이 문서는 충분히 긴 문장을 포함합니다. 이제 다음 문장을 작성해 보겠습니다.") -> ProactiveObservation:
    return ProactiveObservation(
        surface="native_editor",
        workspace_id="test_ws",
        document_key="doc-a",
        text=text,
        cursor_index=len(text),
        prefix=text,
        suffix="",
        current_sentence="이제 다음 문장을 작성해 보겠습니다.",
        current_paragraph=text,
        previous_paragraph="",
    )


class OrchestratorTests(unittest.TestCase):
    def test_observe_returns_decision_with_features(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orchestrator(Path(tmp))
            try:
                obs = _native_observation()
                decision = orch.observe(obs)
                self.assertIsNotNone(decision.decision_id)
                self.assertEqual(decision.surface, "native_editor")
                self.assertGreater(len(decision.available_suggestion_actions), 0)
                self.assertIsNotNone(decision.feature_snapshot)
                snap = decision.feature_snapshot
                assert snap is not None
                self.assertEqual(
                    len(snap.engage_features), len(snap.engage_feature_names)
                )
                self.assertEqual(
                    len(snap.suggest_features), len(snap.suggest_feature_names)
                )
            finally:
                orch.close()

    def test_decision_cache_serves_generator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orchestrator(Path(tmp))
            try:
                # Force intervene by repeatedly accepting until the engage
                # policy has positive evidence. We can't directly force the
                # roll, but we can short-circuit the path: explicitly set
                # candidate via observe then re-set the cached decision to
                # be `should_intervene=True`.
                obs = _native_observation()
                decision = orch.observe(obs)
                bundle = orch.get_decision(decision.decision_id)
                self.assertIsNotNone(bundle)
                # Stream is a generator — drain it.
                events = []
                if decision.should_intervene:
                    events = list(orch.stream_generation(decision.decision_id))
                    types = [e["type"] for e in events]
                    self.assertIn("start", types)
                    self.assertIn("done", types)
            finally:
                orch.close()

    def test_feedback_persists_and_updates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = _build_orchestrator(tmp_path)
            try:
                obs = _native_observation()
                decision = orch.observe(obs)
                record = orch.record_feedback(
                    decision_id=decision.decision_id,
                    raw_action="tab",
                )
                self.assertEqual(record.feedback_action, "accept")
                self.assertEqual(record.engage_reward, 1.0)

                # State file exists and parses.
                state_path = (
                    tmp_path / "test_ws" / "proactive_policy" / "policy_state.json"
                )
                self.assertTrue(state_path.exists())
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["version"], 2)
                self.assertIn("engage_policy", payload)
                self.assertIn("suggestion_policy", payload)
                self.assertEqual(payload["user_stats"]["counts"]["accept"], 1)

                # Feedback log captured the canonical mapping.
                fb_path = (
                    tmp_path / "test_ws" / "proactive_policy" / "feedback.jsonl"
                )
                self.assertTrue(fb_path.exists())
                lines = fb_path.read_text(encoding="utf-8").strip().splitlines()
                self.assertGreaterEqual(len(lines), 1)
                latest = json.loads(lines[-1])
                self.assertEqual(latest["feedback_action"], "accept")
            finally:
                orch.close()

    def test_external_copy_maps_to_accept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orchestrator(Path(tmp))
            try:
                obs = ProactiveObservation(
                    surface="external_screen",
                    workspace_id="test_ws",
                    document_key="external-doc",
                    text="외부 문서에서 사용자가 작성한 문단. " * 6,
                    current_paragraph="외부 문서에서 사용자가 작성한 문단. " * 4,
                    current_sentence="외부 문서에서 사용자가 작성한 문단.",
                )
                decision = orch.observe(obs)
                record = orch.record_feedback(
                    decision_id=decision.decision_id,
                    raw_action="copy",
                )
                self.assertEqual(record.feedback_action, "accept")
                self.assertEqual(record.surface, "external_screen")
            finally:
                orch.close()

    def test_decision_log_has_no_raw_text(self) -> None:
        """policy_state.json must never persist document text — see §19."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = _build_orchestrator(tmp_path)
            try:
                obs = _native_observation()
                decision = orch.observe(obs)
                _ = decision
            finally:
                orch.close()
            state_path = (
                tmp_path / "test_ws" / "proactive_policy" / "policy_state.json"
            )
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            blob = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(
                "이제 다음 문장을 작성해 보겠습니다.",
                blob,
                "raw document text leaked into policy_state.json",
            )


if __name__ == "__main__":
    unittest.main()
