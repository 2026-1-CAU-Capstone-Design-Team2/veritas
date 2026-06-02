"""Probe: 옛 turn을 사용자가 키워드로 떠올리면 회수되는가?

100턴을 적재한 다음, 매 옛 user turn마다 그 turn의 핵심 키워드 1-2개로
query를 만들어 ``recall.search()``로 자기 자신을 회수할 수 있는지 측정.

이게 "100%면 묻기만 하면 다 기억", 낮으면 "context-window랑 별 차이 없음
(저장만 됐을 뿐, 사용자 입장에선 잃은 것)".

세 가지 query 모드를 동시에 측정:

  (a) full-content    : turn 본문 그대로 query — trivial upper bound
  (b) two-noun        : 본문에서 명사 2개 추출해서 query — 사용자가 정확히
                        떠올리는 키워드를 던지는 케이스
  (c) one-noun        : 명사 1개만 — 사용자가 흐릿하게 키워드 1개만 떠올림
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from core.memory.models import MemoryRole
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.queue_manage import QueueManager
from services.memory_tools_funcs.store import MemoryStore


class _C:
    def count(self, t):
        return max(1, len(str(t or "")) // 4)


# 100턴치 user 질문 풀 — 도메인은 4개로 섞음. 각 turn은 서로 다른 키워드를 가짐.
_USER_TEMPLATES = [
    # SEMI
    "삼성전자 {year}년 목표주가 전망 알려줘",
    "분기 배당금 {ratio}% 인상 가능한가요",
    "반도체 업황 {region} 회복 시점은",
    "AI 데이터센터 {vendor} 발주 강도",
    "메모리 가격 {trend} 추세 언제 반전",
    "주주환원 정책 {item} 업데이트",
    "환율 {currency} 영향 분석",
    "자사주 매입 {amount}억 규모",
    # COOK
    "김치찌개 신김치 {grams}g 비율 적당한가요",
    "스테이크 {doneness} 굽는 시간 조언",
    "파스타 면수 {usage} 활용법",
    "베이킹 {dough} 발효 시간 가이드",
    "비빔밥 {ingredient} 배합 비율",
    "탕수육 {sauce} 소스 레시피",
    # TRAVEL
    "교토 {season} 단풍 추천 시기",
    "후쿠오카 공항 {transport} 이동",
    "유럽 {card} 분실 시 절차",
    "오사카 {food} 맛집 추천",
    "방콕 {market} 시장 동선",
    # MISC
    "운동 루틴 {workout} 효과 분석",
    "독서 모임 {book} 추천 도서",
    "정리 정돈 {area} 공간 효율",
    "재테크 {fund} 펀드 비교",
]

_FILLERS = {
    "year": ["2026", "2027", "2028", "2029", "2030"],
    "ratio": ["3", "5", "7", "10", "12"],
    "region": ["북미", "유럽", "중국", "동남아", "국내"],
    "vendor": ["하이퍼스케일러", "엔비디아", "TSMC", "오라클", "MS"],
    "trend": ["하락", "정체", "상승", "둔화", "급등"],
    "item": ["분기배당", "자사주매입", "특별배당", "스톡옵션", "EPS가이던스"],
    "currency": ["원달러환율", "엔화", "위안화", "유로화", "원파운드"],
    "amount": ["1000", "2000", "5000", "8000", "10000"],
    "grams": ["200", "250", "300", "350", "400"],
    "doneness": ["미디엄레어", "미디엄", "웰던", "레어", "미디엄웰"],
    "usage": ["크림소스", "토마토소스", "오일파스타", "라구소스", "알리오올리오"],
    "dough": ["식빵반죽", "통밀반죽", "사워도우", "치아바타", "포카치아"],
    "ingredient": ["고추장", "참기름", "소금간", "달걀비율", "나물조합"],
    "sauce": ["케찹베이스", "식초베이스", "간장베이스", "굴소스", "파인애플"],
    "season": ["10월말", "11월초", "11월중순", "12월초", "12월중순"],
    "transport": ["지하철", "공항버스", "택시", "렌터카", "셔틀"],
    "card": ["비자", "마스터카드", "아멕스", "유니온페이", "JCB"],
    "food": ["타코야끼", "오코노미야끼", "쿠시카츠", "라멘", "스시"],
    "market": ["짜뚜짝", "롬프라오", "차이나타운", "딸랏롯파이", "끄렁삔야오"],
    "workout": ["풀업", "데드리프트", "벤치프레스", "스쿼트", "오버헤드프레스"],
    "book": ["사피엔스", "총균쇠", "팩트풀니스", "코스모스", "지대넓얕"],
    "area": ["부엌수납", "현관입구", "옷장정리", "서재공간", "다용도실"],
    "fund": ["인덱스펀드", "ETF상품", "리츠펀드", "채권펀드", "혼합형펀드"],
}


def _gen_turns(n_pairs: int) -> list[str]:
    """Deterministic synthesis — same seed always produces same data."""
    questions: list[str] = []
    for i in range(n_pairs):
        tmpl = _USER_TEMPLATES[i % len(_USER_TEMPLATES)]
        # Pick filler values by position to keep this deterministic
        filled = tmpl
        for slot, values in _FILLERS.items():
            if "{" + slot + "}" in filled:
                filled = filled.replace("{" + slot + "}", values[i % len(values)])
        questions.append(filled)
    return questions


_PARTICLES = ("의", "이", "가", "을", "를", "은", "는", "도", "만", "에", "와", "과", "로")
_STOPWORDS = frozenset({
    "알려줘", "분석", "추천", "어떻게", "왜", "언제", "어디", "무엇", "그리고", "하지만",
    "가능한가요", "관련", "방법", "있나요", "활용법", "절차", "시점은", "이동", "효율",
    "조언", "가이드", "효과", "비교", "비율", "동선", "맛집", "공간",
})


def _strip_particle(token: str) -> str:
    if len(token) < 2:
        return token
    for p in _PARTICLES:
        if len(token) - len(p) >= 1 and token.endswith(p):
            return token[: -len(p)]
    return token


def _extract_nouns(text: str) -> list[str]:
    """Same heuristic the FtsMemoryStore uses for picking content tokens."""
    raw = re.findall(r"[\w가-힣]+", str(text or "").lower())
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw:
        stem = _strip_particle(tok)
        if len(stem) < 2:
            continue
        if stem in _STOPWORDS:
            continue
        if stem in seen:
            continue
        seen.add(stem)
        out.append(stem)
    # length-desc to favor compound nouns
    out.sort(key=len, reverse=True)
    return out


def _run(n_pairs: int) -> None:
    questions = _gen_turns(n_pairs)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = MemoryStore(Path(tmp), reuse_connection=True)
        recall = RecallStorage(store, _C())
        queue = QueueManager(store, _C(), recall)
        try:
            recorded: list[str] = []
            for q in questions:
                queue.append_event(role=MemoryRole.USER, content=q, source="bench")
                queue.append_event(
                    role=MemoryRole.ASSISTANT, content=f"답변: {q}", source="bench"
                )
                recorded.append(q)

            tail_count = len(recall.tail(limit=10_000))
            assert tail_count == 2 * n_pairs, f"adapter lost rows: {tail_count}"

            modes = ("full", "two-noun", "one-noun")
            stats = {m: {"top2": 0, "top5": 0} for m in modes}

            for q in recorded:
                nouns = _extract_nouns(q)
                queries = {
                    "full": q,
                    "two-noun": " ".join(nouns[:2]) if len(nouns) >= 2 else (nouns[0] if nouns else q),
                    "one-noun": nouns[0] if nouns else q,
                }
                for mode, query in queries.items():
                    for k, key in ((2, "top2"), (5, "top5")):
                        hits = recall.search(query, limit=k)
                        # q (the original user turn) should appear in the top-k
                        if any(q == str(h.get("content") or "") for h in hits):
                            stats[mode][key] += 1

            print(f"\n=== {n_pairs} pairs ({2*n_pairs} rows in recall) ===")
            print(f"{'mode':<12}{'top-2 hit':<14}{'top-5 hit':<14}")
            for m in modes:
                t2 = stats[m]["top2"]
                t5 = stats[m]["top5"]
                print(
                    f"{m:<12}{t2}/{n_pairs} ({100*t2/n_pairs:.1f}%)   "
                    f"{t5}/{n_pairs} ({100*t5/n_pairs:.1f}%)"
                )
        finally:
            store.close()


def main() -> None:
    for n in (20, 50, 100, 200):
        _run(n)


if __name__ == "__main__":
    main()
