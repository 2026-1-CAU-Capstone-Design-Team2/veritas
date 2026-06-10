"""Research page '수집된 문서 수' tile denominator clamping.

Regression for the "18 / 15" display: the denominator is the run's document
*cap*, so it can never be below the collected count. A stale/lower target
(a workspace re-opened in a fresh page where ``_max_docs`` fell back to the
default 15, or a persisted job predating the ``maxDocs`` field) must be clamped
up to the collected count for display, while a genuinely larger requested cap
(``18 / 20`` when a run stopped early) is preserved.

Skipped automatically when a (headless) QApplication can't be created.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:  # pragma: no cover - environment dependent
    from PySide6.QtWidgets import QApplication

    from frontend.ui.pages.research_page import ResearchPage

    _APP = QApplication.instance() or QApplication([])
    _QT_OK = True
except Exception:  # pragma: no cover - no Qt / no offscreen platform
    _QT_OK = False


@unittest.skipUnless(_QT_OK, "PySide6 offscreen QApplication unavailable")
class DocCountFormatTests(unittest.TestCase):
    fmt = staticmethod(ResearchPage._format_doc_count) if _QT_OK else None

    def test_stale_lower_target_is_clamped_up(self) -> None:
        # The reported bug: 18 collected but the denominator stuck at default 15.
        self.assertEqual(self.fmt(18, 15), "18 / 18건")

    def test_larger_requested_cap_is_preserved(self) -> None:
        # Requested 20, run stopped early at 18 → keep the informative "/ 20".
        self.assertEqual(self.fmt(18, 20), "18 / 20건")

    def test_exact_match(self) -> None:
        self.assertEqual(self.fmt(15, 15), "15 / 15건")

    def test_in_progress_below_target(self) -> None:
        self.assertEqual(self.fmt(3, 18), "3 / 18건")

    def test_zero_collected(self) -> None:
        self.assertEqual(self.fmt(0, 15), "0 / 15건")


if __name__ == "__main__":
    unittest.main()
