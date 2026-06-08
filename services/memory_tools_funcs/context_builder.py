"""Convert a CallRequest and memory state into OpenAI chat messages."""

from __future__ import annotations

from typing import Any

from core.memory.budget import MemoryBudget
from core.memory.policy import FixedKRetrievalPolicy, RetrievalDecision, RetrievalPolicy
from core.memory.request import CallRequest
from services.memory_tools_funcs.debug import mem_debug
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
    retrieval_policy: RetrievalPolicy | None = None,
    history_limit: int = 6,
) -> list[dict[str, Any]]:
    """
        Build messages[] while enforcing the per-tier memory budget.
    """
    """
        system prompt + working context + FIFO summary + retrieved memory rows 순으로 프롬프트에 넣으면서, 
        MemoryBudget의 토큰 한도 내에서 최대한 많은 메모리를 활용하는 방식.

        - base_system_msg: 메모리 블록이 없는 기본 시스템 프롬프트 (COMMON_GATEWAY_INSTRUCTIONS + task_instruction)
        - required_tokens: 이번 호출에서 반드시 필요한 토큰의 하한 값(최소 토큰 개수)
        - remaining_tokens: 메모리 블록에 채워질 수 있는 토큰 예산 (budget.usable_prompt_tokens - required_tokens)
    """
    
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

    # 최근 대화(FIFO history)용 최소 예산을 미리 떼어둔다. 문서가 큰 RAG
    # 프롬프트가 아래 working/summary/recall 단계에서 예산을 다 먹어 직전 대화가
    # 0 토큰으로 밀리는 것을 막는다. 여기서는 그 단계들에 floor를 숨기고 FIFO
    # 주입 시 다시 더해, FIFO는 최소 floor를 보장받되 위 단계가 남긴 만큼 더
    # 키울 수 있다. grounded(working/summary/recall 미주입)에서는 FIFO가 유일한
    # 메모리 블록이라 경쟁이 없으므로 floor를 잡지 않는다.
    fifo_floor = 0
    if req.use_history and memory_allowed and not c.grounded:
        fifo_floor = min(budget.fifo_tokens, remaining_tokens // 3)
        remaining_tokens -= fifo_floor

###########################################################################################################
###########################################################################################################  
    """
        working_context 우선 주입. @ working.load()
            - working_context는 메모리에서 가장 최근에 만들어진 컨텍스트
            - FIFO보다 최신 정보 담고 있을 가능성 높음.
        내부적으로, WorkingContextManager.load() 호출

    """
    working_context = ""
    if memory_allowed and not c.grounded and not working.is_empty() and remaining_tokens > 0:
        cap = min(budget.working_context_tokens, remaining_tokens)
        # working.load() -> workingContextManger.load() 
        #                -> MemoryStore.format_working_records()
        # 만들어진 텍스트는, _trim_to_tokens로 cap 에 맞춰 잘린다.
        working_context = _trim_to_tokens(working.load(), token_counter, cap)
        remaining_tokens -= token_counter.count(working_context)
        if working_context:
            mem_debug(
                "context",
                f"working injected ({token_counter.count(working_context)} tokens, "
                f"cap={cap}, remaining={remaining_tokens})",
            )

###########################################################################################################
###########################################################################################################
    """  Latest Summary 주입 """
    """
        load_latest_summary()로 FIFO의 최신 summary를 가져와서, 남은 토큰 예산 내에서 최대한 주입.
            - summary는 FIFO의 가장 최근 상태를 압축해서 담고 있을 가능성 높음.
            - summary 토큰 수가 cap보다 많으면, _trim_to_tokens로 자름
    """
    fifo_summary = ""
    if req.use_history and memory_allowed and not c.grounded and remaining_tokens > 0:
        summary = store.load_latest_summary()
        if summary:
            cap = min(budget.fifo_tokens, remaining_tokens)
            fifo_summary = _trim_to_tokens(summary, token_counter, cap)
            remaining_tokens -= token_counter.count(fifo_summary)
            if fifo_summary:
                mem_debug(
                    "context",
                    f"summary injected ({token_counter.count(fifo_summary)} tokens, "
                    f"cap={cap}, remaining={remaining_tokens})",
                )

    # Dedup baseline must be the FIFO rows that are ACTUALLY injected as history.
    # FIFO is only added to the prompt when use_history is True (see below), so
    # when it is False recall results must not be suppressed as "duplicates" of
    # rows that never enter the prompt.
###########################################################################################################
###########################################################################################################
    """
        FIFO 최근 행을 dedupe baseline 으로 설정.
        -> QueueManager.recent_rows() -> FifoStorage.tail()
        -> 6개의 최근 FIFO 행을 가져와서, 이 행들의 ID와 content를 중복 제외 기준으로 삼는다.
    """
    recent_fifo_rows = (
        queue.recent_rows(limit=history_limit)
        if (req.use_history and memory_allowed)
        else []
    )
    excluded_ids, excluded_contents = _memory_row_keys(recent_fifo_rows)
    query = (req.record_content or req.user_content or "").strip()
    
###########################################################################################################
###########################################################################################################
    # RetrievalPolicy에 따라 recall을 수행할지 여부와 recall 시 최대 몇 개의 행을 가져올지 결정한다.
    """
        _decide_retrieval -> chat profile 이면 recall_limit=3 이 반환
    """
    retrieval_decision = _decide_retrieval(
        retrieval_policy=retrieval_policy,
        query=query,
        budget=budget,
        memory_allowed=memory_allowed and not c.grounded,
    )

###########################################################################################################
###########################################################################################################
    """ 
    Recall 검색 
    search -> _fts_query() -> _strip_particle() -> _search_sqlite , 
    못잡으면 fallback(_like_fallback) -> _rerank()

    "내 이름은 박서원이고, 삼성 주가가 어떻게 됐는지 알려줘" 가 query 로 오면:
        regex 토큰: [내, 이름은, 박서원이고, 삼성, 주가가, 어떻게, 됐는지, 알려줘]
        _strip_particle() 통과 후: [내, 이름, 박서원, 삼성, 주가, 어떻게, 됐는지, 알려줘]
        길이 < 3 제거: [이름, 박서원, 삼성, 주가, 어떻게, 됐는지, 알려줘] (내, 길이 < 3 인 토큰 drop)
        stopword 제거: 어떻게, 알려줘 제거 → [이름, 박서원, 삼성, 주가, 됐는지]
        길이 desc 정렬: [박서원, 됐는지, 이름, 삼성, 주가]
        상위 6개 (5개 그대로): "박서원" OR "됐는지" OR "이름" OR "삼성" OR "주가"
    
        _strip_particle() 
    """
    recall_context = ""
    if retrieval_decision.recall_limit > 0 and remaining_tokens > 0:
        # 
        recall_raw_hits = (
            queue.recall.search(query, limit=max(retrieval_decision.recall_limit * 3, retrieval_decision.recall_limit))
            if query
            else []
        )
        recall_rows = _dedupe_memory_rows(
            recall_raw_hits,
            exclude_ids=excluded_ids,
            exclude_contents=excluded_contents,
            limit=retrieval_decision.recall_limit,
        )
        mem_debug(
            "retrieval",
            f"recall query={query!r} limit={retrieval_decision.recall_limit} "
            f"raw_hits={len(recall_raw_hits)} after_dedup={len(recall_rows)}",
        )
        if recall_rows:
            raw_recall = _format_memory_rows(recall_rows)
            cap = min(budget.recall_tokens, remaining_tokens)
            recall_context = _trim_to_tokens(raw_recall, token_counter, cap)
            remaining_tokens -= token_counter.count(recall_context)
            # _memory_rows_keys()로 앞에서 가져온 키와 일치하는 건 제외한다.
            recall_ids, recall_contents = _memory_row_keys(recall_rows)
            excluded_ids.update(recall_ids)
            excluded_contents.update(recall_contents)
            mem_debug(
                "retrieval",
                f"recall injected {len(recall_rows)} rows "
                f"({token_counter.count(recall_context)} tokens, cap={cap}, remaining={remaining_tokens})",
            )

    warn_pressure = memory_allowed and queue.is_warning_pressure(budget)
    
    # 시스템 메시지 조립. system_msg는 시스템 메시지 + working context + summary + recall context + 메모리 압박 경고 등을 담는다.
    system_msg = assemble_system_message(
        task_instruction=req.task_instruction,
        working_context=working_context,
        fifo_summary=fifo_summary,
        recall_context=recall_context,
        memory_pressure=warn_pressure,
        json_strict=c.json_strict,
    )

    # FIFO history 를 chat messages 로 변환
    # as_chat_messages -
    #  -> tail 부터 거꾸로 채우는 방식
    #  ->가장 최근 turn 부터 토큰 cap 까지 채우다가 한 turn 이 cap 을 넘기면 멈춤
    """ message 결과물
                [
            {
                "role": "system",
                "content": (
                    "COMMON_GATEWAY_INSTRUCTIONS...\n\n"
                    "(task_instruction)\n\n"
                    "## Working Context\n- 사용자 이름: 박서원\n- 프로젝트: veritas\n\n"
                    "## Recent Conversation Summary\n사용자는 ...\n\n"
                    "## Retrieved Recall Context\n- user @ 2026-06-01: 삼성전자 시가총액...\n"
                ),
            },
            {"role": "user", "content": "(직전 user turn)"},
            {"role": "assistant", "content": "(직전 assistant turn)"},
            ... (FIFO 최근 6 turn)
            {"role": "user", "content": "내 이름은 박서원이고, 삼성 주가가 어떻게 됐는지 알려줘"},
        ]

    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_msg}]

    if req.use_history and memory_allowed and (remaining_tokens + fifo_floor) > 0:
        fifo_cap = min(budget.fifo_tokens, remaining_tokens + fifo_floor)
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
