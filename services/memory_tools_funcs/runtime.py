"""Memory runtime — prepare / commit orchestrator."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.memory.budget import MemoryBudget
from core.memory.models import MemoryRole
from core.memory.policy import ProfilePolicyDispatcher
from core.memory.request import CallConstraints, CallRequest
from services.memory_tools_funcs.context_builder import build_messages
from services.memory_tools_funcs.debug import mem_debug, mem_debug_enabled
from services.memory_tools_funcs.external_context.embedding_recall_store import (
    EmbeddingRecallStore,
)
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.heuristic_memory import extract_explicit_facts
from services.memory_tools_funcs.main_context.queue_manage import QueueManager, utc_now_iso
from services.memory_tools_funcs.main_context.working_context import WorkingContextManager
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.summerizer import MemorySummarizer
from services.memory_tools_funcs.token_counter import TokenCounter


@dataclass
class PreparedCall:
    """prepare() → wrapper 운반 객체."""

    invocation_id: str
    messages: list[dict[str, Any]]
    constraints: CallConstraints
    user_content: str
    stream_label: str = ""
    sampling_params: dict[str, Any] | None = None
    extra_sampling_params: dict[str, Any] | None = None
    timeout_sec: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryRuntime:
    """prepare / commit 라이프사이클 owner."""

    def __init__(
        self,
        *,
        raw_llm,
        workspace_root: Path,
        max_context_tokens: int,
    ) -> None:
        self.raw_llm = raw_llm
        self.workspace_root = Path(workspace_root)
        self.max_context_tokens = int(max_context_tokens)

        self.token_counter = TokenCounter(raw_llm)
        self.store = MemoryStore(self.workspace_root, reuse_connection=True)
        self.embedding_recall = EmbeddingRecallStore(self.workspace_root, raw_llm)
        self.recall = RecallStorage(
            self.store, self.token_counter, embedding_store=self.embedding_recall
        )
        self.working = WorkingContextManager(self.store, self.token_counter)
        self.queue = QueueManager(self.store, self.token_counter, self.recall)
        self.summarizer = MemorySummarizer(raw_llm)
        self.policy_dispatcher = ProfilePolicyDispatcher()

        # bg flush 동시성 제어. 동시 flush 1개로 제한, 다음 prepare는 옛 summary로 진행.
        self._flush_lock = threading.Lock()
        self._flush_in_progress = False

        self._launch_embedding_backfill()

    def configure_workspace(self, workspace_root: Path) -> None:
        """workspace 전환 시 storage 핸들을 새 디렉토리로 교체한다."""
        self.store.close()
        if getattr(self, "embedding_recall", None) is not None:
            self.embedding_recall.close()
        self.workspace_root = Path(workspace_root)
        self.store = MemoryStore(self.workspace_root, reuse_connection=True)
        self.embedding_recall = EmbeddingRecallStore(self.workspace_root, self.raw_llm)
        self.recall = RecallStorage(
            self.store, self.token_counter, embedding_store=self.embedding_recall
        )
        self.working = WorkingContextManager(self.store, self.token_counter)
        self.queue = QueueManager(self.store, self.token_counter, self.recall)
        self._launch_embedding_backfill()

    def close(self) -> None:
        """Release runtime-owned storage resources."""
        self.store.close()
        if getattr(self, "embedding_recall", None) is not None:
            self.embedding_recall.close()

    # Upper bound on rows pulled into a one-shot dense backfill for a
    # workspace whose recall predates the embedding index. Recall keeps every
    # turn, so this caps embed cost to the most recent slice.
    EMBED_BACKFILL_LIMIT = 2000

    def _launch_embedding_backfill(self) -> None:
        """Background one-shot: index pre-existing recall turns into the dense
        store so a workspace opened before embedding recall existed still gets
        semantic search, without blocking the first chat turn. Captures the
        current recall/embedding handles so a workspace swap mid-backfill does
        not retarget the worker."""
        recall = self.recall
        embedding_recall = self.embedding_recall
        t = threading.Thread(
            target=self._embedding_backfill_worker,
            args=(recall, embedding_recall),
            name="memory-embed-backfill",
            daemon=True,
        )
        t.start()

    def _embedding_backfill_worker(self, recall, embedding_recall) -> None:
        try:
            if embedding_recall.count() > 0:
                return
            rows = recall.tail(limit=self.EMBED_BACKFILL_LIMIT)
            if not rows:
                return
            indexed = embedding_recall.backfill(rows)
            if indexed:
                mem_debug("embed", f"backfilled {indexed} recall turns into dense index")
        except Exception as e:
            print(f"[memory][embed_backfill][warn] {type(e).__name__}: {e}")

    def update_n_ctx(self, max_context_tokens: int) -> None:
        """모델 스왑 후 n_ctx를 갱신한다."""
        self.max_context_tokens = int(max_context_tokens)

    # Cap on the read-side projection used to render workspace chat history
    # in the UI. The workspace memory.sqlite3 recall tier preserves every
    # turn (FIFO eviction does not touch recall), so this only bounds the
    # number of rows pulled into one fetch — far above any realistic
    # conversation length.
    CHAT_HISTORY_PROJECTION_LIMIT = 1_000

    def history_as_chat_items(self, *, limit: int | None = None) -> list[dict[str, str]]:
        """Project the recall tier into the UI-shaped chat-history list.

        Yields ``{"role": "user"|"assistant", "text": "..."}`` items in
        chronological order, filtering out non-conversational rows (screen
        interventions are not recorded; tool-call markers, system entries,
        and empty content are skipped). This is the read-side path used by
        ``draft_chat_service`` to render the chat panel, replacing the
        legacy ``chat_history.json`` per-workspace JSON file.
        """
        cap = int(limit if limit is not None else self.CHAT_HISTORY_PROJECTION_LIMIT)
        try:
            rows = self.recall.tail(limit=cap)
        except Exception:
            return []
        items: list[dict[str, str]] = []
        for row in rows:
            role = str(row.get("role") or "").lower()
            if role not in {"user", "assistant"}:
                continue
            text = str(row.get("content") or "").strip()
            if not text:
                continue
            items.append({"role": role, "text": text})
        return items

    def import_legacy_chat_items(self, items: list[dict[str, Any]]) -> int:
        """One-shot import of a legacy ``chat_history.json`` payload.

        Used during workspace bootstrap to lift the pre-memory JSON log into
        the workspace memory.sqlite3 (FIFO + recall) once, so the new
        read-side projection (``history_as_chat_items``) surfaces the user's
        prior conversations after the storage cut-over. Idempotency is the
        caller's responsibility — :class:`AgentRuntime` runs this only when
        the recall tier is empty and then renames the JSON file out of the
        way so the import never repeats.

        Returns the number of valid ``{role, text}`` rows imported.
        """
        imported = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            role_raw = str(item.get("role") or "").lower()
            if role_raw == "user":
                role = MemoryRole.USER
            elif role_raw == "assistant":
                role = MemoryRole.ASSISTANT
            else:
                continue
            text = str(item.get("text") or item.get("content") or "").strip()
            if not text:
                continue
            self.queue.append_event(
                role=role,
                content=text,
                source=str(item.get("source") or "legacy_chat_history"),
                metadata={"imported_from": "chat_history.json"},
            )
            imported += 1
        return imported

    NO_HISTORY_TEXT = "(No previous conversation)"

    def recent_history_text(self, *, turns: int) -> str:
        """Flat "role: content" block of the FIFO tail, for prompts that do
        NOT go through prepare/commit and therefore receive no chat-message
        injection from the runtime — currently the tool-routing call (raw
        passthrough on MemoryAwareLLMClient) and the RAG query rewrite.

        ``turns`` is the number of (user, assistant) pairs requested; the
        underlying FIFO row count is ``turns * 2``. Returns
        ``NO_HISTORY_TEXT`` when nothing is recorded or the FIFO read fails.

        Centralizes what used to be duplicated between ChatAgent and
        RAGService so both surfaces share one definition of "recent
        history".
        """
        try:
            rows = self.queue.recent_rows(limit=max(0, int(turns)) * 2)
        except Exception:
            return self.NO_HISTORY_TEXT
        if not rows:
            return self.NO_HISTORY_TEXT
        parts: list[str] = []
        for row in rows:
            role = str(row.get("role") or "user")
            content = " ".join(str(row.get("content") or "").split())
            if content:
                parts.append(f"{role}: {content}")
        return "\n".join(parts) if parts else self.NO_HISTORY_TEXT

    def prepare(self, req: CallRequest) -> PreparedCall:
        """CallRequest를 받아 messages[]를 만들고 USER turn을 기록한다."""
        invocation_id = str(uuid.uuid4())
        
        """
        MemoryBudget으로 만들어지면, 속성 결정됨
            - usable_prompt_tokens : 응답 reserve를 뺀 입력 가능 token 수
            - warning_tokens : memory pressure 경고 임계값.
            - flush_tokens : FIFO evict + summarize 임계값.
        """
        budget = MemoryBudget(
            max_context_tokens=self.max_context_tokens,
            system_tokens=self.token_counter.count(req.task_instruction),
            current_request_tokens=self.token_counter.count(req.user_content),
        )

        # debug용 로그
        if mem_debug_enabled():
            c = req.constraints
            mem_debug(
                "prepare",
                f"id={invocation_id[:8]} profile={req.profile} method={req.method_hint} "
                f"use_history={req.use_history} "
                f"constraints[grounded={c.grounded},json_strict={c.json_strict},"
                f"latency_critical={c.latency_critical},no_record={c.no_record},"
                f"inject_memory_context={c.inject_memory_context}] "
                f"budget[usable={budget.usable_prompt_tokens},system={budget.system_tokens},"
                f"request={budget.current_request_tokens}] "
                f"fifo_tokens={self.queue.total_fifo_tokens()} "
                f"pressure[warn={self.queue.is_warning_pressure(budget)},"
                f"flush={self.queue.is_flush_pressure(budget)}]",
            )

        # no_record 제약이 없으면 invocations.jsonl 한 줄 기록 
        # (메모리 디버깅과 호출 패턴 분석용, 실제 LLM 프롬프트에는 영향 없음).
        if not req.constraints.no_record:
            self._log_invocation(invocation_id, req, budget)

        # 플래그만 마킹하고, flush_pressure 감지 시 즉시 flush 안 함. 
        # 이번 호출은 옛 summary로 진행하고,
        # commit() 끝난 뒤 background thread로 처리 (1세대 stale 감수).
        flush_pending = (
            self._should_record(req.constraints)
            and self.queue.is_flush_pressure(budget)
        )
        """
        =================================중요=================================
         - build_messages()는 MemoryBudget을 받아서, 시스템 프롬프트 + FIFO
            prompts 생성
            # USER turn 기록은 build_messages 뒤로 
            # — history에 user_content가 중복 들어가는 것 방지.
        """
        messages = build_messages(
            req=req,
            budget=budget,
            store=self.store,
            working=self.working,
            queue=self.queue,
            retrieval_policy=self.policy_dispatcher.retrieval_for(req.profile),
        )

        if mem_debug_enabled():
            system_len = len(messages[0]["content"]) if messages else 0
            history_msgs = max(0, len(messages) - 2)  # minus system + current user
            mem_debug(
                "context",
                f"id={invocation_id[:8]} messages={len(messages)} "
                f"system_chars={system_len} history_msgs={history_msgs} "
                f"system_tokens={self.token_counter.count(messages[0]['content']) if messages else 0}",
            )


        # should append -> append_event 수행, 
        #   FIFO와 Recall에 USER turn 기록. 
        #   heuristic fact 추출해서 working에도 추가.
        if self._should_record(req.constraints):
            record_content = req.record_content or req.user_content
            self.queue.append_event(
                role=MemoryRole.USER,
                content=record_content,
                source=req.stream_label or req.method_hint,
                metadata={"invocation_id": invocation_id},
            )
            mem_debug(
                "prepare",
                f"id={invocation_id[:8]} recorded USER turn "
                f"({self.token_counter.count(record_content)} tokens)",
            )
          
        #  Heuristic explicit fact 추출해서 working context에 추가 (옵션이 켜져 있고, 기록 제약이 없을 때).
        #  append_fact() -> working 테이블에 저장. 
            for fact in extract_explicit_facts(record_content):
                self.working.append_fact(
                    fact,
                    source="heuristic",
                    tags=["explicit_user"],
                    max_tokens=budget.working_context_tokens,
                )
                mem_debug("working", f"id={invocation_id[:8]} heuristic fact appended: {fact!r}")
        elif mem_debug_enabled():
            mem_debug(
                "prepare",
                f"id={invocation_id[:8]} turn NOT recorded "
                f"(no_record/json_strict/latency_critical)",
            )

        return PreparedCall(
            invocation_id=invocation_id,
            messages=messages,
            constraints=req.constraints,
            user_content=req.user_content,
            stream_label=req.stream_label,
            sampling_params=req.sampling_params,
            extra_sampling_params=req.extra_sampling_params,
            timeout_sec=req.timeout_sec,
            metadata={
                "method_hint": req.method_hint,
                "profile": req.profile,
                "flush_pending": flush_pending,
            },
        )



#########################################################################################
#########################################################################################
    """
        ASSISTANT 저장 + flush 트리거 
        append_event() 가 USER turn 저장과 똑같은 경로로 FIFO + Recall 에 동시 INSERT
    """
    def commit(self, prepared: PreparedCall, assistant_text: str) -> None:
        """LLM 응답을 ASSISTANT turn으로 기록하고, flush가 마킹돼 있으면 bg launch."""
        inv = prepared.invocation_id[:8]
        if self._should_record(prepared.constraints):
            text = str(assistant_text or "").strip()
            if text:
                self.queue.append_event(
                    role=MemoryRole.ASSISTANT,
                    content=text,
                    source=prepared.stream_label or prepared.metadata.get("method_hint", "assistant"),
                    metadata={"invocation_id": prepared.invocation_id},
                )
                mem_debug(
                    "commit",
                    f"id={inv} recorded ASSISTANT turn "
                    f"({self.token_counter.count(text)} tokens)",
                )

        if prepared.metadata.get("flush_pending"):
            mem_debug("commit", f"id={inv} flush_pending=True -> launching background flush")
            self._maybe_launch_bg_flush(str(prepared.metadata.get("profile") or "chat"))

    @staticmethod
    def _should_record(constraints: CallConstraints) -> bool:
        """turn을 FIFO+Recall에 기록할지."""
        if constraints.no_record:
            return False
        if constraints.latency_critical:
            return False
        if constraints.json_strict:
            return False
        return True

    def _log_invocation(
        self,
        invocation_id: str,
        req: CallRequest,
        budget: MemoryBudget,
    ) -> None:
        """invocations.jsonl 한 줄 기록."""
        self.store.append_jsonl(
            self.store.invocations_path,
            {
                "invocation_id": invocation_id,
                "method": req.method_hint,
                "stream_label": req.stream_label,
                "constraints": {
                    "grounded": req.constraints.grounded,
                    "json_strict": req.constraints.json_strict,
                    "latency_critical": req.constraints.latency_critical,
                    "no_record": req.constraints.no_record,
                    "inject_memory_context": req.constraints.inject_memory_context,
                },
                "profile": req.profile,
                "use_history": req.use_history,
                "system_tokens": budget.system_tokens,
                "request_tokens": budget.current_request_tokens,
                "created_at": utc_now_iso(),
            },
        )

    def _maybe_launch_bg_flush(self, profile: str = "chat") -> None:
        """이미 flush 진행 중이면 skip. 아니면 daemon thread로 fire-and-forget 시작."""
        with self._flush_lock:
            if self._flush_in_progress:
                return
            self._flush_in_progress = True
        t = threading.Thread(
            target=self._bg_flush_worker,
            args=(profile,),
            name="memory-bg-flush",
            daemon=True,
        )
        t.start()

    def _bg_flush_worker(self, profile: str = "chat") -> None:
        """bg thread 본체. _flush_fifo를 실행하고 lock 풀어준다."""
        try:
            self._flush_fifo(profile=profile)
        except Exception as e:
            print(f"[memory][bg_flush][warn] {type(e).__name__}: {e}")
        finally:
            with self._flush_lock:
                self._flush_in_progress = False

############################################################################################
############################################################################################

    def _flush_fifo(self, *, profile: str = "chat") -> None:
        """FIFO 오래된 prefix를 evict하고 summary로 압축한다."""
        fifo_before = self.queue.fifo.count() if mem_debug_enabled() else 0
        evicted_rows = self.queue.select_evicted_rows(
            budget=MemoryBudget(max_context_tokens=self.max_context_tokens),
            eviction_policy=self.policy_dispatcher.eviction_for(profile),
        )
        if not evicted_rows:
            mem_debug("flush", f"profile={profile} no rows to evict (skip)")
            return

        mem_debug(
            "flush",
            f"profile={profile} evicting {len(evicted_rows)} rows "
            f"(FIFO {fifo_before} rows before compaction)",
        )
        previous_summary = self.store.load_latest_summary()
        evicted_text = "\n".join(
            f"{row.get('role')}: {row.get('content')}" for row in evicted_rows
        )
        summary = self.summarizer.summarize_evicted(
            previous_summary=previous_summary,
            evicted_messages=evicted_text,
        )
        if summary:
            self.queue.reset_fifo_with_summary(summary, evicted_rows=evicted_rows)
            mem_debug(
                "flush",
                f"profile={profile} summary written ({self.token_counter.count(summary)} tokens), "
                f"FIFO now {self.queue.fifo.count() if mem_debug_enabled() else 0} rows",
            )
        else:
            mem_debug("flush", f"profile={profile} summarizer returned empty -> FIFO unchanged")
