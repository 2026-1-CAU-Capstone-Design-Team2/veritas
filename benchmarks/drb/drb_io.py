"""DRB task loading and raw-output writing.

DRB's own ``query.jsonl`` is one JSON object per line, but model outputs and
intermediate files in the wild are not always clean JSONL — articles contain
embedded newlines and files are sometimes concatenated object streams. So the
reader here is a robust object iterator built on ``json.JSONDecoder().raw_decode``
rather than ``json.loads`` per line.

Two output shapes are produced:

* the **official** raw file (``<model>.jsonl``) — exactly ``id`` / ``prompt`` /
  ``article`` per row, the only thing the DRB evaluator reads, and
* a **sidecar** ``<model>.jsonl.meta.jsonl`` — run metadata (timings, budgets,
  counts, warnings). The sidecar carries no API keys or fetched bodies; callers
  pass only bounded metadata.

Pure I/O — no network, no LLM, no DRB import.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator


OFFICIAL_KEYS = ("id", "prompt", "article")

# A stray leading UTF-8 BOM (U+FEFF) — some editors and PowerShell
# ``Set-Content -Encoding utf8`` prepend one — would fail the first decode and
# drop the first record, so it is stripped before parsing.
_BOM = chr(0xFEFF)


@dataclass
class DRBTask:
    """One benchmark task. ``id`` is kept as-is (DRB uses ints) for round-trip."""

    id: Any
    prompt: str
    topic: str = ""
    language: str = ""


def iter_json_objects(text: str) -> Iterator[dict[str, Any]]:
    """Yield top-level JSON *objects* from *text*, tolerantly.

    Handles clean one-per-line JSONL, a concatenated object stream with no
    separators, blank lines, objects whose string values contain embedded
    newlines, and a leading UTF-8 BOM. A line that fails to decode is skipped
    (advance to the next newline) rather than aborting the whole file.
    """
    text = text.lstrip(_BOM)
    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            newline = text.find("\n", idx)
            if newline == -1:
                break
            idx = newline + 1
            continue
        idx = end
        if isinstance(obj, dict):
            yield obj


def _as_filter_set(values: Any, *, lower: bool = False) -> set[str] | None:
    """Normalize a filter arg (None / scalar / iterable) into a set or ``None``."""
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        items = [values]
    else:
        items = list(values)
    out = {str(v).strip() for v in items if str(v).strip()}
    return {v.lower() for v in out} if lower else out


def load_tasks(
    query_file: str | Path,
    *,
    limit: int | None = None,
    task_ids: Any = None,
    languages: Any = None,
    topics: Any = None,
) -> list[DRBTask]:
    """Load and filter tasks from a DRB ``query.jsonl``.

    Filters are applied before ``limit`` (so ``limit`` caps the *matching*
    tasks). Rows missing ``id`` or a non-empty ``prompt`` are skipped. ``topic``
    and ``language`` are preserved for stratified sampling and metadata.
    """
    text = Path(query_file).read_text(encoding="utf-8")
    id_filter = _as_filter_set(task_ids)
    lang_filter = _as_filter_set(languages, lower=True)
    topic_filter = _as_filter_set(topics, lower=True)

    tasks: list[DRBTask] = []
    for obj in iter_json_objects(text):
        if "id" not in obj or "prompt" not in obj:
            continue
        prompt = str(obj.get("prompt") or "").strip()
        if not prompt:
            continue
        task_id = obj["id"]
        topic = str(obj.get("topic") or "")
        language = str(obj.get("language") or "")
        if id_filter is not None and str(task_id) not in id_filter:
            continue
        if lang_filter is not None and language.lower() not in lang_filter:
            continue
        if topic_filter is not None and topic.lower() not in topic_filter:
            continue
        tasks.append(DRBTask(id=task_id, prompt=prompt, topic=topic, language=language))
        if limit is not None and limit > 0 and len(tasks) >= limit:
            break
    return tasks


def official_row(task_id: Any, prompt: str, article: str) -> dict[str, Any]:
    """Build a row with exactly the official keys."""
    return {"id": task_id, "prompt": prompt, "article": article}


def write_raw_jsonl(
    path: str | Path, rows: Iterable[dict[str, Any]], *, append: bool = False
) -> None:
    """Write official rows, emitting **only** ``id`` / ``prompt`` / ``article``.

    Any extra keys on an input row are dropped — the official file must never
    leak run metadata into the evaluator's input.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a" if append else "w", encoding="utf-8") as handle:
        for row in rows:
            official = {key: row.get(key) for key in OFFICIAL_KEYS}
            handle.write(json.dumps(official, ensure_ascii=False) + "\n")


def append_official_row(path: str | Path, task_id: Any, prompt: str, article: str) -> None:
    write_raw_jsonl(path, [official_row(task_id, prompt, article)], append=True)


def meta_path_for(raw_path: str | Path) -> Path:
    """Sidecar metadata path for a raw output file (``<raw>.meta.jsonl``)."""
    raw = Path(raw_path)
    return raw.with_name(raw.name + ".meta.jsonl")


def append_meta_row(raw_path: str | Path, meta: dict[str, Any]) -> None:
    """Append one metadata record to the sidecar. Caller supplies bounded data."""
    target = meta_path_for(raw_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(meta, ensure_ascii=False) + "\n")


def completed_task_ids(raw_path: str | Path) -> set[str]:
    """Ids already present in the raw file with a non-empty article (for resume)."""
    path = Path(raw_path)
    if not path.exists():
        return set()
    done: set[str] = set()
    for obj in iter_json_objects(path.read_text(encoding="utf-8")):
        if "id" in obj and str(obj.get("article") or "").strip():
            done.add(str(obj["id"]))
    return done


__all__ = [
    "OFFICIAL_KEYS",
    "DRBTask",
    "iter_json_objects",
    "load_tasks",
    "official_row",
    "write_raw_jsonl",
    "append_official_row",
    "meta_path_for",
    "append_meta_row",
    "completed_task_ids",
]
