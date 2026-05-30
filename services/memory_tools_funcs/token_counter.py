"""Token counting with llama-server tokenizer fallback."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Any


class TokenCounter:
    """Count prompt tokens using llama-server when available.

    The remote tokenizer is best-effort: if the endpoint is unavailable or
    unsupported, the counter disables remote calls for the process lifetime and
    falls back to a conservative UTF-8 byte heuristic.
    """

    def __init__(
        self,
        raw_llm: Any | None = None,
        *,
        timeout_sec: float = 0.5,
        max_cache: int = 4096,
    ) -> None:
        self.raw_llm = raw_llm
        self.timeout_sec = float(timeout_sec)
        self.max_cache = max(0, int(max_cache))
        self._cache: OrderedDict[str, int] = OrderedDict()
        self._remote_disabled = not callable(getattr(raw_llm, "tokenize_count", None))

    def count(self, text: str) -> int:
        """Return an estimated or exact token count for text."""
        text = str(text or "")
        if not text:
            return 0

        cache_key = self._cache_key(text)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached

        count = self._count_remote(text)
        if count is None:
            count = self._fallback_count(text)

        self._remember(cache_key, count)
        return count

    def count_messages(self, messages: list[str]) -> int:
        """Return the sum of token counts for a list of strings."""
        return sum(self.count(m) for m in messages)

    def reset_remote(self) -> None:
        """Allow remote tokenization to be retried after a model/server refresh."""
        self._remote_disabled = not callable(getattr(self.raw_llm, "tokenize_count", None))
        self._cache.clear()

    def _count_remote(self, text: str) -> int | None:
        if self._remote_disabled:
            return None
        tokenize_count = getattr(self.raw_llm, "tokenize_count", None)
        if not callable(tokenize_count):
            self._remote_disabled = True
            return None

        token_count: int | None = None
        try:
            token_count = tokenize_count(text, timeout_sec=self.timeout_sec)
        except TypeError:
            try:
                token_count = tokenize_count(text)
            except Exception:
                token_count = None
        except Exception:
            token_count = None
        if isinstance(token_count, int) and token_count >= 0:
            return token_count
        self._remote_disabled = True
        return None

    @staticmethod
    def _fallback_count(text: str) -> int:
        # UTF-8 bytes are more conservative than len(text)//3 for Korean text
        # while staying close to the usual 4 bytes/token English heuristic.
        return max(1, len(text.encode("utf-8")) // 4)

    @staticmethod
    def _cache_key(text: str) -> str:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        return f"{len(text)}:{digest}"

    def _remember(self, key: str, value: int) -> None:
        if self.max_cache <= 0:
            return
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self.max_cache:
            self._cache.popitem(last=False)
