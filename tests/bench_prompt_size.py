"""Probe: 현재 메모리 구조 vs "그냥 history 다 넣기" — prompt 크기 비교.

가짜 대화를 N턴 적재한 다음:

  - 현재 구조 (MemoryRuntime + chat profile)가 만드는 messages[] 토큰 수
  - naive concat — 모든 turn을 chronological order로 prompt에 그대로 박았을 때 토큰 수
  - large-context 모델이라도 한 번에 처리해야 할 양

이게 진짜로 줄어드는 영역인지, 아니면 그냥 같은 데이터를 다른 그릇에 담는 것인지
실측으로 검증."""
from __future__ import annotations

import tempfile
from pathlib import Path

from core.memory.budget import MemoryBudget
from core.memory.policy import ProfilePolicyDispatcher
from core.memory.request import CallConstraints, CallRequest
from services.memory_tools_funcs.context_builder import build_messages
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.queue_manage import QueueManager
from services.memory_tools_funcs.main_context.working_context import WorkingContextManager
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.summerizer import MemorySummarizer
from core.memory.models import MemoryRole


class _CharCounter:
    """4 char ≈ 1 token. 측정만 위한 근사. 실제 토크나이저랑 다를 수 있지만
    상대 비교(현재 vs naive)에는 충분."""
    def count(self, text: str) -> int:
        return max(1, len(str(text or "")) // 4)


class _FakeLLM:
    """build_messages는 LLM을 직접 안 부름 — flush 같이 호출하는 경로에서만 필요.
    이번 bench는 build_messages만 호출하므로 stub만 있으면 됨."""
    def ask(self, *args, **kwargs) -> str:
        return ""


def _simulate_turns(n_pairs: int) -> list[tuple[MemoryRole, str]]:
    """N개의 (user, assistant) pair 생성. 내용은 다양해서 토큰 길이가 일정 분포를
    가지게 함."""
    base_questions = [
        "삼성전자 2026 목표주가 전망은 어떻게 됩니까?",
        "분기 배당 정책 변경 가능성이 있나요?",
        "반도체 업황 회복이 늦어지면 어떤 리스크가 있나요?",
        "AI 데이터센터 수요는 어떻게 전망되나요?",
        "환율 변동이 영업이익에 미치는 영향은?",
        "주주환원 정책 업데이트 일정 알려주세요.",
        "북미 hyperscaler 발주 강도는 유지될까요?",
        "메모리 가격 하락 추세는 언제 반전될까요?",
    ]
    base_answers = [
        "보수적 시나리오 80,000원, 기본 95,000원, 낙관 120,000원으로 추정됩니다.",
        "2026년 2분기 실적 발표 시점에 배당 정책 업데이트가 예정되어 있습니다.",
        "메모리 가격 하락 지속 + AI 수요 둔화가 겹치면 영업이익 -15% 시나리오가 가능합니다.",
        "북미 hyperscaler 발주가 2026년 하반기까지 강세를 유지할 전망입니다.",
        "원화 강세는 단기 부담이지만 hedging으로 영향을 분기당 200억 이하로 제한합니다.",
        "자사주 매입 확대안이 이사회 안건으로 올라와 있습니다.",
        "수요는 강세이지만 발주 시점이 분기 후반으로 밀릴 가능성이 있습니다.",
        "공급 측 감산 효과가 가시화되는 2026년 2분기 이후가 반전 시점으로 봅니다.",
    ]
    turns: list[tuple[MemoryRole, str]] = []
    for i in range(n_pairs):
        q = base_questions[i % len(base_questions)]
        a = base_answers[i % len(base_answers)]
        turns.append((MemoryRole.USER, f"[turn {i+1}] {q}"))
        turns.append((MemoryRole.ASSISTANT, f"[turn {i+1}] {a}"))
    return turns


def _naive_full_history_tokens(turns, system_prompt: str, current_user: str,
                                counter: _CharCounter) -> int:
    """모든 turn을 그대로 prompt에 박았을 때 토큰 수.
    실제 운영에서 이 방식을 쓰면 매 turn마다 prompt가 비례 증가."""
    total = counter.count(system_prompt) + counter.count(current_user)
    for _role, content in turns:
        total += counter.count(content)
    return total


def _current_structure_tokens(messages: list[dict], counter: _CharCounter) -> int:
    return sum(counter.count(m.get("content", "")) for m in messages)


def main() -> None:
    system_prompt = "You are a helpful research assistant. ..."
    current_user = "방금 그 얘기 좀 더 자세히 알려주세요."
    counter = _CharCounter()

    print(f"{'턴수':<8}{'naive concat':<18}{'현재 구조':<14}{'절감률':<10}{'msgs[] 길이':<14}")
    print("-" * 70)

    for n_pairs in (5, 10, 30, 60, 100, 200, 500):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = MemoryStore(Path(tmp), reuse_connection=True)
            recall = RecallStorage(store, counter)
            working = WorkingContextManager(store, counter)
            queue = QueueManager(store, counter, recall)

            try:
                turns = _simulate_turns(n_pairs)
                for role, content in turns:
                    queue.append_event(role=role, content=content, source="bench")

                # naive: 모든 turn을 chronological order로 prompt에 박음
                naive_tokens = _naive_full_history_tokens(
                    turns, system_prompt, current_user, counter
                )

                # 현재 구조: build_messages가 만드는 messages[]
                req = CallRequest(
                    task_instruction=system_prompt,
                    user_content=current_user,
                    record_content=current_user,
                    constraints=CallConstraints(),
                    use_history=True,
                    profile="chat",
                    method_hint="chat_final",
                )
                budget = MemoryBudget(
                    max_context_tokens=8192,
                    system_tokens=counter.count(system_prompt),
                    current_request_tokens=counter.count(current_user),
                )
                policy = ProfilePolicyDispatcher().retrieval_for("chat")
                messages = build_messages(
                    req=req, budget=budget, store=store,
                    working=working, queue=queue,
                    retrieval_policy=policy,
                )
                current_tokens = _current_structure_tokens(messages, counter)
                saved = (naive_tokens - current_tokens) / max(naive_tokens, 1) * 100
                print(
                    f"{n_pairs:<8}{naive_tokens:<18}{current_tokens:<14}"
                    f"{saved:>6.1f}%   {len(messages):<14}"
                )
            finally:
                store.close()


if __name__ == "__main__":
    main()
