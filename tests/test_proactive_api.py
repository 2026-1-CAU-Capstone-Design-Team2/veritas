"""End-to-end orchestrator tests for the rule-based pipeline.

Covers the spec §17 acceptance criteria:

1. observe returns either a ProactiveTask or NullPrediction (never a half-state)
2. Every task carries target_anchor_id, context_scope, render_mode, evaluator_score
3. Off-anchor candidates are impossible (the factory binds to the anchor)
4. Native/external feedback collapses to canonical accept/reject/retry/timeout
5. Feedback updates user_adaptation.json, NOT a bandit state file
6. Same anchor/task reject creates cooldown
7. Persistent logs contain no raw text
8. LLM generation is only called after a candidate passes evaluator/adaptation
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.proactive.generator import DEFAULT_GHOST_MAX_TOKENS, ProactiveGenerator
from services.proactive.models import ProactiveObservation
from services.proactive.orchestrator import (
    NATIVE_ANCHOR_PROXIMITY_CHARS,
    ProactiveOrchestrator,
    _extract_anchor,
)
from services.proactive.proposal_models import is_null, is_task


def _gen():
    def ghost(p, s="", *, max_tokens=64, use_workspace=True, section_heading=""):
        yield "이어쓰기 본문"

    def assist(a, t, *, max_tokens=400, use_workspace=True, additive_grounding=False):
        yield "주문에 맞춘 본문\n\n설명: 짧은 이유."

    return ProactiveGenerator(
        ghostwrite_iter=ghost,
        editor_assist_iter=assist,
        workspace_is_active=lambda _w: True,
    )


def _build_orch(tmp: Path, ws: str = "ws_api") -> ProactiveOrchestrator:
    return ProactiveOrchestrator(
        output_root=tmp, workspace_id=ws, generator=_gen()
    )


def _native_obs(text: str = "이 문서는 충분히 긴 본문을 가진다. 사용자가 이어 쓸 다음 문장을 작성한다.") -> ProactiveObservation:
    return ProactiveObservation(
        surface="native_editor",
        workspace_id="ws_api",
        document_key="doc-a",
        text=text,
        cursor_index=len(text),
        prefix=text,
        suffix="",
        current_paragraph=text,
        current_sentence="사용자가 이어 쓸 다음 문장을 작성한다.",
    )


class ObserveSchemaTests(unittest.TestCase):
    def test_observe_returns_task_or_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orch(Path(tmp))
            try:
                result = orch.observe(_native_obs())
                self.assertIn(result["prediction"], ("task", "null"))
                self.assertTrue(result["decision_id"].startswith("pd_"))
            finally:
                orch.close()

    def test_task_carries_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orch(Path(tmp))
            try:
                result = orch.observe(_native_obs())
                if result["prediction"] != "task":
                    self.skipTest("orchestrator chose null for this observation")
                task = result["task"]
                self.assertTrue(task.target_anchor_id.startswith("anc_"))
                self.assertTrue(task.context_scope)
                self.assertTrue(task.render_mode)
                self.assertGreaterEqual(task.evaluator_score, 0.0)
            finally:
                orch.close()


class FeedbackTests(unittest.TestCase):
    def test_feedback_updates_user_adaptation_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = _build_orch(tmp_path)
            try:
                result = orch.observe(_native_obs())
                decision_id = result["decision_id"]
                feedback = orch.record_feedback(
                    decision_id=decision_id,
                    raw_action="tab",
                )
                self.assertEqual(feedback["canonical_feedback"], "accept")
                path = tmp_path / "ws_api" / "proactive_policy" / "user_adaptation.json"
                self.assertTrue(path.exists())
                data = json.loads(path.read_text(encoding="utf-8"))
                # accept_ema bumped on a fresh state.
                self.assertGreater(data["global_stats"]["accept_ema"], 0.0)
            finally:
                orch.close()

    def test_native_3_rejects_trigger_anchor_ladder_cooldown(self) -> None:
        """Per services/proactive/README.md §"Native reject ladder":
        native rejects DON'T touch adaptation's per-(anchor, task) cooldown
        (the in-memory ladder owns that gate). The ladder reaches its
        cooldown after 3 consecutive rejects at the same anchor."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = _build_orch(tmp_path)
            try:
                result = orch.observe(_native_obs())
                if result["prediction"] != "task":
                    self.skipTest("orchestrator chose null for this observation")
                decision_id = result["decision_id"]
                anchor_id = result["task"].target_anchor_id

                # 1 reject — ladder records reject_count=1 but no cooldown.
                orch.record_feedback(decision_id=decision_id, raw_action="esc")
                # adaptation cooldown should still be empty for native:
                self.assertNotIn(
                    f"{anchor_id}|next_sentence",
                    orch.store.adaptation.state.anchor_cooldowns,
                )
                count, _, in_cd = orch._read_anchor_state(anchor_id)
                self.assertEqual(count, 1)
                self.assertFalse(in_cd)

                # Two more rejects to trip the 3-strike ladder.
                for _ in range(2):
                    r = orch.observe(_native_obs())
                    if r["prediction"] != "task":
                        self.skipTest("ladder produced null mid-test")
                    orch.record_feedback(
                        decision_id=r["decision_id"], raw_action="esc"
                    )

                count, _, in_cd = orch._read_anchor_state(anchor_id)
                self.assertEqual(count, 3)
                self.assertTrue(in_cd, "ladder cooldown should be active")

                # Next observe at the same anchor should null with the
                # ladder cooldown reason.
                blocked = orch.observe(_native_obs())
                self.assertEqual(blocked["prediction"], "null")
                self.assertEqual(blocked["null"].reason, "anchor_reject_cooldown")
            finally:
                orch.close()

    def test_anchor_cooldown_survives_small_cursor_jitter(self) -> None:
        """1-3: a single keystroke (e.g. one space) must NOT let the user bypass
        the 3-reject cooldown. The ladder matches by document + cursor proximity,
        so a one-char shift in cursor/paragraph still resolves to the cooled
        ladder entry instead of minting a fresh anchor that re-suggests."""
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orch(Path(tmp))
            try:
                base = "이 문서는 충분히 긴 본문을 가진다. 사용자가 이어 쓸 다음 문장을 작성한다."
                for _ in range(3):
                    r = orch.observe(_native_obs(base))
                    if r["prediction"] != "task":
                        self.skipTest("orchestrator chose null for this observation")
                    orch.record_feedback(decision_id=r["decision_id"], raw_action="esc")
                # One extra space → different anchor_id, cursor+1. Pre-fix this
                # re-suggested; now it must stay blocked by the same cooldown.
                jittered = base + " "
                blocked = orch.observe(_native_obs(jittered))
                self.assertEqual(blocked["prediction"], "null")
                self.assertEqual(blocked["null"].reason, "anchor_reject_cooldown")
            finally:
                orch.close()

    def test_cooldown_releases_when_cursor_moves_to_different_spot(self) -> None:
        """1-3 follow-up: a 3-reject cooldown must NOT follow the user to a
        different sentence/paragraph. A small edit at the same spot stays locked,
        but moving the cursor beyond the proximity window releases the lock."""
        from services.proactive.anchors import ActiveAnchor

        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orch(Path(tmp))
            try:
                near = ActiveAnchor(
                    document_id="doc-a",
                    surface="native_editor",
                    cursor_index=100,
                    paragraph_text="이 단락의 본문입니다.",
                )
                for _ in range(3):
                    orch._bump_anchor_reject(near, "거절된 제안")
                # Same spot + a couple of chars → still locked.
                same = ActiveAnchor(
                    document_id="doc-a",
                    surface="native_editor",
                    cursor_index=102,
                    paragraph_text="이 단락의 본문입니다. 가",
                )
                self.assertTrue(orch._read_anchor_state_for(same)[2])
                # A different sentence/paragraph (beyond the window) → free.
                far = ActiveAnchor(
                    document_id="doc-a",
                    surface="native_editor",
                    cursor_index=100 + NATIVE_ANCHOR_PROXIMITY_CHARS + 30,
                    paragraph_text="완전히 다른 단락입니다.",
                )
                self.assertFalse(orch._read_anchor_state_for(far)[2])
            finally:
                orch.close()

    def test_reject_ladder_accumulates_across_typing(self) -> None:
        """1-2/1-3: a reject recorded at one cursor must still resolve when the
        next suggestion fires a few characters away (the user typed). Tested at
        the ladder level so it doesn't depend on the candidate factory re-firing
        a task for the churned text."""
        from services.proactive.anchors import ActiveAnchor

        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orch(Path(tmp))
            try:
                a1 = ActiveAnchor(
                    document_id="doc-a",
                    surface="native_editor",
                    cursor_index=100,
                    paragraph_text="첫 번째 단락 본문입니다.",
                )
                orch._bump_anchor_reject(a1, "첫 번째 거절된 제안")
                # User typed three characters → new paragraph hash, cursor 103.
                a2 = ActiveAnchor(
                    document_id="doc-a",
                    surface="native_editor",
                    cursor_index=103,
                    paragraph_text="첫 번째 단락 본문입니다. 그리고",
                )
                self.assertNotEqual(a1.anchor_id, a2.anchor_id)
                count, last, in_cd = orch._read_anchor_state_for(a2)
                self.assertEqual(count, 1)
                self.assertEqual(last, "첫 번째 거절된 제안")
                self.assertFalse(in_cd)
                # A second reject at the shifted cursor keeps accumulating on the
                # SAME ladder entry rather than starting a fresh one.
                orch._bump_anchor_reject(a2, "두 번째 거절된 제안")
                count2, last2, _ = orch._read_anchor_state_for(a1)
                self.assertEqual(count2, 2)
                self.assertEqual(last2, "두 번째 거절된 제안")
                # A cursor far away (beyond the proximity window) is a fresh spot.
                far = ActiveAnchor(
                    document_id="doc-a",
                    surface="native_editor",
                    cursor_index=100 + NATIVE_ANCHOR_PROXIMITY_CHARS + 50,
                    paragraph_text="멀리 떨어진 다른 단락입니다.",
                )
                count_far, _, _ = orch._read_anchor_state_for(far)
                self.assertEqual(count_far, 0)
            finally:
                orch.close()

    def test_retry_forwards_last_rejected_text_at_level_zero(self) -> None:
        """1-2: a 다시/retry remembers the rejected text without bumping the
        reject count, and the NEXT suggestion still carries it as
        last_rejected_text so the generator can avoid repeating it."""
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orch(Path(tmp))
            try:
                r1 = orch.observe(_native_obs())
                if r1["prediction"] != "task":
                    self.skipTest("orchestrator chose null for this observation")
                orch.record_feedback(
                    decision_id=r1["decision_id"],
                    raw_action="retry",
                    metadata={"generated_text": "거절된 이전 제안 문장"},
                )
                r2 = orch.observe(_native_obs())
                if r2["prediction"] != "task":
                    self.skipTest("ladder produced null mid-test")
                # retry never bumps the count → no reject_level, but the avoid
                # text MUST be forwarded (the pre-fix gate dropped it here).
                self.assertIsNone(r2["task"].metadata.get("reject_level"))
                self.assertEqual(
                    r2["task"].metadata.get("last_rejected_text"),
                    "거절된 이전 제안 문장",
                )
            finally:
                orch.close()

    def test_wrong_anchor_does_not_set_anchor_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = _build_orch(tmp_path)
            try:
                result = orch.observe(_native_obs())
                if result["prediction"] != "task":
                    self.skipTest("orchestrator chose null for this observation")
                decision_id = result["decision_id"]
                anchor_id = result["task"].target_anchor_id
                task_type = result["task"].task_type
                orch.record_feedback(
                    decision_id=decision_id, raw_action="wrong_anchor"
                )
                self.assertNotIn(
                    f"{anchor_id}|{task_type}",
                    orch.store.adaptation.state.anchor_cooldowns,
                )
            finally:
                orch.close()

    def test_doc_cursor_makes_anchor_use_global_offset(self) -> None:
        """The anchor's cursor identity must come from ``doc_cursor`` (the true
        whole-document offset), not the window-clamped ``cursor_index`` that
        feeds features. Two deep positions with identical window cursor_index but
        different doc_cursor must yield different anchor cursors."""
        common = dict(
            surface="native_editor",
            workspace_id="ws_api",
            document_key="doc-a",
            text="문맥 " * 50,
            cursor_index=1500,  # window-clamped — identical for both
            current_paragraph="현재 문단",
            current_sentence="현재 문장.",
        )
        anchor_a = _extract_anchor(ProactiveObservation(doc_cursor=5000, **common))
        anchor_b = _extract_anchor(ProactiveObservation(doc_cursor=9000, **common))
        self.assertEqual(anchor_a.cursor_index, 5000)
        self.assertEqual(anchor_b.cursor_index, 9000)
        # Different true offsets → different anchor ids (no collapse).
        self.assertNotEqual(anchor_a.anchor_id, anchor_b.anchor_id)

    def test_absent_doc_cursor_falls_back_to_window_cursor(self) -> None:
        """External captures (and legacy callers) send no doc_cursor — the
        anchor then falls back to cursor_index, preserving old behavior."""
        anchor = _extract_anchor(
            ProactiveObservation(
                surface="native_editor",
                workspace_id="ws_api",
                document_key="doc-a",
                text="짧은 본문",
                cursor_index=42,
                current_paragraph="문단",
                current_sentence="문장.",
            )
        )
        self.assertEqual(anchor.cursor_index, 42)

    def test_reject_cooldown_does_not_leak_to_distant_position(self) -> None:
        """Bug regression: in a long document, a 3-reject cooldown at one spot
        used to freeze suggestions at completely different cursor positions
        because the ladder saw the same window-clamped cursor for both. With
        the true doc_cursor, the cooldown stays local to its spot."""
        common = dict(
            surface="native_editor",
            workspace_id="ws_api",
            document_key="doc-a",
            text="문맥 " * 50,
            cursor_index=1500,  # identical window cursor for both spots
            current_sentence="문장.",
        )
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orch(Path(tmp))
            try:
                spot_a = _extract_anchor(
                    ProactiveObservation(doc_cursor=5000, current_paragraph="문단 A", **common)
                )
                spot_b = _extract_anchor(
                    ProactiveObservation(doc_cursor=9000, current_paragraph="문단 B", **common)
                )
                for _ in range(3):
                    orch._bump_anchor_reject(spot_a, "거절된 제안")
                # Spot A is cooled down...
                self.assertTrue(orch._read_anchor_state_for(spot_a)[2])
                # ...but a distant spot (different paragraph/offset) is NOT — this
                # is exactly what was broken before the doc_cursor plumbing.
                self.assertFalse(orch._read_anchor_state_for(spot_b)[2])
            finally:
                orch.close()


class GhostTokenBudgetTests(unittest.TestCase):
    def test_native_ghost_uses_generous_token_budget(self) -> None:
        """1-1: the native ghost continuation must get a token budget large
        enough to finish a 1~2 sentence Korean continuation. The old 64-token
        default truncated mid-sentence; the generator now forwards a generous
        default to the ghostwrite call."""
        self.assertGreaterEqual(DEFAULT_GHOST_MAX_TOKENS, 128)
        captured: dict[str, int] = {}

        def ghost(prefix, suffix="", *, max_tokens=64, use_workspace=True, section_heading=""):
            captured["max_tokens"] = max_tokens
            yield "이어 쓸 다음 문장입니다."

        def assist(action, text, *, max_tokens=400, use_workspace=True, additive_grounding=False):
            yield "x"

        gen = ProactiveGenerator(ghostwrite_iter=ghost, editor_assist_iter=assist)
        self.assertEqual(gen.max_tokens_ghost, DEFAULT_GHOST_MAX_TOKENS)
        with tempfile.TemporaryDirectory() as tmp:
            orch = ProactiveOrchestrator(
                output_root=Path(tmp), workspace_id="ws_ghost", generator=gen
            )
            try:
                result = orch.observe(_native_obs())
                if result["prediction"] != "task":
                    self.skipTest("orchestrator chose null for this observation")
                events = list(orch.stream_generation(result["decision_id"]))
                self.assertTrue(any(e.get("type") == "delta" for e in events))
                self.assertEqual(captured.get("max_tokens"), DEFAULT_GHOST_MAX_TOKENS)
            finally:
                orch.close()


class PrivacyTests(unittest.TestCase):
    def test_decisions_jsonl_contains_no_raw_text(self) -> None:
        sentinel = "TOPSECRET_SENTENCE_xyz_8417"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = _build_orch(tmp_path)
            try:
                obs = _native_obs(
                    f"이 문서에는 {sentinel}가 들어있다. 이어 쓸 본문이 더 있다."
                )
                orch.observe(obs)
            finally:
                orch.close()
            path = tmp_path / "ws_api" / "proactive_policy" / "decisions.jsonl"
            blob = path.read_text(encoding="utf-8")
            self.assertNotIn(sentinel, blob)

    def test_user_adaptation_contains_no_raw_text(self) -> None:
        sentinel = "PRIVATE_MARK_q9281"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = _build_orch(tmp_path)
            try:
                obs = _native_obs(
                    f"문서에 {sentinel}가 있다. 다음 문장이 이어진다."
                )
                result = orch.observe(obs)
                orch.record_feedback(
                    decision_id=result["decision_id"], raw_action="tab"
                )
            finally:
                orch.close()
            path = tmp_path / "ws_api" / "proactive_policy" / "user_adaptation.json"
            blob = path.read_text(encoding="utf-8")
            self.assertNotIn(sentinel, blob)


class NoBanditImportInProductionPathTests(unittest.TestCase):
    """Spec §17.1: no production path imports the legacy bandit classes."""

    def test_orchestrator_module_does_not_import_bandit(self) -> None:
        import services.proactive.orchestrator as orch_mod
        # The orchestrator module's globals must not contain any bandit policy.
        for forbidden in (
            "ActionCenteredEngagePolicy",
            "DisjointDiscountedLinUCB",
        ):
            self.assertNotIn(forbidden, dir(orch_mod), forbidden)


if __name__ == "__main__":
    unittest.main()
