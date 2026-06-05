from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from benchmarks.drb import analyze_results as ar


class PairedStatsTests(unittest.TestCase):
    def test_paired_deltas_only_uses_shared_ids(self) -> None:
        a = {"1": {"overall_score": 0.6}, "2": {"overall_score": 0.4}, "3": {"overall_score": 0.5}}
        b = {"1": {"overall_score": 0.5}, "2": {"overall_score": 0.5}, "9": {"overall_score": 0.9}}
        deltas = ar.paired_deltas(a, b)
        ids = [d[0] for d in deltas]
        self.assertEqual(ids, ["1", "2"])
        self.assertAlmostEqual(deltas[0][3], 0.1)
        self.assertAlmostEqual(deltas[1][3], -0.1)

    def test_summary_win_rate_and_mean(self) -> None:
        deltas = [("1", 0.6, 0.5, 0.1), ("2", 0.4, 0.5, -0.1), ("3", 0.7, 0.5, 0.2)]
        summary = ar.summarize_deltas(deltas, n_boot=500)
        self.assertEqual(summary["n"], 3)
        self.assertEqual(summary["wins"], 2)
        self.assertEqual(summary["losses"], 1)
        self.assertAlmostEqual(summary["win_rate"], 2 / 3)
        self.assertAlmostEqual(summary["mean_delta"], (0.1 - 0.1 + 0.2) / 3)

    def test_bootstrap_ci_is_deterministic(self) -> None:
        values = [0.1, -0.1, 0.2, -0.05, 0.15, 0.0, 0.3, -0.2]
        first = ar.bootstrap_ci(values, seed=12345, n_boot=1000)
        second = ar.bootstrap_ci(values, seed=12345, n_boot=1000)
        self.assertEqual(first, second)
        lo, hi = first
        self.assertLessEqual(lo, hi)
        self.assertGreaterEqual(lo, min(values))
        self.assertLessEqual(hi, max(values))

    def test_empty_deltas_summary(self) -> None:
        self.assertEqual(ar.summarize_deltas([]), {"n": 0})


class ParsingTests(unittest.TestCase):
    def test_parse_race_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "race_result.txt"
            path.write_text(
                "Comprehensiveness: 0.6000\nInsight: 0.5500\n"
                "Instruction Following: 0.5000\nReadability: 0.7000\n"
                "Overall Score: 0.5800\n",
                encoding="utf-8",
            )
            agg = ar.parse_race_aggregate(path)
            self.assertAlmostEqual(agg["overall_score"], 0.58)
            self.assertAlmostEqual(agg["instruction_following"], 0.5)

    def test_parse_fact_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fact_result.txt"
            path.write_text(
                "total_citations: 12.5\ntotal_valid_citations: 9.0\nvalid_rate: 0.72\n",
                encoding="utf-8",
            )
            agg = ar.parse_fact_aggregate(path)
            self.assertAlmostEqual(agg["valid_rate"], 0.72)

    def test_parse_race_per_task_skips_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw_results.jsonl"
            path.write_text(
                '{"id": 1, "overall_score": 0.6, "insight": 0.5}\n'
                '{"id": 2, "error": "boom"}\n'
                '{"id": 3, "overall_score": 0.4}\n',
                encoding="utf-8",
            )
            per_task = ar.parse_race_per_task(path)
            self.assertEqual(set(per_task), {"1", "3"})
            self.assertAlmostEqual(per_task["1"]["overall_score"], 0.6)


class AnalyzeModeTests(unittest.TestCase):
    def _make_results(self, root: Path, model: str, *, per_task: bool, overall: float) -> None:
        race_dir = root / "results" / "race" / model
        fact_dir = root / "results" / "fact" / model
        race_dir.mkdir(parents=True, exist_ok=True)
        fact_dir.mkdir(parents=True, exist_ok=True)
        (race_dir / "race_result.txt").write_text(
            f"Comprehensiveness: {overall}\nInsight: {overall}\n"
            f"Instruction Following: {overall}\nReadability: {overall}\n"
            f"Overall Score: {overall}\n",
            encoding="utf-8",
        )
        (fact_dir / "fact_result.txt").write_text(
            "total_citations: 10.0\ntotal_valid_citations: 7.0\nvalid_rate: 0.7\n",
            encoding="utf-8",
        )
        if per_task:
            (race_dir / "raw_results.jsonl").write_text(
                f'{{"id": 1, "overall_score": {overall}}}\n'
                f'{{"id": 2, "overall_score": {overall - 0.05}}}\n',
                encoding="utf-8",
            )

    def test_paired_mode_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_results(root, "sysA", per_task=True, overall=0.62)
            self._make_results(root, "sysB", per_task=True, overall=0.55)
            result = ar.analyze(drb_root=root, system_a="sysA", system_b="sysB", n_boot=200)
            self.assertEqual(result["mode"], "paired")
            self.assertEqual(result["summary"]["n"], 2)
            self.assertEqual(result["summary"]["wins"], 2)  # sysA wins both tasks

            out = ar.write_outputs(result, root / "out", label="budget_judge")
            self.assertTrue(out["summary_csv"].is_file())
            self.assertTrue(out["paired_deltas_csv"].is_file())
            report = out["comparison_report_md"].read_text(encoding="utf-8")
            self.assertIn("budget_judge", report)
            self.assertIn("Win rate", report)

    def test_aggregate_only_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_results(root, "sysA", per_task=False, overall=0.7)
            self._make_results(root, "sysB", per_task=False, overall=0.6)
            result = ar.analyze(drb_root=root, system_a="sysA", system_b="sysB")
            self.assertEqual(result["mode"], "aggregate_only")
            self.assertFalse(result["per_task_available"])
            # Falls back to race_result.txt means.
            self.assertAlmostEqual(result["race_means_a"]["overall_score"], 0.7)
            self.assertEqual(result["summary"], {"n": 0})


if __name__ == "__main__":
    unittest.main()
