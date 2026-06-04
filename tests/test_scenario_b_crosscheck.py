"""Scenario B (대체육 + (주)그린테이블) integration test.

Regenerates the synthetic local files into a temp dir (the committed fixtures
under ``test_data/`` are git-ignored), parses the real ``.docx`` / ``.pdf`` via
the local-corpus :class:`ParserRegistry`, and runs the cross-check pipeline
against the frozen web claims. Asserts that the deliberately-offset internal
figures are flagged as numeric mismatches, the severe (>20%) case is NOT flagged
(by the pipeline's ratio gate), and the ``.csv`` / ``.xlsx`` table sums match the
recorded check values.

See ``tests/fixtures/generate_scenario_b.py`` for the data design.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from core.knowledge_models import (
    KnowledgeSourceRecord,
    PrivacyLabel,
    SourceKind,
    SourceScope,
)
from core.models import ParsedDocRecord
from services.local_corpus import ParserRegistry
from services.verification.crosscheck import run_crosscheck_pipeline
from tests.fixtures import generate_scenario_b as gen


try:
    from services.verification.tokenization import HybridTokenizer

    # Built once (Kiwi init is ~hundreds of ms) — the same tokenizer the real
    # VerificationService injects, so the Korean-morphology path is exercised.
    _TOKENIZER = HybridTokenizer()
    HAS_KIWI = True
except Exception:
    _TOKENIZER = None
    HAS_KIWI = False

_SUFFIX_KIND = {".docx": SourceKind.DOCX, ".pdf": SourceKind.PDF}


class ScenarioBCrosscheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.data_dir = root / "scenario_b"
        cls.expected = gen.generate(cls.data_dir, root / "scenario_b_expected")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _run_crosscheck(self):
        external = [
            ParsedDocRecord(
                doc_id=claim["doc_id"],
                title=claim["title"],
                domain=claim["domain"],
                key_points=claim["key_points"],
            )
            for claim in self.expected["web_claims"]
        ]
        registry = ParserRegistry()
        sources: list[KnowledgeSourceRecord] = []
        documents: dict[str, str] = {}
        for index, name in enumerate([gen.DOCX_NAME, gen.PDF_NAME]):
            source_id = f"local_{index}"
            parsed = registry.parse(source_id, self.data_dir / name)
            documents[source_id] = parsed.markdown_text
            sources.append(
                KnowledgeSourceRecord(
                    source_id=source_id,
                    workspace_id="scenario_b",
                    source_scope=SourceScope.LOCAL,
                    source_kind=_SUFFIX_KIND[Path(name).suffix],
                    title=name,
                    canonical_uri="local",
                    display_path=name,
                    privacy_label=PrivacyLabel.LOCAL_PRIVATE,
                    content_hash="h",
                )
            )
        return run_crosscheck_pipeline(
            external_docs=external,
            local_sources=sources,
            local_documents=documents,
            tokenizer=_TOKENIZER,
        )

    @unittest.skipUnless(HAS_KIWI, "Kiwi tokenizer required for Korean morphology")
    def test_intended_numeric_mismatches_are_flagged(self) -> None:
        flags = self._run_crosscheck().flags
        messages = [flag["message"] for flag in flags]
        for mismatch in self.expected["expected_mismatches"]:
            matched = any(
                f"external={mismatch['external']}" in msg
                and f"local={mismatch['local']}" in msg
                for msg in messages
            )
            self.assertTrue(
                matched,
                f"mismatch not flagged: {mismatch['metric']} "
                f"({mismatch['external']} vs {mismatch['local']}); flags={messages}",
            )

    @unittest.skipUnless(HAS_KIWI, "Kiwi tokenizer required for Korean morphology")
    def test_severe_mismatch_is_not_flagged_by_design(self) -> None:
        # The ratio gate treats a >20% gap as a different metric, not a
        # mis-stated one — so the severe FMI case must NOT surface as a flag.
        messages = " ".join(f["message"] for f in self._run_crosscheck().flags)
        for severe in self.expected["expected_severe_not_flagged"]:
            self.assertNotIn(f"local={severe['local']}", messages)

    @unittest.skipUnless(HAS_KIWI, "Kiwi tokenizer required for Korean morphology")
    def test_flags_are_exactly_the_intended_mismatches(self) -> None:
        flags = self._run_crosscheck().flags
        self.assertEqual(len(flags), len(self.expected["expected_mismatches"]))
        self.assertTrue(all(f["relation"] == "numeric_mismatch" for f in flags))

    def test_csv_table_sum_matches_recorded_checkvalue(self) -> None:
        check = self.expected["table_checks"][gen.CSV_NAME]
        total = 0
        rows = 0
        with (self.data_dir / gen.CSV_NAME).open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                total += int(row["매출(원)"].replace(",", ""))  # comma-formatted source
                rows += 1
        self.assertEqual(rows, check["rows"])
        self.assertEqual(total, check["total_매출(원)"])

    def test_xlsx_detail_sum_matches_recorded_checkvalue(self) -> None:
        import openpyxl

        check = self.expected["table_checks"][gen.XLSX_NAME]
        workbook = openpyxl.load_workbook(
            self.data_dir / gen.XLSX_NAME, read_only=True, data_only=True
        )
        rows = list(workbook["월별상세"].iter_rows(values_only=True))
        workbook.close()
        header = list(rows[0])
        col = header.index("매출(억원)")
        total = sum(int(str(row[col]).replace(",", "")) for row in rows[1:])
        self.assertEqual(len(rows) - 1, check["월별상세_rows"])
        self.assertEqual(total, check["월별상세_total_매출(억원)"])


if __name__ == "__main__":
    unittest.main()
