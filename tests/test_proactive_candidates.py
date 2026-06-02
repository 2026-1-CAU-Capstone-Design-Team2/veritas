"""CandidateFactory tests.

Contracts we lock down:

1. Low-confidence anchor → no candidates (spec §3.2).
2. Every emitted candidate's ``target_anchor_id == anchor.anchor_id``.
3. Native surface only emits next_sentence when inline_diff/marker are off.
4. paragraph_rewrite requires both a long paragraph AND a churn/undo signal.
5. Long paragraph (≥500) emits long_paragraph_split.
"""
from __future__ import annotations

import unittest

from services.proactive.anchors import ActiveAnchor
from services.proactive.candidates import PrimitiveSignals, build_candidates
from services.proactive.proposal_models import SurfaceCapabilities


def _anchor(
    *,
    confidence: float = 0.9,
    paragraph: str = "이것은 약 80자 이상 되는 단락입니다. 다음 문장도 함께 길게 적어둡니다.",
    sentence: str = "다음 문장도 함께 길게 적어둡니다.",
    surface: str = "native_editor",
    prev_paragraph: str | None = None,
    next_paragraph: str | None = None,
) -> ActiveAnchor:
    return ActiveAnchor(
        document_id="doc-1",
        surface=surface,  # type: ignore[arg-type]
        cursor_index=len(paragraph),
        sentence_text=sentence,
        paragraph_text=paragraph,
        prev_paragraph=prev_paragraph,
        next_paragraph=next_paragraph,
        source="native_cursor" if surface == "native_editor" else "uia_caret",
        confidence=confidence,
    )


def _signals(**overrides) -> PrimitiveSignals:
    s = PrimitiveSignals(
        idle_sec=3.0,
        stable_capture_count=2,
        edit_volume_window=80.0,
        net_growth_window=60.0,
        churn_score=0.10,
        paragraph_len=80,
        document_len=400,
        cursor_pos=0.9,
        relevant_sources_available=False,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class CandidateFactoryTests(unittest.TestCase):
    def test_low_confidence_anchor_returns_no_candidates(self) -> None:
        out = build_candidates(
            anchor=_anchor(confidence=0.30),
            signals=_signals(),
            surface=SurfaceCapabilities.for_native(),
        )
        self.assertEqual(out, [])

    def test_native_default_is_next_sentence_only(self) -> None:
        out = build_candidates(
            anchor=_anchor(),
            signals=_signals(),
            surface=SurfaceCapabilities.for_native(),
        )
        # paragraph_rewrite needs churn; logic_flow needs neighbor; etc.
        self.assertGreater(len(out), 0)
        types = {c.task_type for c in out}
        # Only next_sentence should be possible with default native caps.
        self.assertEqual(types, {"next_sentence"})

    def test_every_candidate_targets_the_anchor(self) -> None:
        anchor = _anchor(
            paragraph="이 단락은 80자 이상이며 사용자가 작성한 본문입니다. " * 4,
            prev_paragraph="앞 단락",
            next_paragraph="뒤 단락",
            surface="external_app",
        )
        out = build_candidates(
            anchor=anchor,
            signals=_signals(churn_score=0.40, paragraph_len=300, document_len=1500),
            surface=SurfaceCapabilities.for_external(),
        )
        self.assertGreater(len(out), 0)
        for cand in out:
            self.assertEqual(cand.target_anchor_id, anchor.anchor_id)

    def test_paragraph_rewrite_needs_churn(self) -> None:
        out_low = build_candidates(
            anchor=_anchor(paragraph="긴 문단입니다. " * 20, surface="external_app"),
            signals=_signals(paragraph_len=300, churn_score=0.05),
            surface=SurfaceCapabilities.for_external(),
        )
        types_low = {c.task_type for c in out_low}
        self.assertNotIn("paragraph_rewrite", types_low)

        out_high = build_candidates(
            anchor=_anchor(paragraph="긴 문단입니다. " * 20, surface="external_app"),
            signals=_signals(paragraph_len=300, churn_score=0.50),
            surface=SurfaceCapabilities.for_external(),
        )
        types_high = {c.task_type for c in out_high}
        self.assertIn("paragraph_rewrite", types_high)

    def test_long_paragraph_split_when_paragraph_huge(self) -> None:
        big_para = "긴 단락 내용. " * 80
        out = build_candidates(
            anchor=_anchor(paragraph=big_para, surface="external_app"),
            signals=_signals(paragraph_len=len(big_para), document_len=len(big_para)),
            surface=SurfaceCapabilities.for_external(),
        )
        types = {c.task_type for c in out}
        self.assertIn("long_paragraph_split", types)

    def test_native_rewrite_omitted_without_inline_diff_renderer(self) -> None:
        out = build_candidates(
            anchor=_anchor(paragraph="긴 문단입니다. " * 20, surface="native_editor"),
            signals=_signals(paragraph_len=300, churn_score=0.50),
            surface=SurfaceCapabilities.for_native(inline_diff=False),
        )
        types = {c.task_type for c in out}
        self.assertNotIn("paragraph_rewrite", types)

    def test_max_three_candidates(self) -> None:
        big = "이 단락은 80자 이상이며 사용자가 작성한 본문입니다. " * 8
        out = build_candidates(
            anchor=_anchor(
                paragraph=big,
                prev_paragraph="앞 단락",
                next_paragraph="뒤 단락",
                surface="external_app",
            ),
            signals=_signals(
                churn_score=0.50,
                paragraph_len=len(big),
                document_len=len(big),
                relevant_sources_available=True,
            ),
            surface=SurfaceCapabilities.for_external(),
        )
        self.assertLessEqual(len(out), 3)


if __name__ == "__main__":
    unittest.main()
