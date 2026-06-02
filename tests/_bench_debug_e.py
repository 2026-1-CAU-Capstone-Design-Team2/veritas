"""E 시나리오 단독 디버그."""
import sys
sys.path.insert(0, ".")
import tempfile
from pathlib import Path
from core.memory.models import MemoryRole
from services.memory_tools_funcs.external_context.recall_storage import RecallStorage
from services.memory_tools_funcs.external_context.fts_memory_store import FtsMemoryStore
from services.memory_tools_funcs.main_context.queue_manage import QueueManager
from services.memory_tools_funcs.store import MemoryStore

class C:
    def count(self, t): return max(1, len(str(t or ""))//4)

_TURNS = [
    ("user",      "삼성전자 2026년 목표주가는 어떻게 전망되나요?"),
    ("assistant", "보수적 시나리오에서 80,000원, 기본 95,000원."),
    ("user",      "삼성전자의 분기 배당금 인상 일정이 있나요?"),
    ("assistant", "배당 정책 업데이트가 예정되어 있습니다."),
    ("user",      "반도체 리스크는?"),
    ("assistant", "메모리 가격 하락 + AI 수요 둔화."),
    ("user",      "AI 데이터센터 수요 전망?"),
    ("assistant", "북미 hyperscaler 발주 강세."),
    ("user",      "주주환원 정책 변경?"),
    ("assistant", "자사주 매입 확대안 이사회 안건."),
    ("user",      "환율 영향?"),
    ("assistant", "원화 강세는 단기 부담."),
]

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    store = MemoryStore(Path(tmp), reuse_connection=True)
    recall = RecallStorage(store, C())
    queue = QueueManager(store, C(), recall)
    try:
        for r, c in _TURNS:
            role = MemoryRole.USER if r=="user" else MemoryRole.ASSISTANT
            queue.append_event(role=role, content=c, source="x")
        q = "삼성에 대해 말한 거 다시 보여줘"
        print(f"Query: {q!r}")
        print(f"_fts_query: {FtsMemoryStore._fts_query(q)!r}")
        print(f"_like_terms: {FtsMemoryStore._like_terms(q)!r}")
        print()
        print("FTS top-10 raw:")
        from services.memory_tools_funcs.external_context.fts_memory_store import FtsMemoryStore as F
        with store.connection() as conn:
            recall._ensure_schema(conn)
            fts_q = F._fts_query(q)
            if fts_q:
                rows = conn.execute(
                    f"SELECT recall_items.* FROM recall_fts JOIN recall_items ON recall_items.id=recall_fts.id WHERE recall_fts MATCH ? LIMIT 10",
                    (fts_q,),
                ).fetchall()
                for r in rows:
                    print(f"  -> {r['content'][:60]}")
            else:
                print("  (empty FTS query)")
        print()
        print("LIKE fallback:")
        like_rows = recall._like_fallback(q, limit=10)
        for r in like_rows:
            print(f"  -> {r.get('content')[:60]}")
        print()
        print("search(limit=2) final:")
        results = recall.search(q, limit=2)
        for r in results:
            print(f"  -> {r.get('content')[:60]}")
        print()
        print(f"Needle '삼성전자' hit? {any('삼성전자' in r.get('content','') for r in results)}")
    finally:
        store.close()
