from __future__ import annotations

import re
from typing import Any


ALLOWED_AUTOSURVEY_MEMORY_CATEGORIES = frozenset(
    {"preference", "profile", "constraint", "project"}
)

_CATEGORY_LABELS = {
    "preference": "Preference",
    "profile": "Profile",
    "constraint": "Constraint",
    "project": "Project",
}
_DEFAULT_MAX_ITEMS = 8
_DEFAULT_MAX_ITEM_CHARS = 180
_DEFAULT_MAX_BRIEF_CHARS = 1200


def build_autosurvey_memory_brief(
    memory_runtime: Any,
    user_request: str = "",
    *,
    max_items: int = _DEFAULT_MAX_ITEMS,
    max_item_chars: int = _DEFAULT_MAX_ITEM_CHARS,
    max_chars: int = _DEFAULT_MAX_BRIEF_CHARS,
) -> str:
    """Build a bounded planning-only memory brief for chat-triggered AutoSurvey.

    This reads structured working-context facts only. It intentionally excludes
    recall results, FIFO chat history, source text, screen captures, and any
    uncategorized records.
    """
    del user_request  # Reserved for future relevance filtering without changing callers.

    records = _load_working_records(memory_runtime)
    if not records:
        return ""

    selected: list[tuple[str, str]] = []
    seen: set[str] = set()
    item_limit = max(0, int(max_items or 0))
    if item_limit <= 0:
        return ""

    for row in records:
        if not isinstance(row, dict):
            continue
        category = _record_category(row)
        if category not in ALLOWED_AUTOSURVEY_MEMORY_CATEGORIES:
            continue

        text = _clean_text(row.get("text") or row.get("content") or "")
        if not text:
            continue

        text = _truncate(text, max_item_chars)
        key = f"{category}:{text}".casefold()
        if key in seen:
            continue
        seen.add(key)
        selected.append((category, text))
        if len(selected) >= item_limit:
            break

    if not selected:
        return ""

    lines = [
        "Planning-only user context. Use as preferences/constraints, not as evidence:",
    ]
    lines.extend(
        f"- {_CATEGORY_LABELS.get(category, category.title())}: {text}"
        for category, text in selected
    )
    return _truncate("\n".join(lines), max_chars)


def _load_working_records(memory_runtime: Any) -> list[dict[str, Any]]:
    working = getattr(memory_runtime, "working", None)
    records_func = getattr(working, "records", None)
    if not callable(records_func):
        return []
    try:
        records = records_func()
    except Exception:
        return []
    return records if isinstance(records, list) else []


def _record_category(row: dict[str, Any]) -> str:
    tags = row.get("tags") or []
    if isinstance(tags, list):
        for tag in tags:
            tag_text = str(tag or "").strip().lower()
            if tag_text.startswith("category:"):
                return tag_text.split(":", 1)[1].strip()

    raw_category = str(row.get("category") or "").strip().lower()
    return raw_category


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("-"):
        text = text[1:].strip()
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str, max_chars: int) -> str:
    limit = max(0, int(max_chars or 0))
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: max(0, limit - 3)].rstrip() + "..."


__all__ = [
    "ALLOWED_AUTOSURVEY_MEMORY_CATEGORIES",
    "build_autosurvey_memory_brief",
]
