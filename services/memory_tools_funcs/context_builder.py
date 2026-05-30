"""Convert a CallRequest and memory state into OpenAI chat messages."""

from __future__ import annotations

from typing import Any

from core.memory.budget import MemoryBudget
from core.memory.policy import FixedKRetrievalPolicy, RetrievalDecision, RetrievalPolicy
from core.memory.request import CallRequest
from services.memory_tools_funcs.external_context.archival_storage import ArchivalStorage
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
    archival: ArchivalStorage | None = None,
    retrieval_policy: RetrievalPolicy | None = None,
    history_limit: int = 6,
) -> list[dict[str, Any]]:
    """Build messages[] while enforcing the per-tier memory budget."""
    c = req.constraints
    token_counter = queue.token_counter
    memory_allowed = (
        c.inject_memory_context
        and not c.json_strict
        and not c.latency_critical
    )

    base_system_msg = assemble_system_message(
        task_instruction=req.task_instruction,
        json_strict=c.json_strict,
    )
    required_tokens = token_counter.count(base_system_msg) + token_counter.count(req.user_content)
    remaining_tokens = max(0, budget.usable_prompt_tokens - required_tokens)

    working_context = ""
    if memory_allowed and not c.grounded and not working.is_empty() and remaining_tokens > 0:
        cap = min(budget.working_context_tokens, remaining_tokens)
        working_context = _trim_to_tokens(working.load(), token_counter, cap)
        remaining_tokens -= token_counter.count(working_context)

    fifo_summary = ""
    if req.use_history and memory_allowed and not c.grounded and remaining_tokens > 0:
        summary = store.load_latest_summary()
        if summary:
            cap = min(budget.fifo_tokens, remaining_tokens)
            fifo_summary = _trim_to_tokens(summary, token_counter, cap)
            remaining_tokens -= token_counter.count(fifo_summary)

    # Dedup baseline must be the FIFO rows that are ACTUALLY injected as history.
    # FIFO is only added to the prompt when use_history is True (see below), so
    # when it is False recall/archival results must not be suppressed as
    # "duplicates" of rows that never enter the prompt.
    recent_fifo_rows = (
        queue.recent_rows(limit=history_limit)
        if (req.use_history and memory_allowed)
        else []
    )
    excluded_ids, excluded_contents = _memory_row_keys(recent_fifo_rows)
    query = (req.record_content or req.user_content or "").strip()
    retrieval_decision = _decide_retrieval(
        retrieval_policy=retrieval_policy,
        query=query,
        budget=budget,
        memory_allowed=memory_allowed and not c.grounded,
    )

    recall_context = ""
    if retrieval_decision.recall_limit > 0 and remaining_tokens > 0:
        recall_rows = (
            _dedupe_memory_rows(
                queue.recall.search(query, limit=max(retrieval_decision.recall_limit * 3, retrieval_decision.recall_limit)),
                exclude_ids=excluded_ids,
                exclude_contents=excluded_contents,
                limit=retrieval_decision.recall_limit,
            )
            if query
            else []
        )
        if recall_rows:
            raw_recall = _format_memory_rows(recall_rows)
            cap = min(budget.recall_tokens, remaining_tokens)
            recall_context = _trim_to_tokens(raw_recall, token_counter, cap)
            remaining_tokens -= token_counter.count(recall_context)
            recall_ids, recall_contents = _memory_row_keys(recall_rows)
            excluded_ids.update(recall_ids)
            excluded_contents.update(recall_contents)

    archival_context = ""
    if archival is not None and retrieval_decision.archival_limit > 0 and remaining_tokens > 0:
        archival_rows = (
            _dedupe_memory_rows(
                archival.search(query, limit=max(retrieval_decision.archival_limit * 3, retrieval_decision.archival_limit)),
                exclude_ids=excluded_ids,
                exclude_contents=excluded_contents,
                limit=retrieval_decision.archival_limit,
            )
            if query
            else []
        )
        if archival_rows:
            raw_archival = _format_memory_rows(archival_rows)
            cap = min(budget.archival_tokens, remaining_tokens)
            archival_context = _trim_to_tokens(raw_archival, token_counter, cap)
            remaining_tokens -= token_counter.count(archival_context)

    warn_pressure = memory_allowed and queue.is_warning_pressure(budget)
    system_msg = assemble_system_message(
        task_instruction=req.task_instruction,
        working_context=working_context,
        fifo_summary=fifo_summary,
        recall_context=recall_context,
        archival_context=archival_context,
        memory_pressure=warn_pressure,
        json_strict=c.json_strict,
    )

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_msg}]

    if req.use_history and memory_allowed and remaining_tokens > 0:
        fifo_cap = min(budget.fifo_tokens, remaining_tokens)
        messages.extend(queue.as_chat_messages(limit=history_limit, max_tokens=fifo_cap))

    messages.append({"role": "user", "content": req.user_content})
    return messages


def _decide_retrieval(
    *,
    retrieval_policy: RetrievalPolicy | None,
    query: str,
    budget: MemoryBudget,
    memory_allowed: bool,
) -> RetrievalDecision:
    if not memory_allowed:
        return RetrievalDecision(reason="memory_disabled")
    policy = retrieval_policy or FixedKRetrievalPolicy()
    return policy.decide_retrieval(query, budget)


def _format_memory_rows(rows: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in rows:
        role = str(row.get("role") or "memory")
        content = " ".join(str(row.get("content") or "").split())
        created_at = str(row.get("created_at") or "")
        if not content:
            continue
        prefix = f"- {role}"
        if created_at:
            prefix += f" @ {created_at}"
        parts.append(f"{prefix}: {content}")
    return "\n".join(parts)


def _dedupe_memory_rows(
    rows: list[dict[str, Any]],
    *,
    exclude_ids: set[str],
    exclude_contents: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    limit = max(0, int(limit))
    if limit <= 0:
        return []
    selected: list[dict[str, Any]] = []
    seen_ids = set(exclude_ids)
    seen_contents = set(exclude_contents)
    for row in rows:
        item_id = str(row.get("id") or "").strip()
        content_key = _normalize_memory_content(row.get("content"))
        if item_id and item_id in seen_ids:
            continue
        if content_key and content_key in seen_contents:
            continue
        if item_id:
            seen_ids.add(item_id)
        if content_key:
            seen_contents.add(content_key)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _memory_row_keys(rows: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    contents: set[str] = set()
    for row in rows:
        item_id = str(row.get("id") or "").strip()
        content_key = _normalize_memory_content(row.get("content"))
        if item_id:
            ids.add(item_id)
        if content_key:
            contents.add(content_key)
    return ids, contents


def _normalize_memory_content(content: Any) -> str:
    return " ".join(str(content or "").split()).casefold()


def _trim_to_tokens(text: str, token_counter, max_tokens: int) -> str:
    text = str(text or "").strip()
    max_tokens = int(max_tokens)
    if not text or max_tokens <= 0:
        return ""
    if token_counter.count(text) <= max_tokens:
        return text

    low = 0
    high = len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].rstrip()
        if token_counter.count(candidate) <= max_tokens:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best
