from __future__ import annotations

import unittest

from core.report_markdown_normalizer import normalize_final_report_markdown


_HEADER = "| Doc ID | Title / Type | Year | What it contributes | Reliability / Caveat |"
_SEPARATOR = "|---|---|---|---|---|"


def _source_notes_lines(md: str) -> list[str]:
    lines = md.split("\n")
    start = next(i for i, l in enumerate(lines) if l.strip().lower().startswith("## source notes"))
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        out.append(line)
    return out


class SourceNotesNormalizerTests(unittest.TestCase):
    def test_bullet_prefixed_rows_become_canonical_table(self) -> None:
        md = (
            "# Final Research Brief\n\n"
            "## Source Notes\n"
            "- [doc_000] | RLVR-World | 2025 | RL world model | High\n"
            "- doc_001 | PLSM | 2025 | latent dynamics | Medium\n\n"
            "## Remaining Gaps\n- None\n"
        )
        out = normalize_final_report_markdown(md)
        notes = [l for l in _source_notes_lines(out) if l.strip()]
        self.assertEqual(notes[0], _HEADER)
        self.assertEqual(notes[1], _SEPARATOR)
        self.assertIn("| [doc_000] | RLVR-World | 2025 | RL world model | High |", notes)
        self.assertIn("| [doc_001] | PLSM | 2025 | latent dynamics | Medium |", notes)
        # No bullet-prefixed rows survive.
        self.assertFalse(any(l.lstrip().startswith("-") and "|" in l for l in notes))

    def test_missing_separator_is_inserted(self) -> None:
        md = (
            "## Source Notes\n"
            f"{_HEADER}\n"
            "| [doc_000] | Title | 2025 | contributes | High |\n"
        )
        out = normalize_final_report_markdown(md)
        notes = [l for l in _source_notes_lines(out) if l.strip()]
        self.assertEqual(notes[0], _HEADER)
        self.assertEqual(notes[1], _SEPARATOR)
        self.assertEqual(notes.count(_SEPARATOR), 1)

    def test_short_and_bare_doc_ids_become_zero_padded_bracketed(self) -> None:
        md = (
            "## Source Notes\n"
            "| doc_1 | A | 2025 | x | High |\n"
            "| [doc_2] | B | 2025 | y | Low |\n"
            "| doc-3 | C | 2025 | z | Medium |\n"
        )
        out = normalize_final_report_markdown(md)
        joined = "\n".join(_source_notes_lines(out))
        self.assertIn("| [doc_001] |", joined)
        self.assertIn("| [doc_002] |", joined)
        self.assertIn("| [doc_003] |", joined)

    def test_unknown_value_dash_is_preserved(self) -> None:
        md = "## Source Notes\n| doc_5 | Title | - | contributes | - |\n"
        out = normalize_final_report_markdown(md)
        self.assertIn("| [doc_005] | Title | - | contributes | - |", out)

    def test_only_source_notes_section_is_touched(self) -> None:
        body = (
            "# Final Research Brief\n\n"
            "## Consolidated Findings\n"
            "| Approach | Formula |\n|---|---|\n| MDP | $s_{t+1}$ |\n\n"
            "Inline math $p(z_t)$ and a - bullet line stay as-is.\n\n"
            "## Source Notes\n"
            "- doc_1 | A | 2025 | x | High |\n"
        )
        out = normalize_final_report_markdown(md=body)
        # The findings table + prose are byte-identical.
        self.assertIn("| Approach | Formula |\n|---|---|\n| MDP | $s_{t+1}$ |", out)
        self.assertIn("Inline math $p(z_t)$ and a - bullet line stay as-is.", out)
        # Source Notes got normalized.
        self.assertIn("| [doc_001] | A | 2025 | x | High |", out)

    def test_idempotent(self) -> None:
        md = (
            "## Source Notes\n"
            "- doc_1 | A | 2025 | x | High |\n"
        )
        once = normalize_final_report_markdown(md)
        twice = normalize_final_report_markdown(once)
        self.assertEqual(once, twice)

    def test_no_source_notes_section_returns_unchanged(self) -> None:
        md = "# Report\n\n## Findings\n| A | B |\n|---|---|\n| 1 | 2 |\n"
        self.assertEqual(normalize_final_report_markdown(md), md)

    def test_none_placeholder_not_turned_into_table(self) -> None:
        md = "## Source Notes\n- None\n\n## Remaining Gaps\n- None\n"
        out = normalize_final_report_markdown(md)
        # No pipe table fabricated when there are no rows.
        self.assertNotIn("|", _source_notes_lines(out)[0] if _source_notes_lines(out) else "")
        self.assertEqual(out, md)


if __name__ == "__main__":
    unittest.main()
