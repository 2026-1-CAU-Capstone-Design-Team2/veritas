from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from benchmarks.drb import drb_vendor


_REPO_ROOT = Path(__file__).resolve().parents[1]


class DRBVendorLayoutTests(unittest.TestCase):
    def test_current_checkout_layout_is_valid(self) -> None:
        self.assertTrue(drb_vendor.is_valid_layout())
        root = drb_vendor.validate_layout()
        self.assertTrue(root.is_dir())
        self.assertEqual(drb_vendor.missing_layout_entries(), [])

    def test_query_file_path_default(self) -> None:
        path = drb_vendor.query_file_path()
        self.assertEqual(path.name, "query.jsonl")
        self.assertTrue(path.is_file())

    def test_default_raw_output_path(self) -> None:
        path = drb_vendor.raw_output_path("veritas_autosurvey_local_m15")
        self.assertEqual(path.name, "veritas_autosurvey_local_m15.jsonl")
        self.assertEqual(
            path.parent.relative_to(drb_vendor.resolve_drb_root()).as_posix(),
            "data/test_data/raw_data",
        )

    def test_meta_output_path(self) -> None:
        path = drb_vendor.meta_output_path("flat_local_web_m15")
        self.assertTrue(path.name.endswith(".jsonl.meta.jsonl"))

    def test_traversal_root_rejected(self) -> None:
        for bad in ("../escape", "deep_research_bench/../..", "a/../../b"):
            with self.assertRaises(drb_vendor.DRBLayoutError):
                drb_vendor.resolve_drb_root(bad)

    def test_bad_model_name_rejected(self) -> None:
        for bad in ("../evil", "a/b", "with space", ""):
            with self.assertRaises(drb_vendor.DRBLayoutError):
                drb_vendor.raw_output_path(bad)

    def test_missing_entries_reported_for_empty_root(self) -> None:
        # An existing-but-empty dir reports every required entry as missing
        # (resolve still succeeds; only the contents are absent).
        missing = drb_vendor.missing_layout_entries(_REPO_ROOT / "benchmarks")
        self.assertIn("README.md", missing)
        self.assertIn("utils/", missing)

    def test_generated_artifacts_are_git_ignored(self) -> None:
        if not (_REPO_ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        try:
            subprocess.run(
                ["git", "--version"], cwd=_REPO_ROOT, capture_output=True, check=True
            )
        except (OSError, subprocess.CalledProcessError):  # pragma: no cover
            self.skipTest("git unavailable")

        ignored_examples = [
            "runs/drb/x/final.md",
            "deep_research_bench/data/test_data/raw_data/veritas_autosurvey_local_m15.jsonl",
            "deep_research_bench/data/test_data/raw_data/flat_local_web_m15.jsonl",
            "deep_research_bench/data/test_data/raw_data/veritas_autosurvey_local_m15.jsonl.meta.jsonl",
            "deep_research_bench/results/race/flat_local_web_m15/race_result.txt",
            "deep_research_bench/results/fact/veritas_autosurvey_local_m15/fact_result.txt",
            "bench_results/drb/cmp/summary.csv",
        ]
        for rel in ignored_examples:
            proc = subprocess.run(
                ["git", "check-ignore", rel],
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"expected {rel} to be git-ignored")


if __name__ == "__main__":
    unittest.main()
