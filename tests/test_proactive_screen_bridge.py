"""Tests for the screen-intervention → proactive observe bridge.

The bridge swaps a legacy screen ``event_id`` for a ``pd_*`` decisionId so the
existing frontend SuggestionCard renders the spec's 복사 / 거절 / 다시 row
and feedback is routed through ``services.proactive.reward``. We protect:

1. The bridge actually drives a proactive observe (decisionId returned).
2. The bridge respects the ``VERITAS_PROACTIVE_SCREEN=0`` opt-out.
3. Malformed interventions are silently ignored (the legacy screen surface
   must never break because of a bandit hiccup).
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
from services.proactive.screen_bridge import (
    observe_screen_intervention,
    proactive_screen_enabled,
)


def _fake_ghost(p, s="", *, max_tokens=64, use_workspace=True):
    yield "x"


def _fake_assist(a, t, *, max_tokens=400, use_workspace=True):
    yield "x"


def _build_orchestrator(tmp: Path) -> ProactiveOrchestrator:
    gen = ProactiveGenerator(
        ghostwrite_iter=_fake_ghost,
        editor_assist_iter=_fake_assist,
        workspace_is_active=lambda _w: True,
    )
    return ProactiveOrchestrator(
        output_root=tmp,
        workspace_id="test_ws",
        generator=gen,
        rng=random.Random(11),
    )


class ScreenBridgeTests(unittest.TestCase):
    def test_observe_returns_decision_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orchestrator(Path(tmp))
            try:
                intervention = {
                    "event_id": "ev_legacy_42",
                    "intervention_type": "paragraph_rewrite_needed",
                    "writing_context": {
                        "focused_sentence": "이 문장의 의미가 명확하지 않습니다.",
                        "recent_sentences": "이전에 작성한 단락 본문입니다. 이 문장의 의미가 명확하지 않습니다.",
                        "previous_paragraph": "이전 단락.",
                        "paragraph_source": "main",
                    },
                    "app_context": {
                        "process_name": "WINWORD.EXE",
                        "title": "report.docx - Word",
                    },
                }
                decision_id = observe_screen_intervention(
                    orchestrator=orch,
                    intervention=intervention,
                    workspace_id="test_ws",
                )
                self.assertIsNotNone(decision_id)
                assert decision_id is not None
                self.assertTrue(decision_id.startswith("pd_"))
            finally:
                orch.close()

    def test_env_opt_out(self) -> None:
        with mock.patch.dict(os.environ, {"VERITAS_PROACTIVE_SCREEN": "0"}):
            self.assertFalse(proactive_screen_enabled())
            # With opt-out the bridge must not even call observe.
            with tempfile.TemporaryDirectory() as tmp:
                orch = _build_orchestrator(Path(tmp))
                try:
                    decision_id = observe_screen_intervention(
                        orchestrator=orch,
                        intervention={
                            "event_id": "ev_x",
                            "writing_context": {"recent_sentences": "긴 단락"},
                        },
                        workspace_id="test_ws",
                    )
                    self.assertIsNone(decision_id)
                finally:
                    orch.close()

    def test_empty_intervention_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orchestrator(Path(tmp))
            try:
                decision_id = observe_screen_intervention(
                    orchestrator=orch,
                    intervention={"event_id": "ev_y"},
                    workspace_id="test_ws",
                )
                self.assertIsNone(decision_id)
            finally:
                orch.close()


if __name__ == "__main__":
    unittest.main()
