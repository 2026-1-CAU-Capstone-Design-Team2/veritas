"""Surface-aware mask + format-strict prompts.

These three properties together address the user's "irrelevant suggestions
poison the reward signal" concern:

1. Native editor mask narrows to ``next_sentence`` so the bandit never
   explores actions that would dump commentary into the ghost overlay.
2. External lead-ins all carry the "[응답 형식 — 반드시 준수]" contract so
   the SuggestionCard's body/note split actually works.
3. The orchestrator's explain() returns a readable trace including the
   top-3 UCB scores.
"""
from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from services.proactive.action_space import build_suggestion_action_mask
from services.proactive.generator import (
    ProactiveGenerator,
    _LEAD_IN_EXTERNAL,
    _LEAD_IN_NATIVE,
    _resolve_lead_in,
)
from services.proactive.models import ProactiveObservation
from services.proactive.orchestrator import ProactiveOrchestrator


def _primitive(**overrides):
    p = {
        "idle_sec": 5.0,
        "stable_capture_count": 2,
        "edit_volume": 200.0,
        "net_growth": 300.0,
        "churn_score": 0.3,
        "paragraph_len": 400.0,
        "document_len": 2000.0,
        "cursor_pos": 0.5,
        "evidence_need_score": 0.4,
        "relevant_sources_available": True,
        "recent_negative_rate": 0.0,
        "time_since_last_intervention": 60.0,
        "surface_is_native": 1.0,
    }
    p.update(overrides)
    return p


class SurfaceMaskTests(unittest.TestCase):
    def test_native_mask_is_next_sentence_only(self) -> None:
        # Even with all the gates that would unlock paragraph_rewrite /
        # logic_flow_review on external, the native mask stays narrow.
        mask = build_suggestion_action_mask(
            _primitive(surface_is_native=1.0),
            surface_is_native=True,
        )
        self.assertEqual(mask, ["next_sentence"])

    def test_external_mask_is_full_menu(self) -> None:
        mask = build_suggestion_action_mask(
            _primitive(surface_is_native=0.0),
            surface_is_native=False,
        )
        # The same primitives unlock multiple types on external.
        self.assertIn("next_sentence", mask)
        self.assertIn("paragraph_rewrite", mask)
        self.assertIn("local_copyedit", mask)
        self.assertIn("logic_flow_review", mask)
        self.assertGreater(len(mask), 1)

    def test_surface_inferred_from_primitive_when_omitted(self) -> None:
        # Backward-compat: callers that pass only the primitive dict still
        # get the right narrowing because we read surface_is_native from it.
        mask = build_suggestion_action_mask(_primitive(surface_is_native=1.0))
        self.assertEqual(mask, ["next_sentence"])


class LeadInFormatTests(unittest.TestCase):
    def test_external_lead_ins_carry_format_contract(self) -> None:
        """Every external lead-in must include the 본문 + 설명: contract.
        Without it the SuggestionCard's body/note split degenerates and the
        copy button picks up commentary along with the paste-ready text."""
        for arm in (
            "next_sentence",
            "paragraph_rewrite",
            "local_copyedit",
            "logic_flow_review",
            "evidence_citation_prompt",
            "recovery_integration_note",
        ):
            with self.subTest(arm=arm):
                lead = _LEAD_IN_EXTERNAL[arm]
                self.assertIn("설명:", lead, arm)
                self.assertIn("[응답 형식", lead, arm)
                # Forbids "추천합니다" / meta-commentary in body.
                self.assertIn("추천합니다", lead, arm)

    def test_native_lead_ins_forbid_wrapping_prose(self) -> None:
        """Native lead-ins must NOT emit the 설명: contract — they're for
        the inline-diff renderer which replaces text wholesale, so any
        wrapping commentary would land in the document body."""
        for arm in (
            "paragraph_rewrite",
            "local_copyedit",
            "logic_flow_review",
            "evidence_citation_prompt",
            "recovery_integration_note",
        ):
            with self.subTest(arm=arm):
                lead = _LEAD_IN_NATIVE[arm]
                self.assertNotIn("설명:", lead, arm)
                self.assertIn("[과업]", lead, arm)

    def test_resolve_lead_in_picks_per_surface(self) -> None:
        ext = _resolve_lead_in("paragraph_rewrite", surface_is_native=False)
        nat = _resolve_lead_in("paragraph_rewrite", surface_is_native=True)
        self.assertNotEqual(ext, nat)
        self.assertIn("설명:", ext)
        self.assertNotIn("설명:", nat)


def _build_orch(tmp: Path) -> ProactiveOrchestrator:
    def g(p, s="", *, max_tokens=64, use_workspace=True):
        yield "x"

    def a(act, t, *, max_tokens=400, use_workspace=True):
        yield "x"

    gen = ProactiveGenerator(
        ghostwrite_iter=g,
        editor_assist_iter=a,
        workspace_is_active=lambda _w: True,
    )
    return ProactiveOrchestrator(
        output_root=tmp,
        workspace_id="explain_ws",
        generator=gen,
        rng=random.Random(7),
    )


class ExplainTests(unittest.TestCase):
    def test_explain_returns_readable_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orch(Path(tmp))
            try:
                obs = ProactiveObservation(
                    surface="native_editor",
                    workspace_id="explain_ws",
                    document_key="doc-1",
                    text="이 문서에는 일정 길이의 본문이 있다. 이어 쓸 문장을 작성한다.",
                    cursor_index=33,
                    prefix="이 문서에는 일정 길이의 본문이 있다. 이어 쓸 문장을 작성한다.",
                    current_paragraph="이 문서에는 일정 길이의 본문이 있다.",
                    current_sentence="이어 쓸 문장을 작성한다.",
                )
                decision = orch.observe(obs)
                trace = orch.explain(decision.decision_id)
                self.assertIsNotNone(trace)
                assert trace is not None
                self.assertEqual(trace["decisionId"], decision.decision_id)
                self.assertEqual(trace["surface"], "native_editor")
                self.assertIn("engage", trace)
                self.assertIn("suggestion", trace)
                self.assertIn("context", trace)
                self.assertIn("primitive", trace)
                self.assertIn("topScores", trace["suggestion"])
                # Native mask is narrowed → only one arm in the mask.
                self.assertEqual(trace["suggestion"]["mask"], ["next_sentence"])
            finally:
                orch.close()

    def test_explain_returns_none_for_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orch(Path(tmp))
            try:
                self.assertIsNone(orch.explain("pd_nonexistent"))
            finally:
                orch.close()


if __name__ == "__main__":
    unittest.main()
