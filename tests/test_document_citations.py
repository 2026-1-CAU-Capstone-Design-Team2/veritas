from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from api.services import document_citation_service as svc
from frontend.citation_links import (
    CITATION_SCHEME,
    extract_claim_from_line,
    linkify_citations,
    parse_citation_url,
)


# Source paragraph used across the matching tests. The first sentence is the
# "exact quote" target, the PLSM sentence is the "paraphrase overlap" target;
# the rest are filler so the matcher has to discriminate.
_SOURCE = (
    "Atari 26 게임에서 평균 점수가 5.6 포인트 향상되었다. "
    "PLSM 모델은 Atari 환경에서 5.6 포인트 성능 향상을 달성했다. "
    "이 문서는 강화학습과 세계 모델의 통합 프레임워크를 다룬다.\n\n"
    "RLVR-World는 검증 가능한 보상을 활용하여 월드 모델을 최적화한다."
)


def _make_workspace(root: Path, doc_id: str = "000") -> None:
    ws = root / "WS"
    (ws / "clean_md").mkdir(parents=True, exist_ok=True)
    (ws / "summary").mkdir(parents=True, exist_ok=True)
    (ws / "clean_md" / f"{doc_id}.md").write_text(_SOURCE, encoding="utf-8")
    index = {
        "records": [
            {
                "doc_id": doc_id,
                "title": "RLVR-World 논문",
                "url": "https://example.com/raw",
                "final_url": "https://arxiv.org/abs/2505.13934",
                "domain": "arxiv.org",
            }
        ]
    }
    (ws / "summary" / "index.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8"
    )


class DocumentCitationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._prev_env = os.environ.get("VERITAS_OUTPUT_DIR")
        os.environ["VERITAS_OUTPUT_DIR"] = str(self._root)
        _make_workspace(self._root)

    def tearDown(self) -> None:
        if self._prev_env is None:
            os.environ.pop("VERITAS_OUTPUT_DIR", None)
        else:
            os.environ["VERITAS_OUTPUT_DIR"] = self._prev_env
        self._tmp.cleanup()

    def test_returns_metadata_and_best_match(self) -> None:
        result = svc.get_citation("WS", "doc_000", "Atari 26 게임 평균 점수 5.6")
        self.assertEqual(result["docId"], "doc_000")
        self.assertEqual(result["title"], "RLVR-World 논문")
        self.assertEqual(result["url"], "https://arxiv.org/abs/2505.13934")
        self.assertEqual(result["domain"], "arxiv.org")
        self.assertIsNotNone(result["match"])
        self.assertIn("Atari", result["match"]["paragraphText"])

    def test_accepts_doc_prefixed_and_bare_ids(self) -> None:
        claim = "RLVR-World 검증 가능한 보상"
        prefixed = svc.get_citation("WS", "doc_000", claim)
        bare = svc.get_citation("WS", "000", claim)
        self.assertEqual(prefixed["docId"], "doc_000")
        self.assertEqual(bare["docId"], "doc_000")
        self.assertIsNotNone(prefixed["match"])
        self.assertIsNotNone(bare["match"])
        self.assertEqual(
            prefixed["match"]["text"], bare["match"]["text"]
        )

    def test_short_id_resolves_to_zero_padded_file(self) -> None:
        # A short id (doc_7) must resolve to the 3-digit clean_md file (007.md)
        # and report the canonical docId doc_007.
        (self._root / "WS" / "clean_md" / "007.md").write_text(
            "RLVR-World 검증 가능한 보상 강화학습 프레임워크.", encoding="utf-8"
        )
        result = svc.get_citation("WS", "doc_7", "RLVR-World 검증 가능한 보상")
        self.assertEqual(result["docId"], "doc_007")
        self.assertIsNotNone(result["match"])

    def test_path_traversal_doc_id_rejected(self) -> None:
        result = svc.get_citation("WS", "../../secret", "anything")
        self.assertIsNone(result["match"])
        self.assertEqual(result.get("error"), "invalid_doc_id")

    def test_path_traversal_workspace_rejected(self) -> None:
        result = svc.get_citation("../WS", "doc_000", "anything")
        self.assertIsNone(result["match"])
        self.assertEqual(result.get("error"), "source_not_found")

    def test_exact_quote_match_is_high_confidence(self) -> None:
        # The trailing marker must be stripped before matching.
        claim = "Atari 26 게임에서 평균 점수가 5.6 포인트 향상되었다 [doc_000]"
        result = svc.get_citation("WS", "doc_000", claim)
        match = result["match"]
        self.assertIsNotNone(match)
        self.assertEqual(match["confidence"], "high")
        self.assertIn("Atari 26", match["text"])
        self.assertGreaterEqual(match["score"], 0.6)

    def test_paraphrased_overlap_match(self) -> None:
        claim = "PLSM 모델은 Atari 에서 5.6 포인트 향상"
        result = svc.get_citation("WS", "doc_000", claim)
        match = result["match"]
        self.assertIsNotNone(match)
        self.assertIn("PLSM", match["text"])
        self.assertNotEqual(match["confidence"], "low")

    def test_no_match_fallback_is_low_confidence(self) -> None:
        result = svc.get_citation("WS", "doc_000", "고양이가 정원에서 잠을 잤다 맑음")
        match = result["match"]
        # Still returns the closest candidate rather than crashing/None.
        self.assertIsNotNone(match)
        self.assertEqual(match["confidence"], "low")

    def test_missing_source_returns_metadata_without_match(self) -> None:
        result = svc.get_citation("WS", "doc_999", "anything")
        self.assertIsNone(result["match"])
        self.assertEqual(result.get("error"), "source_not_found")


class CitationLinkifyTests(unittest.TestCase):
    def _doc_links(self, rendered: str) -> list[str]:
        return re.findall(rf"{CITATION_SCHEME}:(doc_\d+)", rendered)

    def test_bracketed_marker_keeps_bracket_label(self) -> None:
        out = linkify_citations("핵심 결과는 중요하다 [doc_000].")
        self.assertIn(f"{CITATION_SCHEME}:doc_000?claim=", out)
        # Nested-bracket label so the rendered text stays [doc_000], not doc_000.
        self.assertIn("[[doc_000]](", out)
        # Must NOT use escaped brackets — \[..\] collides with LaTeX display math
        # in the document renderer (markdown_view._extract_math).
        self.assertNotIn("\\[", out)

    def test_renders_through_document_math_pipeline(self) -> None:
        # Regression guard for the real render path: linkify -> _extract_math ->
        # _normalize_for_qt -> markdown. The earlier escaped-bracket label broke
        # here (it was eaten as LaTeX \[..\]); nested brackets survive.
        try:
            import markdown

            from frontend.ui.markdown_view import _extract_math, _normalize_for_qt
        except Exception:  # pragma: no cover - renderer/Qt unavailable
            self.skipTest("document renderer unavailable")
        src = linkify_citations("DS 부문 실적 [doc_010] 그리고 bare doc_008 도 있다")
        protected, math_map = _extract_math(src)
        html = markdown.markdown(
            _normalize_for_qt(protected),
            extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
            output_format="html5",
        )
        for token, fragment in math_map.items():
            html = html.replace(token, fragment)
        self.assertEqual(html.count('<a href="veritas-citation:'), 2)
        self.assertIn(">[doc_010]<", html)
        self.assertIn(">[doc_008]<", html)
        # No orphaned, unparsed link markdown left in the output.
        self.assertNotIn("](veritas-citation", html)

    def test_bare_markers_linkified_and_normalized(self) -> None:
        out = linkify_citations("근거는 doc_000 이고 doc-001 과 doc002 도 있다")
        self.assertEqual(self._doc_links(out), ["doc_000", "doc_001", "doc_002"])
        # Every spelling renders with the same normalized bracket label.
        self.assertIn("[[doc_000]](", out)
        self.assertIn("[[doc_001]](", out)
        self.assertIn("[[doc_002]](", out)

    def test_short_form_ids_zero_padded_to_canonical(self) -> None:
        # doc7 / doc_7 / doc-7 / [doc_7] must all render as [doc_007] and point
        # at the doc_007 endpoint, matching FINAL_PROMPT's 3-digit [doc_NNN] rule.
        for raw in ("doc7", "doc_7", "doc-7", "[doc_7]"):
            out = linkify_citations(f"근거 {raw} 참고")
            self.assertEqual(self._doc_links(out), ["doc_007"], raw)
            self.assertIn("[[doc_007]](", out)

    def test_code_fence_markers_untouched(self) -> None:
        text = "```\n[doc_000] in code\n```\n바깥 문장 [doc_001]"
        out = linkify_citations(text)
        self.assertNotIn(f"{CITATION_SCHEME}:doc_000", out)
        self.assertIn(f"{CITATION_SCHEME}:doc_001", out)
        # The fenced marker stays as literal text.
        self.assertIn("[doc_000] in code", out)

    def test_inline_code_marker_untouched(self) -> None:
        out = linkify_citations("코드 `doc_000` 와 본문 doc_001")
        self.assertNotIn(f"{CITATION_SCHEME}:doc_000", out)
        self.assertIn(f"{CITATION_SCHEME}:doc_001", out)
        self.assertIn("`doc_000`", out)

    def test_existing_link_target_untouched(self) -> None:
        text = "[원문 보기](http://example.com/doc_000)"
        self.assertEqual(linkify_citations(text), text)

    def test_url_and_path_markers_untouched(self) -> None:
        self.assertNotIn(
            CITATION_SCHEME, linkify_citations("https://arxiv.org/doc_000 참고")
        )
        self.assertNotIn(
            CITATION_SCHEME, linkify_citations("파일 clean_md/doc_000.md 확인")
        )

    def test_already_linked_marker_not_double_wrapped(self) -> None:
        text = "[doc_000](http://x)"
        self.assertEqual(linkify_citations(text), text)

    def test_claim_round_trips_through_link_and_parser(self) -> None:
        line = "Atari 26 게임 점수가 5.6 향상 [doc_000]"
        out = linkify_citations(line)
        start = out.index(f"{CITATION_SCHEME}:")
        href = out[start : out.index(")", start)]
        parsed = parse_citation_url(href)
        self.assertIsNotNone(parsed)
        doc_id, claim = parsed
        self.assertEqual(doc_id, "doc_000")
        self.assertEqual(claim, extract_claim_from_line(line))

    def test_parse_rejects_non_citation_scheme(self) -> None:
        self.assertIsNone(parse_citation_url("https://arxiv.org/abs/1"))
        self.assertIsNone(parse_citation_url(""))

    def test_extract_claim_strips_bracketed_and_bare_markers(self) -> None:
        claim = extract_claim_from_line("| 핵심 | 결과 [doc_003] 와 doc_004 |")
        self.assertNotIn("doc_003", claim)
        self.assertNotIn("doc_004", claim)
        self.assertNotIn("[", claim)
        self.assertNotIn("|", claim)


if __name__ == "__main__":
    unittest.main()
