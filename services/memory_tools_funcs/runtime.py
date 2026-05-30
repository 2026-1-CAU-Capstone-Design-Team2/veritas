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
from services.memory_tools_funcs.external_context.archival_storage import ArchivalStorage
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
        self.recall = RecallStorage(self.store, self.token_counter)
        self.archival = ArchivalStorage(self.store, self.token_counter)
        self.working = WorkingContextManager(self.store, self.token_counter)
        self.queue = QueueManager(self.store, self.token_counter, self.recall)
        self.summarizer = MemorySummarizer(raw_llm)
        self.policy_dispatcher = ProfilePolicyDispatcher()

        # bg flush 동시성 제어. 동시 flush 1개로 제한, 다음 prepare는 옛 summary로 진행.
        self._flush_lock = threading.Lock()
        self._flush_in_progress = False

    def configure_workspace(self, workspace_root: Path) -> None:
        """workspace 전환 시 storage 핸들을 새 디렉토리로 교체한다."""
        self.store.close()
        self.workspace_root = Path(workspace_root)
        self.store = MemoryStore(self.workspace_root, reuse_connection=True)
        self.recall = RecallStorage(self.store, self.token_counter)
        self.archival = ArchivalStorage(self.store, self.token_counter)
        self.working = WorkingContextManager(self.store, self.token_counter)
        self.queue = QueueManager(self.store, self.token_counter, self.recall)

    def close(self) -> None:
        """Release runtime-owned storage resources."""
        self.store.close()

    def update_n_ctx(self, max_context_tokens: int) -> None:
        """모델 스왑 후 n_ctx를 갱신한다."""
        self.max_context_tokens = int(max_context_tokens)

    def prepare(self, req: CallRequest) -> PreparedCall:
        """CallRequest를 받아 messages[]를 만들고 USER turn을 기록한다."""
        invocation_id = str(uuid.uuid4())
        budget = MemoryBudget(
            max_context_tokens=self.max_context_tokens,
            system_tokens=self.token_counter.count(req.task_instruction),
            current_request_tokens=self.token_counter.count(req.user_content),
        )

        if not req.constraints.no_record:
            self._log_invocation(invocation_id, req, budget)

        # flush_pressure 감지 시 즉시 flush 안 함. 이번 호출은 옛 summary로 진행하고,
        # commit() 끝난 뒤 background thread로 처리 (1세대 stale 감수).
        flush_pending = (
            self._should_record(req.constraints)
            and self.queue.is_flush_pressure(budget)
        )

        # USER turn 기록은 build_messages 뒤로 — history에 user_content가 중복 들어가는 것 방지.
        messages = build_messages(
            req=req,
            budget=budget,
            store=self.store,
            working=self.working,
            queue=self.queue,
            archival=self.archival,
            retrieval_policy=self.policy_dispatcher.retrieval_for(req.profile),
        )

        if self._should_record(req.constraints):
            record_content = req.record_content or req.user_content
            self.queue.append_event(
                role=MemoryRole.USER,
                content=record_content,
                source=req.stream_label or req.method_hint,
                metadata={"invocation_id": invocation_id},
            )
            for fact in extract_explicit_facts(record_content):
                self.working.append_fact(
                    fact,
                    source="heuristic",
                    tags=["explicit_user"],
                    max_tokens=budget.working_context_tokens,
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

    def commit(self, prepared: PreparedCall, assistant_text: str) -> None:
        """LLM 응답을 ASSISTANT turn으로 기록하고, flush가 마킹돼 있으면 bg launch."""
        if self._should_record(prepared.constraints):
            text = str(assistant_text or "").strip()
            if text:
                self.queue.append_event(
                    role=MemoryRole.ASSISTANT,
                    content=text,
                    source=prepared.stream_label or prepared.metadata.get("method_hint", "assistant"),
                    metadata={"invocation_id": prepared.invocation_id},
                )

        if prepared.metadata.get("flush_pending"):
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

    def _flush_fifo(self, *, profile: str = "chat") -> None:
        """FIFO 오래된 prefix를 evict하고 summary로 압축한다."""
        evicted_rows = self.queue.select_evicted_rows(
            budget=MemoryBudget(max_context_tokens=self.max_context_tokens),
            eviction_policy=self.policy_dispatcher.eviction_for(profile),
        )
        if not evicted_rows:
            return

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
