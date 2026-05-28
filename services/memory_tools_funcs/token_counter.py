"""token 수 추정기 (chars//3 휴리스틱)."""

from __future__ import annotations


class TokenCounter:
    """단순 token 추정."""

    def count(self, text: str) -> int:
        """텍스트의 추정 token 수."""
        text = str(text or "")
        if not text:
            return 0
        return max(1, len(text) // 3)

    def count_messages(self, messages: list[str]) -> int:
        """텍스트 리스트의 합산 token 수."""
        return sum(self.count(m) for m in messages)
