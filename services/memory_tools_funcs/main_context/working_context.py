"""Record-based working context management."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


DEFAULT_WORKING_CONTEXT_TOKENS = 1200

# Categories whose fact is single-valued: a new declaration replaces the prior
# one instead of letting a contradiction accumulate (e.g. the user's name or
# project). Other categories — including ``None`` and "remember" — accumulate.
_SINGLE_VALUE_CATEGORIES = frozenset({"name", "project"})


def _category_tag(category: str) -> str:
    return f"category:{category}"


def utc_now_iso() -> str:
    """Current UTC timestamp in ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


class WorkingContextManager:
    """Load, save, and edit working-context records."""

    def __init__(self, store: MemoryStore, token_counter: TokenCounter) -> None:
        self.store = store
        self.token_counter = token_counter

    def load(self) -> str:
        """Return prompt-ready working context text."""
        return self.store.format_working_records(self.records())

    def records(self) -> list[dict[str, Any]]:
        """Return structured working-context records."""
        return self.store.load_working_records()

    def save(self, content: str, *, max_tokens: int | None = None) -> None:
        """Save flat text after converting it to records."""
        records = self.store.working_records_from_text(
            content,
            source="manual",
            updated_at=utc_now_iso(),
        )
        self._save_records(records, max_tokens=max_tokens)

    def is_empty(self) -> bool:
        """Return whether working context has no records."""
        return not self.records()

    def token_count(self) -> int:
        """Approximate token count for prompt-ready working context."""
        return self.token_counter.count(self.load())

    def append_fact(
        self,
        fact: str,
        *,
        category: str | None = None,
        source: str = "heuristic",
        confidence: float = 1.0,
        tags: list[str] | None = None,
        max_tokens: int | None = None,
    ) -> bool:
        """Append one stable fact record, replacing the prior value of a
        single-valued ``category`` (name/project) so contradictions don't
        accumulate. Returns False when nothing changed (exact duplicate, or the
        category already holds this value)."""
        text = self._clean_fact(fact)
        if not text:
            return False
        current = self.records()
        cat = str(category or "").strip()

        if cat in _SINGLE_VALUE_CATEGORIES:
            cat_tag = _category_tag(cat)
            same_category = [row for row in current if cat_tag in (row.get("tags") or [])]
            normalized = self._normalize_fact(text)
            if any(self._normalize_fact(row.get("text")) == normalized for row in same_category):
                return False
            # Drop the stale value(s) for this category before adding the new one.
            current = [row for row in current if cat_tag not in (row.get("tags") or [])]
        else:
            key = self._normalize_fact(text)
            if key in {self._normalize_fact(row.get("text")) for row in current}:
                return False

        row_tags = list(tags or [])
        if cat:
            row_tags.append(_category_tag(cat))
        current.append(
            {
                "id": str(uuid.uuid4()),
                "text": text,
                "source": str(source or "unknown"),
                "confidence": float(confidence),
                "tags": row_tags,
                "updated_at": utc_now_iso(),
            }
        )
        self._save_records(
            current,
            max_tokens=max_tokens or DEFAULT_WORKING_CONTEXT_TOKENS,
        )
        return True

    def replace_fact(
        self,
        old: str,
        new: str,
        *,
        source: str = "tool",
        confidence: float = 1.0,
        tags: list[str] | None = None,
        max_tokens: int | None = None,
    ) -> bool:
        """Replace the first record containing ``old`` with ``new``."""
        old_text = str(old or "").strip()
        new_text = self._clean_fact(new)
        if not old_text:
            return False
        current = self.records()
        for row in current:
            text = str(row.get("text") or "")
            if old_text not in text:
                continue
            row["text"] = text.replace(old_text, new_text, 1) if new_text else ""
            row["source"] = str(source or row.get("source") or "tool")
            row["confidence"] = float(confidence)
            row["tags"] = list(tags or row.get("tags") or [])
            row["updated_at"] = utc_now_iso()
            self._save_records(
                [record for record in current if str(record.get("text") or "").strip()],
                max_tokens=max_tokens or DEFAULT_WORKING_CONTEXT_TOKENS,
            )
            return True
        return False

    def _save_records(
        self,
        records: list[dict[str, Any]],
        *,
        max_tokens: int | None,
    ) -> None:
        self.store.save_working_records(
            self._compact_records(records, max_tokens=max_tokens)
        )

    def _compact_records(
        self,
        records: list[dict[str, Any]],
        *,
        max_tokens: int | None,
    ) -> list[dict[str, Any]]:
        deduped = self._dedupe_records(records)
        max_tokens = int(max_tokens or 0)
        if max_tokens <= 0:
            return deduped
        if self.token_counter.count(self.store.format_working_records(deduped)) <= max_tokens:
            return deduped

        selected_reversed: list[dict[str, Any]] = []
        for row in reversed(deduped):
            candidate = [row, *reversed(selected_reversed)]
            if self.token_counter.count(self.store.format_working_records(candidate)) > max_tokens:
                continue
            selected_reversed.append(row)
        return list(reversed(selected_reversed))

    @classmethod
    def _dedupe_records(cls, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in records:
            text = cls._clean_fact(row.get("text"))
            key = cls._normalize_fact(text)
            if not key or key in seen:
                continue
            copy = dict(row)
            copy["text"] = text
            selected.append(copy)
            seen.add(key)
        return selected

    @staticmethod
    def _clean_fact(fact: Any) -> str:
        text = str(fact or "").strip()
        if text.startswith("-"):
            text = text[1:].strip()
        return " ".join(text.split())

    @staticmethod
    def _normalize_fact(fact: Any) -> str:
        return " ".join(str(fact or "").split()).casefold()
