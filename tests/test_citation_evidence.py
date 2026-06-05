from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from api.services import document_citation_service as svc
from services.citation_evidence import (
    build_evidence_atoms,
    load_atoms_from_payload,
    match_claim_to_evidence,
)
from services.final_citations import build_final_citations


# An English source document. The localized (Korean) summary claims below must
# anchor back to these English sentences — the cross-language case raw lexical
# matching cannot bridge.
_EN_SOURCE = (
    "The PLSM model achieved a 5.6 point improvement on Atari benchmarks. "
    "It combines reinforcement learning with a learned world model.\n\n"
    "RLVR-World optimizes the world model using verifiable rewards. "
    "Training converged within 48 hours on a single GPU."
)


class BuildEvidenceAtomsTests(unittest.TestCase):
    def test_verbatim_quote_is_verified_and_anchored(self) -> None:
        payload = {
            "evidence": [
                {
                    "claim": "PLSM 모델이 Atari 에서 5.6 포인트 향상을 달성했다",
                    "quote": "The PLSM model achieved a 5.6 point improvement on Atari benchmarks.",
                }
            ]
        }
        atoms = build_evidence_atoms("000", payload, _EN_SOURCE)
        self.assertEqual(len(atoms), 1)
        atom = atoms[0]
        self.assertEqual(atom["evidenceId"], "doc_000-e0")
        self.assertEqual(atom["docId"], "doc_000")
        # Localized claim stays Korean; the anchored source sentence is English.
        self.assertIn("PLSM 모델", atom["localizedClaim"])
        self.assertIn("PLSM model", atom["text"])
        self.assertGreaterEqual(atom["score"], 0.5)

    def test_unverifiable_quote_is_dropped(self) -> None:
        payload = {
            "evidence": [
                {
                    "claim": "관련 없는 주장",
                    "quote": "A cat slept quietly in the sunny garden all afternoon.",
                }
            ]
        }
        self.assertEqual(build_evidence_atoms("000", payload, _EN_SOURCE), [])

    def test_item_missing_claim_or_quote_skipped(self) -> None:
        payload = {
            "evidence": [
                {"quote": "The PLSM model achieved a 5.6 point improvement on Atari benchmarks."},
                {"claim": "근거 없는 주장"},
                "not-a-dict",
            ]
        }
        self.assertEqual(build_evidence_atoms("000", payload, _EN_SOURCE), [])

    def test_missing_evidence_field_returns_empty(self) -> None:
        self.assertEqual(build_evidence_atoms("000", {"summary": "x"}, _EN_SOURCE), [])
        self.assertEqual(build_evidence_atoms("000", None, _EN_SOURCE), [])

    def test_no_raw_body_persisted_only_bounded_snippets(self) -> None:
        payload = {
            "evidence": [
                {
                    "claim": "RLVR-World 는 검증 가능한 보상으로 월드 모델을 최적화한다",
                    "quote": "RLVR-World optimizes the world model using verifiable rewards.",
                }
            ]
        }
        atom = build_evidence_atoms("003", payload, _EN_SOURCE)[0]
        self.assertLessEqual(len(atom["text"]), 500)
        self.assertLessEqual(len(atom["sourceQuote"]), 500)
        self.assertLessEqual(len(atom["paragraphText"]), 700)


class MatchClaimToEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        payload = {
            "evidence": [
                {
                    "claim": "PLSM 모델이 Atari 에서 5.6 포인트 향상을 달성했다",
                    "quote": "The PLSM model achieved a 5.6 point improvement on Atari benchmarks.",
                },
                {
                    "claim": "RLVR-World 는 검증 가능한 보상으로 월드 모델을 최적화한다",
                    "quote": "RLVR-World optimizes the world model using verifiable rewards.",
                },
            ]
        }
        self.atoms = build_evidence_atoms("000", payload, _EN_SOURCE)

    def test_korean_claim_resolves_to_english_source_sentence(self) -> None:
        # The clicked final claim is a Korean paraphrase; it matches the Korean
        # localized claim and returns the verified English source sentence.
        atom = match_claim_to_evidence("PLSM 모델은 Atari 에서 5.6 포인트 성능 향상", self.atoms)
        self.assertIsNotNone(atom)
        self.assertIn("PLSM model", atom["text"])
        self.assertIn("claimOverlap", atom)

    def test_picks_the_more_related_atom(self) -> None:
        atom = match_claim_to_evidence("RLVR-World 검증 가능한 보상 월드 모델 최적화", self.atoms)
        self.assertIsNotNone(atom)
        self.assertIn("RLVR-World", atom["text"])

    def test_unrelated_claim_returns_none(self) -> None:
        self.assertIsNone(
            match_claim_to_evidence("전혀 다른 주제의 정원 고양이 이야기", self.atoms)
        )

    def test_empty_inputs_return_none(self) -> None:
        self.assertIsNone(match_claim_to_evidence("", self.atoms))
        self.assertIsNone(match_claim_to_evidence("아무 주장", []))


class LoadAtomsFromPayloadTests(unittest.TestCase):
    def test_accepts_wrapped_and_bare_lists(self) -> None:
        atoms = [{"localizedClaim": "주장", "text": "x"}]
        self.assertEqual(load_atoms_from_payload({"atoms": atoms}), atoms)
        self.assertEqual(load_atoms_from_payload(atoms), atoms)

    def test_filters_non_dicts_and_claimless(self) -> None:
        payload = {"atoms": [{"localizedClaim": "ok", "text": "y"}, {"text": "no claim"}, 7]}
        self.assertEqual(load_atoms_from_payload(payload), [{"localizedClaim": "ok", "text": "y"}])

    def test_malformed_returns_empty(self) -> None:
        self.assertEqual(load_atoms_from_payload(None), [])
        self.assertEqual(load_atoms_from_payload("nope"), [])


class GetCitationEvidenceFirstTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._prev_env = os.environ.get("VERITAS_OUTPUT_DIR")
        os.environ["VERITAS_OUTPUT_DIR"] = str(self._root)
        ws = self._root / "WS"
        (ws / "clean_md").mkdir(parents=True, exist_ok=True)
        (ws / "summary" / "citation_evidence").mkdir(parents=True, exist_ok=True)
        (ws / "clean_md" / "000.md").write_text(_EN_SOURCE, encoding="utf-8")
        atoms = build_evidence_atoms(
            "000",
            {
                "evidence": [
                    {
                        "claim": "PLSM 모델이 Atari 에서 5.6 포인트 향상을 달성했다",
                        "quote": "The PLSM model achieved a 5.6 point improvement on Atari benchmarks.",
                    }
                ]
            },
            _EN_SOURCE,
        )
        (ws / "summary" / "citation_evidence" / "000.json").write_text(
            json.dumps({"doc_id": "000", "atoms": atoms}, ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        if self._prev_env is None:
            os.environ.pop("VERITAS_OUTPUT_DIR", None)
        else:
            os.environ["VERITAS_OUTPUT_DIR"] = self._prev_env
        self._tmp.cleanup()

    def test_korean_final_claim_resolves_evidence_anchor(self) -> None:
        result = svc.get_citation(
            "WS", "doc_000", "PLSM 모델은 Atari 에서 5.6 포인트 성능 향상 [doc_000]"
        )
        self.assertEqual(result["resolution"], "evidence_anchor")
        self.assertIsNotNone(result["match"])
        self.assertEqual(result["match"]["matchSource"], "evidence_anchor")
        self.assertIn("PLSM model", result["match"]["text"])
        self.assertEqual(result["match"]["evidenceId"], "doc_000-e0")

    def test_unrelated_claim_falls_through_to_document_only(self) -> None:
        # No evidence atom matches and the source has no related sentence, so the
        # evidence step must not force a wrong anchor — fall through honestly.
        result = svc.get_citation("WS", "doc_000", "고양이가 정원에서 잠을 잤다 맑음 날씨")
        self.assertNotEqual(result["resolution"], "evidence_anchor")
        self.assertIsNone(result["match"])


class BuildFinalCitationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence_by_doc = {
            "000": build_evidence_atoms(
                "000",
                {
                    "evidence": [
                        {
                            "claim": "PLSM 모델이 Atari 에서 5.6 포인트 향상을 달성했다",
                            "quote": "The PLSM model achieved a 5.6 point improvement on Atari benchmarks.",
                        }
                    ]
                },
                _EN_SOURCE,
            )
        }

    def test_body_marker_resolves_source_notes_stays_document_only(self) -> None:
        md = (
            "## Consolidated Findings\n"
            "PLSM 모델은 Atari 에서 5.6 포인트 성능 향상을 보였다 [doc_000].\n\n"
            "## Source Notes\n"
            "| Doc ID | Title | Year | What | Caveat |\n"
            "|---|---|---|---|---|\n"
            "| [doc_000] | PLSM | 2025 | 기여 | High |\n"
        )
        out = build_final_citations(md, self.evidence_by_doc)
        occ = out["occurrences"]
        self.assertEqual(len(occ), 2)
        self.assertEqual(occ[0]["resolution"], "evidence_anchor")
        self.assertEqual(occ[0]["evidenceId"], "doc_000-e0")
        # The Source Notes Doc ID is a document description, not a claim.
        self.assertEqual(occ[1]["resolution"], "document_only")
        self.assertEqual(out["counts"]["evidence_anchor"], 1)
        self.assertEqual(out["counts"]["document_only"], 1)

    def test_fenced_markers_ignored(self) -> None:
        md = "```\n샘플 [doc_000]\n```\n본문 PLSM Atari 5.6 포인트 향상 [doc_000]."
        out = build_final_citations(md, self.evidence_by_doc)
        # Only the body marker is counted; the fenced one is skipped.
        self.assertEqual(out["counts"]["total"], 1)

    def test_unknown_doc_resolves_document_only(self) -> None:
        md = "근거가 약한 문장 [doc_009]."
        out = build_final_citations(md, self.evidence_by_doc)
        self.assertEqual(out["occurrences"][0]["resolution"], "document_only")


if __name__ == "__main__":
    unittest.main()
