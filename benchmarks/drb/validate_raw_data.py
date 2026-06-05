"""Validate a DRB raw-output file before handing it to the official evaluator.

Checks each row of ``<model>.jsonl``:

* contains exactly the official keys ``id`` / ``prompt`` / ``article`` (no run
  metadata leaked in),
* has a non-empty article,
* has at least one inline numeric citation ``[n]``,
* has a ``## References`` section that contains URL-like entries.

Pure validators (importable + testable); a thin CLI prints a per-file report and
exits non-zero on any failure.

    python -m benchmarks.drb.validate_raw_data deep_research_bench/data/test_data/raw_data/<model>.jsonl
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from benchmarks.drb.drb_io import OFFICIAL_KEYS, iter_json_objects


_NUMERIC_CITE_RE = re.compile(r"\[\d+\]")
_URL_RE = re.compile(r"https?://\S+")
_REF_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+References\b", re.IGNORECASE | re.MULTILINE)


def validate_article(article: Any) -> list[str]:
    """Return a list of issues with one article (empty list = valid)."""
    text = str(article or "")
    if not text.strip():
        return ["empty article"]
    issues: list[str] = []
    if not _NUMERIC_CITE_RE.search(text):
        issues.append("no inline numeric [n] citations")
    heading = _REF_HEADING_RE.search(text)
    if not heading:
        issues.append("missing ## References section")
    elif not _URL_RE.search(text[heading.start():]):
        issues.append("References section has no URL-like entries")
    return issues


def validate_row(row: Any, *, allow_extra_keys: bool = False) -> list[str]:
    """Return a list of issues with one raw row (empty list = valid)."""
    if not isinstance(row, dict):
        return ["row is not a JSON object"]
    issues: list[str] = []
    keys = set(row.keys())
    missing = set(OFFICIAL_KEYS) - keys
    if missing:
        issues.append(f"missing keys: {sorted(missing)}")
    if not allow_extra_keys:
        extra = keys - set(OFFICIAL_KEYS)
        if extra:
            issues.append(f"unexpected keys: {sorted(extra)}")
    issues.extend(validate_article(row.get("article")))
    return issues


def validate_file(path: str | Path, *, allow_extra_keys: bool = False) -> dict[str, Any]:
    """Validate every row of a raw JSONL file; return a structured report."""
    text = Path(path).read_text(encoding="utf-8")
    rows = list(iter_json_objects(text))
    failures: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        issues = validate_row(row, allow_extra_keys=allow_extra_keys)
        if issues:
            failures.append({"index": index, "id": row.get("id") if isinstance(row, dict) else None, "issues": issues})
    return {
        "path": str(path),
        "rows": len(rows),
        "failures": failures,
        "ok": not failures and len(rows) > 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate DRB raw-output JSONL.")
    parser.add_argument("paths", nargs="+", help="One or more raw <model>.jsonl files.")
    parser.add_argument("--allow-extra-keys", action="store_true", help="Permit keys beyond id/prompt/article.")
    args = parser.parse_args(argv)

    all_ok = True
    for path in args.paths:
        report = validate_file(path, allow_extra_keys=args.allow_extra_keys)
        status = "OK" if report["ok"] else "FAIL"
        print(f"[{status}] {report['path']} — {report['rows']} row(s), {len(report['failures'])} failing")
        for failure in report["failures"]:
            print(f"    row {failure['index']} (id={failure['id']}): {'; '.join(failure['issues'])}")
        all_ok = all_ok and report["ok"]
    return 0 if all_ok else 1


__all__ = ["validate_article", "validate_row", "validate_file"]


if __name__ == "__main__":
    raise SystemExit(main())
