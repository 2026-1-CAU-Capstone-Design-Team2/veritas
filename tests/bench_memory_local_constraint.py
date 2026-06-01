"""Quantitative benchmark: does the memGPT-style memory improve a *local* (small
context window) agent over a naive full-history baseline?

Local 4B~9B models run with a small KV cache, so the usable context window is
tiny (4K~16K) compared to cloud models (128K+). The survival problem for a local
chat agent is: **how do you fit a long conversation into a small window without
losing old facts?**

This bench compares, over a synthetic N-turn conversation, two prompt-assembly
strategies under the SAME small ``n_ctx``:

  * NAIVE   — accumulate every past turn verbatim into the prompt (no compaction,
              no retrieval). This is the pre-memGPT behaviour and the fairest
              baseline.
  * MEMGPT  — the real MemoryRuntime path: FIFO + background summary compaction +
              recall (FTS) retrieval, capped by the per-tier token budget.

Metrics (all deterministic — NO LLM inference, so the numbers isolate the memory
structure from model quality):

  1. context_overflow_rate — fraction of turns whose assembled prompt exceeds
     usable_prompt_tokens (i.e. would be truncated / OOM on a local model).
  2. avg_prompt_tokens     — mean prompt tokens per turn (KV-cache / latency proxy).
  3. recall_hit_rate       — for planted "remember X" facts asked again many turns
     later, did the fact survive into the prompt? (NAIVE loses it once truncated;
     MEMGPT can pull it back from recall.)

Run:
    conda run -n agent python tests/bench_memory_local_constraint.py
    conda run -n agent python tests/bench_memory_local_constraint.py --n-ctx 4096 --turns 80 --csv out.csv
"""
from __future__ import annotations

import argparse
import csv
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from core.memory.budget import MemoryBudget
from core.memory.models import MemoryRole
from core.memory.request import CallRequest
from services.memory_tools_funcs.context_builder import build_messages
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.queue_manage import QueueManager
from services.memory_tools_funcs.main_context.working_context import WorkingContextManager
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.summerizer import MemorySummarizer
from services.memory_tools_funcs.token_counter import TokenCounter


# A fake summarizer LLM: compacts evicted text into a short deterministic line so
# the bench needs no live model. Length is intentionally small to mimic a real
# recursive summary (the whole point of compaction).
class _FakeSummaryLLM:
    def ask(self, _system, prompt, **_kw) -> str:
        # crude but deterministic: keep the first ~30 words of the evicted block
        words = str(prompt or "").split()
        head = " ".join(words[:30])
        return f"[summary] {head}"


@dataclass
class TurnRecord:
    strategy: str
    turn: int
    prompt_tokens: int
    overflow: bool
    is_probe: bool = False
    probe_hit: bool | None = None


@dataclass
class Result:
    records: list[TurnRecord] = field(default_factory=list)

    def overflow_rate(self, strategy: str) -> float:
        rows = [r for r in self.records if r.strategy == strategy]
        if not rows:
            return 0.0
        return sum(1 for r in rows if r.overflow) / len(rows)

    def avg_prompt_tokens(self, strategy: str) -> float:
        rows = [r for r in self.records if r.strategy == strategy]
        if not rows:
            return 0.0
        return sum(r.prompt_tokens for r in rows) / len(rows)

    def recall_hit_rate(self, strategy: str) -> float:
        probes = [r for r in self.records if r.strategy == strategy and r.is_probe]
        if not probes:
            return 0.0
        return sum(1 for r in probes if r.probe_hit) / len(probes)


def _make_conversation(turns: int, fact_every: int) -> list[dict]:
    """Build a synthetic conversation.

    Every ``fact_every`` turns we plant a unique fact ("기억해: <KEY> is <VALUE>"),
    then probe it again many turns later. Other turns are filler chatter sized to
    push the window. Each item: {role, content, probe_key?}.
    """
    convo: list[dict] = []
    planted: list[tuple[int, str, str]] = []  # (turn_index, key, value)
    filler = (
        "이건 평범한 잡담 turn 입니다. 로컬 모델의 컨텍스트를 채우기 위한 "
        "충분히 긴 문장으로, 실제 대화에서 흔한 분량을 모사합니다. "
        "alpha beta gamma delta epsilon zeta eta theta. "
    )
    for t in range(turns):
        if t % fact_every == 0:
            key = f"FACT{t}"
            value = f"value_{t}"
            planted.append((t, key, value))
            convo.append(
                {"role": "user", "content": f"기억해줘: {key} 는 {value} 야. {filler}", "plant_key": key, "plant_value": value}
            )
        elif planted and t % fact_every == fact_every // 2:
            # probe the OLDEST not-yet-probed fact
            ptn, key, value = planted[0]
            convo.append(
                {"role": "user", "content": f"{key} 가 뭐였지?", "probe_key": key, "probe_value": value}
            )
        else:
            convo.append({"role": "user", "content": f"{t}번째 질문. {filler}"})
        convo.append({"role": "assistant", "content": f"답변 {t}. 알겠습니다."})
    return convo


def _naive_prompt_tokens(history: list[dict], system: str, user: str, counter: TokenCounter) -> int:
    """NAIVE: system + every past turn verbatim + current user."""
    parts = [system]
    for h in history:
        parts.append(f"{h['role']}: {h['content']}")
    parts.append(f"user: {user}")
    return counter.count("\n".join(parts))


def run_bench(*, n_ctx: int, turns: int, fact_every: int = 10) -> Result:
    result = Result()
    counter = TokenCounter()  # no raw_llm -> deterministic utf-8 bytes//4 fallback
    budget = MemoryBudget(max_context_tokens=n_ctx, reserve_output_tokens=512)
    usable = budget.usable_prompt_tokens
    system = "You are a helpful local assistant."
    convo = _make_conversation(turns, fact_every)

    # ---- NAIVE baseline: accumulate all turns, no compaction/retrieval ----
    naive_history: list[dict] = []
    turn_no = 0
    for item in convo:
        if item["role"] != "user":
            naive_history.append(item)
            continue
        turn_no += 1
        ptoks = _naive_prompt_tokens(naive_history, system, item["content"], counter)
        overflow = ptoks > usable
        is_probe = "probe_key" in item
        probe_hit = None
        if is_probe:
            # NAIVE hit only if the planted value still fits within the usable window.
            # Simulate truncation: keep only the newest turns that fit in `usable`.
            kept, acc = [], counter.count(system) + counter.count(item["content"])
            for h in reversed(naive_history):
                c = counter.count(f"{h['role']}: {h['content']}")
                if acc + c > usable:
                    break
                acc += c
                kept.append(h)
            kept_text = " ".join(h["content"] for h in kept)
            probe_hit = item["probe_value"] in kept_text
        result.records.append(TurnRecord("naive", turn_no, ptoks, overflow, is_probe, probe_hit))
        naive_history.append(item)

    # ---- MEMGPT: real MemoryRuntime path with compaction + recall ----
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp))
        try:
            recall = RecallStorage(store, counter)
            queue = QueueManager(store, counter, recall)
            working = WorkingContextManager(store, counter)
            summarizer = MemorySummarizer(_FakeSummaryLLM())

            turn_no = 0
            pending_assistant = None
            for item in convo:
                if item["role"] != "user":
                    pending_assistant = item
                    continue
                turn_no += 1
                req = CallRequest(
                    task_instruction=system,
                    user_content=item["content"],
                    record_content=item["content"],
                    use_history=True,
                )
                messages = build_messages(
                    req=req, budget=budget, store=store, working=working, queue=queue
                )
                ptoks = counter.count("\n".join(str(m["content"]) for m in messages))
                overflow = ptoks > usable

                is_probe = "probe_key" in item
                probe_hit = None
                if is_probe:
                    full = "\n".join(str(m["content"]) for m in messages)
                    probe_hit = item["probe_value"] in full

                result.records.append(
                    TurnRecord("memgpt", turn_no, ptoks, overflow, is_probe, probe_hit)
                )

                # record the turn (USER then ASSISTANT) into memory
                queue.append_event(role=MemoryRole.USER, content=item["content"], source="bench")
                if pending_assistant:
                    queue.append_event(
                        role=MemoryRole.ASSISTANT, content=pending_assistant["content"], source="bench"
                    )
                    pending_assistant = None

                # compaction: when over flush pressure, evict + summarize (real path)
                if queue.is_flush_pressure(budget):
                    evicted = queue.select_evicted_rows(keep_tail=20)
                    if evicted:
                        prev = store.load_latest_summary()
                        text = "\n".join(f"{r.get('role')}: {r.get('content')}" for r in evicted)
                        summary = summarizer.summarize_evicted(previous_summary=prev, evicted_messages=text)
                        if summary:
                            queue.reset_fifo_with_summary(summary, evicted_rows=evicted)
        finally:
            store.close()

    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-ctx", type=int, default=4096, help="local model usable context window")
    ap.add_argument("--turns", type=int, default=80)
    ap.add_argument("--fact-every", type=int, default=10)
    ap.add_argument("--csv", type=str, default="")
    args = ap.parse_args()

    res = run_bench(n_ctx=args.n_ctx, turns=args.turns, fact_every=args.fact_every)

    print(f"\n=== Local-constraint memory benchmark (n_ctx={args.n_ctx}, turns={args.turns}) ===")
    print(f"{'metric':<28}{'NAIVE':>12}{'MEMGPT':>12}")
    print("-" * 52)
    print(f"{'context_overflow_rate':<28}{res.overflow_rate('naive'):>11.1%}{res.overflow_rate('memgpt'):>12.1%}")
    print(f"{'avg_prompt_tokens':<28}{res.avg_prompt_tokens('naive'):>12.0f}{res.avg_prompt_tokens('memgpt'):>12.0f}")
    print(f"{'recall_hit_rate':<28}{res.recall_hit_rate('naive'):>11.1%}{res.recall_hit_rate('memgpt'):>12.1%}")
    print()

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["strategy", "turn", "prompt_tokens", "overflow", "is_probe", "probe_hit"])
            for r in res.records:
                w.writerow([r.strategy, r.turn, r.prompt_tokens, int(r.overflow),
                            int(r.is_probe), "" if r.probe_hit is None else int(r.probe_hit)])
        print(f"per-turn CSV written: {args.csv}")


if __name__ == "__main__":
    main()
