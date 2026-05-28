"""CallRequest + runtime state → OpenAI chat messages[] 변환."""

from __future__ import annotations

from typing import Any

from core.memory.budget import MemoryBudget
from core.memory.request import CallRequest
from services.memory_tools_funcs.main_context.queue_manage import QueueManager
from services.memory_tools_funcs.main_context.system_instruction import assemble_system_message
from services.memory_tools_funcs.main_context.working_context import WorkingContextManager
from services.memory_tools_funcs.store import MemoryStore


def build_messages(
    *,
    req: CallRequest,
    budget: MemoryBudget,
    store: MemoryStore,
    working: WorkingContextManager,
    queue: QueueManager,
    history_limit: int = 6,
) -> list[dict[str, Any]]:
    """CallRequest 한 건에서 messages[]를 만든다.

    history_limit=6 (= recent 3 turn pair). 옛 turn은 summary/recall에서 끌어옴.
    """
    c = req.constraints

    include_working = (
        not c.grounded
        and not c.json_strict
        and not working.is_empty()
    )
    fifo_summary = (
        ""
        if (c.grounded or c.json_strict)
        else store.load_latest_summary()
    )
    warn_pressure = (
        not c.json_strict
        and queue.is_warning_pressure(budget)
    )

    system_msg = assemble_system_message(
        task_instruction=req.task_instruction,
        working_context=working.load() if include_working else "",
        fifo_summary=fifo_summary,
        memory_pressure=warn_pressure,
        json_strict=c.json_strict,
    )

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_msg}]

    if req.use_history and not c.json_strict and not c.latency_critical:
        messages.extend(queue.as_chat_messages(limit=history_limit))

    messages.append({"role": "user", "content": req.user_content})

    return messages
