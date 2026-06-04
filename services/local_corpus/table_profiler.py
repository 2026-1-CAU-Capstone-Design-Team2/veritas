from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable

from core.local_corpus_models import TableColumnProfile, TableProfile


def profile_csv(source_id: str, path: Path, *, max_rows: int = 200) -> TableProfile:
    rows: list[list[str]] = []
    with Path(path).open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        for idx, row in enumerate(reader):
            rows.append([str(cell or "") for cell in row])
            if idx >= max_rows:
                break
    return profile_rows(source_id, None, rows)


def profile_rows(
    source_id: str,
    sheet_name: str | None,
    rows: list[list[Any]],
) -> TableProfile:
    if not rows:
        return TableProfile(
            source_id=source_id,
            sheet_name=sheet_name,
            row_count=0,
            column_count=0,
            columns=[],
            sample_rows_markdown="",
            summary_markdown="Empty table.",
        )

    header = [str(cell or "").strip() or f"column_{idx + 1}" for idx, cell in enumerate(rows[0])]
    data_rows = [[str(cell or "").strip() for cell in row] for row in rows[1:]]
    width = len(header)
    normalized_rows = [row + [""] * max(0, width - len(row)) for row in data_rows]
    normalized_rows = [row[:width] for row in normalized_rows]

    columns: list[TableColumnProfile] = []
    for idx, name in enumerate(header):
        values = [row[idx] for row in normalized_rows if idx < len(row)]
        columns.append(_profile_column(name, values))

    sample = _rows_to_markdown(header, normalized_rows[:8])
    sheet = f" sheet '{sheet_name}'" if sheet_name else ""
    summary = (
        f"Table{sheet}: {len(data_rows)} rows, {len(header)} columns.\n"
        f"Columns: {', '.join(header[:20])}."
    )
    if sample:
        summary = f"{summary}\n\nSample rows:\n{sample}"

    return TableProfile(
        source_id=source_id,
        sheet_name=sheet_name,
        row_count=len(data_rows),
        column_count=len(header),
        columns=columns,
        sample_rows_markdown=sample,
        summary_markdown=summary,
    )


def _profile_column(name: str, values: list[str]) -> TableColumnProfile:
    non_empty = [value for value in values if value != ""]
    numbers: list[float] = []
    for value in non_empty:
        try:
            numbers.append(float(value.replace(",", "")))
        except ValueError:
            pass
    inferred = "number" if non_empty and len(numbers) >= max(1, len(non_empty) // 2) else "text"
    sample_values = []
    seen: set[str] = set()
    for value in non_empty:
        if value in seen:
            continue
        seen.add(value)
        sample_values.append(value)
        if len(sample_values) >= 5:
            break
    mean = sum(numbers) / len(numbers) if numbers else None
    return TableColumnProfile(
        name=name,
        inferred_type=inferred,
        null_count=len(values) - len(non_empty),
        non_null_count=len(non_empty),
        sample_values=sample_values,
        min_value=min(numbers) if numbers else None,
        max_value=max(numbers) if numbers else None,
        mean_value=mean,
    )


def _rows_to_markdown(header: list[str], rows: list[list[str]]) -> str:
    if not header:
        return ""
    lines = [
        "| " + " | ".join(_escape(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in rows:
        padded = row + [""] * max(0, len(header) - len(row))
        lines.append("| " + " | ".join(_escape(cell) for cell in padded[: len(header)]) + " |")
    return "\n".join(lines)


def _escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


__all__ = ["profile_csv", "profile_rows"]
