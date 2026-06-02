"""Probe: can the recall tier surface evicted Korean turns via FTS5?

Not a unit test — a behavioral measurement. Seeds three independent topic
clusters (semiconductor / cooking / travel) into one workspace's recall tier,
then runs queries that should hit a specific cluster. Two axes are measured:

  recall  — does the right cluster's row appear in top-k?
  precision — do *other* clusters' rows wrongly appear in top-k?

LIKE-fallback substring matching can produce false positives ("주가" vs
"한국주가" / "물가") — this bench surfaces them.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from core.memory.models import MemoryRole
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.main_context.queue_manage import QueueManager
from services.memory_tools_funcs.store import MemoryStore


class _IdentityCounter:
    def count(self, text: str) -> int:
        return max(1, len(str(text or "")) // 4)


# Three disjoint topic clusters. The tag prefixes the content so a hit can
# be classified by cluster without needing a second key column. The tag is
# stripped before injection so it doesn't bias FTS.
_SEMI = [
    ("user",      "삼성전자 2026년 목표주가는 어떻게 전망되나요?"),
    ("assistant", "보수적 시나리오 80,000원, 기본 95,000원, 낙관 120,000원으로 추정됩니다."),
    ("user",      "삼성전자의 분기 배당금 인상 일정이 있나요?"),
    ("assistant", "2026년 2분기 실적 발표 시점에 배당 정책 업데이트가 예정되어 있습니다."),
    ("user",      "반도체 업황 회복이 늦어지면 어떤 리스크가 있나요?"),
    ("assistant", "메모리 가격 하락 + AI 수요 둔화가 겹치면 영업이익 -15% 시나리오가 가능합니다."),
    ("user",      "AI 데이터센터 수요 전망은?"),
    ("assistant", "북미 hyperscaler 발주가 2026년 하반기까지 강세를 유지할 것으로 봅니다."),
    ("user",      "주주환원 정책 변경 가능성은?"),
    ("assistant", "자사주 매입 확대안이 이사회 안건으로 올라와 있습니다."),
]

_COOK = [
    ("user",      "김치찌개 끓일 때 신김치 비율이 어느 정도가 좋아요?"),
    ("assistant", "발효가 충분히 진행된 신김치를 두부 한 모당 250g 정도로 잡으면 균형이 좋습니다."),
    ("user",      "스테이크 미디엄레어 굽는 시간 알려주세요"),
    ("assistant", "2.5cm 두께 기준 강불에서 면당 2분, 휴지 5분이 일반적 가이드입니다."),
    ("user",      "파스타 면수 활용은 어떻게?"),
    ("assistant", "면수의 전분이 소스 점도를 잡아주니 면당 1국자씩 추가하며 농도를 맞춥니다."),
    ("user",      "베이킹 발효 시간이 일정하지 않은 이유는?"),
    ("assistant", "주변 온도와 반죽 수분 함량 차이 때문에 동일 레시피도 30분 이상 차이가 날 수 있습니다."),
]

_TRAVEL = [
    ("user",      "교토 가을 단풍 시기 추천일이 언제예요?"),
    ("assistant", "11월 중순부터 하순이 절정이고, 아라시야마와 도후쿠지가 대표 명소입니다."),
    ("user",      "후쿠오카 공항에서 시내 이동 수단 비교 좀"),
    ("assistant", "지하철 공항선이 5분 간격으로 운행되어 가장 빠르고 편리합니다."),
    ("user",      "유럽 신용카드 분실 시 대응 절차는?"),
    ("assistant", "카드사 글로벌 콜센터에 즉시 분실신고하고, 대사관에서 임시여권을 발급받습니다."),
    ("user",      "환율이 갑자기 오르면 여행 예산 어떻게 보정?"),
    ("assistant", "현지 카드 결제는 환차손이 커지므로 사전에 일부를 외화로 환전해 둡니다."),
]

# (label, query, expected_cluster_keyword_in_some_hit, expected_cluster_tag)
# expected_tag is the cluster the hit *should* come from.
# False-positive measurement: hits from other clusters count as precision misses.
_SCENARIOS = [
    # — 같은 도메인 내 검색 정확도 —
    ("A.동일 키워드",            "삼성전자 2026 목표주가",     "목표주가", "SEMI"),
    ("B.조사/어미 변형",          "삼성전자의 목표 주가 알려줘",   "목표주가", "SEMI"),
    ("C.짧은 키워드",            "주가 전망",                  "목표주가", "SEMI"),
    ("D.정보 빈약 follow-up",   "그거 좀 더 자세히 알려줘",      "목표주가", "SEMI"),
    ("E.합성어 prefix",          "삼성에 대해 말한 거 다시 보여줘", "삼성전자", "SEMI"),
    ("F.키워드 일치(다른 표현)",   "주주환원 정책",              "주주환원", "SEMI"),
    ("G.영문 약어 무관",         "AI hyperscaler",            "hyperscaler", "SEMI"),
    ("H.이중 의도",              "리스크와 배당 관련해서 정리",   "리스크", "SEMI"),
    # — 다른 도메인으로 격리 검증 —
    ("I.요리: 키워드 일치",       "김치찌개 비율",              "신김치", "COOK"),
    ("J.요리: 짧은 키워드",       "면수 활용",                   "면수", "COOK"),
    ("K.요리: 합성어 prefix",     "베이킹 시간 왜 달라요",        "베이킹", "COOK"),
    ("L.여행: 도메인 매칭",       "교토 단풍 시기",              "교토", "TRAVEL"),
    ("M.여행: 영문 도시명",       "후쿠오카 공항 이동",          "후쿠오카", "TRAVEL"),
    ("N.여행: 위험 케이스(환율)", "환율 오를 때 여행 예산",       "환율", "TRAVEL"),
    # — 도메인 cross-talk false-positive 측정 —
    # query는 한 도메인 키워드인데, 다른 도메인 row가 잘못 끼면 precision 미스.
    ("O.도메인 분리(반도체)",     "반도체 메모리 가격",          "메모리", "SEMI"),
    ("P.도메인 분리(여행 환율)",  "여행 갈 때 환율 어떻게",      "환율", "TRAVEL"),
    # — 더 엄격한 precision 테스트 — query 키워드가 다른 도메인에도 substring으로
    # 우연히 겹칠 가능성이 있는 케이스.
    ("Q.AI 수요(반도체로)",      "AI 수요 전망",               "AI",   "SEMI"),
    ("R.요리 발효(요리로)",      "베이킹 발효 시간",             "발효", "COOK"),
    ("S.여행 카드 분실",         "신용카드 분실 절차",          "카드사", "TRAVEL"),
    # — false-positive trap — substring이 우연히 다른 도메인에 매칭될 수 있는 케이스.
    # query "정책" 한 키워드만 — SEMI("주주환원 정책")가 정답이지만 TRAVEL/COOK에
    # 우연 매칭이 없으면 정상.
    ("T.단일 키워드 정책",       "정책 변경 가능성",            "정책", "SEMI"),
]

# Tag mapping for false-positive detection
_TAGGED: list[tuple[str, str]] = (
    [("SEMI", c) for _, c in _SEMI] +
    [("COOK", c) for _, c in _COOK] +
    [("TRAVEL", c) for _, c in _TRAVEL]
)
_TAG_BY_CONTENT = {content: tag for tag, content in _TAGGED}


def _seed(queue: QueueManager) -> None:
    bundle = [
        *[("SEMI", r, c) for r, c in _SEMI],
        *[("COOK", r, c) for r, c in _COOK],
        *[("TRAVEL", r, c) for r, c in _TRAVEL],
    ]
    for _tag, role_str, content in bundle:
        role = MemoryRole.USER if role_str == "user" else MemoryRole.ASSISTANT
        queue.append_event(role=role, content=content, source="bench")


def _evict_old_fifo(queue: QueueManager, keep_tail: int = 6) -> int:
    rows = queue.fifo.all()
    if len(rows) <= keep_tail:
        return 0
    evicted = rows[:-keep_tail]
    evicted_ids = {str(r.get("id") or "") for r in evicted}
    queue.fifo.delete_ids(evicted_ids)
    return len(evicted_ids)


def _run_scenarios(recall: RecallStorage) -> None:
    print()
    print(
        f"{'시나리오':<28} {'top2 recall':<12} "
        f"{'top2 precision':<16} {'top5 recall':<12} {'top5 precision':<16}"
    )
    print("-" * 92)
    summary = {"top2_hit": 0, "top5_hit": 0, "top2_clean": 0, "top5_clean": 0, "n": 0}
    for label, query, needle, expected_tag in _SCENARIOS:
        summary["n"] += 1
        line = f"{label:<28}"
        for k in (2, 5):
            hits = recall.search(query, limit=k)
            recall_hit = any(
                needle in str(h.get("content") or "") for h in hits
            )
            # precision: every returned row should belong to expected_tag
            tags = [_TAG_BY_CONTENT.get(str(h.get("content") or ""), "UNK") for h in hits]
            wrong = [t for t in tags if t not in (expected_tag, "UNK")]
            precision_clean = len(wrong) == 0 and len(hits) > 0
            if recall_hit:
                summary[f"top{k}_hit"] += 1
            if precision_clean:
                summary[f"top{k}_clean"] += 1
            r_mark = "✓" if recall_hit else "✗"
            p_mark = (
                "✓"
                if precision_clean
                else (f"✗({len(wrong)}/{len(hits)})" if hits else "∅")
            )
            line += f" {r_mark:<11} {p_mark:<15}"
        print(line)

    n = summary["n"]
    print("-" * 92)
    print(
        f"summary: top2 recall {summary['top2_hit']}/{n}, "
        f"top2 precision-clean {summary['top2_clean']}/{n}, "
        f"top5 recall {summary['top5_hit']}/{n}, "
        f"top5 precision-clean {summary['top5_clean']}/{n}"
    )


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        store = MemoryStore(root, reuse_connection=True)
        counter = _IdentityCounter()
        recall = RecallStorage(store, counter)
        queue = QueueManager(store, counter, recall)
        try:
            _seed(queue)
            fifo_before = queue.fifo.count()
            recall_total = len(recall.tail(limit=1000))
            print(f"[seed] FIFO rows={fifo_before}, recall rows={recall_total}")

            evicted = _evict_old_fifo(queue, keep_tail=6)
            recall_after = len(recall.tail(limit=1000))
            print(
                f"[evict] FIFO 6 kept, evicted {evicted}, recall still {recall_after}"
            )
            if recall_after != recall_total:
                print("[bug] recall lost rows during FIFO eviction!")
                return

            _run_scenarios(recall)
        finally:
            store.close()


if __name__ == "__main__":
    main()
