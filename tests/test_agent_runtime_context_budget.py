from __future__ import annotations

import unittest
from unittest.mock import patch

from api.services.agent_runtime import AgentRuntime


class _RawLLM:
    n_ctx = 40960


class _MemoryRuntime:
    def __init__(self) -> None:
        self.max_context_tokens = 40960

    def update_n_ctx(self, value: int) -> None:
        self.max_context_tokens = int(value)


class AgentRuntimeContextBudgetTests(unittest.TestCase):
    def test_sync_llm_context_budget_uses_per_slot_context(self) -> None:
        runtime = object.__new__(AgentRuntime)
        runtime.raw_llm = _RawLLM()
        runtime.memory_runtime = _MemoryRuntime()

        with patch("llm.llama_supervisor.effective_context_per_slot", return_value=8192):
            applied = runtime._sync_llm_context_budget()

        self.assertEqual(applied, 8192)
        self.assertEqual(runtime.raw_llm.n_ctx, 8192)
        self.assertEqual(runtime.memory_runtime.max_context_tokens, 8192)


if __name__ == "__main__":
    unittest.main()
