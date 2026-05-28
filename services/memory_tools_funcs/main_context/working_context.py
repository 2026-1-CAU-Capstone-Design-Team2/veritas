"""working_context.json의 read/write."""

from __future__ import annotations

from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


class WorkingContextManager:
    """working_context의 load/save/append."""

    def __init__(self, store: MemoryStore, token_counter: TokenCounter) -> None:
        self.store = store
        self.token_counter = token_counter

    def load(self) -> str:
        """현재 working context 본문."""
        return self.store.load_working_context()

    def save(self, content: str) -> None:
        """working context를 통째로 덮어쓴다."""
        self.store.save_working_context(str(content or ""))

    def is_empty(self) -> bool:
        """비어있는지."""
        return not self.load().strip()

    def token_count(self) -> int:
        """추정 token 수."""
        return self.token_counter.count(self.load())

    def append_fact(self, fact: str) -> None:
        """새 사실 한 줄을 덧붙인다."""
        fact = str(fact or "").strip()
        if not fact:
            return
        current = self.load()
        merged = f"{current}\n- {fact}" if current else f"- {fact}"
        self.save(merged.strip())

    def replace_fact(self, old: str, new: str) -> bool:
        """old 문자열을 new로 치환한다. old가 없으면 False."""
        old = str(old or "").strip()
        new = str(new or "").strip()
        if not old:
            return False
        current = self.load()
        if old not in current:
            return False
        self.save(current.replace(old, new, 1))
        return True
