"""AutoSurvey collect-loop perf features: post-fetch rejection + early stop.

Covers INSTRUCTION items 4 (off-topic fetched bodies are recorded as rejected
notes and never consume a maxDocs slot) and 6 (early-stop decision once enough
coverage is reached). The full ``run_all`` loop needs a live LLM/registry, so we
test the seams directly: the pure ``_early_stop_decision``, the RunStore note
writer, and ``_fetch_one`` against a fake fetch tool.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.autosurvey_source_quality import build_topic_terms
from services.run_store_tool_funcs import RunStoreService
from tools.tool import ToolResult
from workflows.autosurvey_workflow import AutoSurveyWorkflow
from workflows.config import AutoSurveyConfig


_PLAN = {
    "topic": "대체육 식물성 단백질 시장",
    "goal": "국내외 시장 규모와 성장 전망, 주요 업체 동향 조사",
    "keywords": ["plant-based meat", "대체육", "식물성 단백질"],
}
_TOPIC = build_topic_terms(plan=_PLAN, query="국내외 대체육 시장 규모 전망")

_ON_TOPIC_BODY = (
    "본 보고서는 글로벌 plant-based meat(대체육) 시장 규모와 식물성 단백질 성장 "
    "전망, 주요 업체 동향을 분석한다. 2025년 시장 규모는 크게 성장할 전망이다."
)
_OFF_TOPIC_BODY = (
    "This complete guide covers the best bath bomb recipes, essential oil blends, "
    "and fragrance tips for a relaxing at-home spa night."
)


class _FakeFetched:
    def __init__(self, text: str, *, title: str = "T", url: str = "https://e.com/a") -> None:
        self.text = text
        self.title = title
        self.url = url
        self.final_url = url
        self.domain = "e.com"
        self.html = "<html><body>x</body></html>"
        self.content_type = "text/html"


class _FakeFetchTool:
    def __init__(self, fetched: _FakeFetched) -> None:
        self._fetched = fetched

    def run(self, **kwargs):  # noqa: ANN003
        return ToolResult(success=True, data=self._fetched)


class _FakeRegistry:
    def __init__(self, tool: _FakeFetchTool) -> None:
        self._tool = tool

    def get(self, name: str):
        return self._tool


def _workflow(tmp: str, fetched: _FakeFetched) -> tuple[AutoSurveyWorkflow, RunStoreService]:
    store = RunStoreService(Path(tmp))
    wf = AutoSurveyWorkflow(
        registry=_FakeRegistry(_FakeFetchTool(fetched)),
        run_store_service=store,
        config=AutoSurveyConfig(max_docs=15),
        progress_callback=lambda *a, **k: None,
    )
    return wf, store


class EarlyStopDecisionTests(unittest.TestCase):
    decide = staticmethod(AutoSurveyWorkflow._early_stop_decision)

    def test_below_min_docs_never_stops(self) -> None:
        self.assertEqual(
            self.decide(kept=5, min_docs=9, gap_directions=[], accepted_this_cycle=0),
            (False, None),
        )

    def test_no_core_gap_past_min_stops(self) -> None:
        self.assertEqual(
            self.decide(kept=10, min_docs=9, gap_directions=[], accepted_this_cycle=3),
            (True, "no_core_gap"),
        )

    def test_first_low_gain_cycle_with_open_gap_does_not_stop(self) -> None:
        # A single slow cycle while a core gap remains must not end the run —
        # the loop should replan/retry first.
        self.assertEqual(
            self.decide(
                kept=10,
                min_docs=9,
                gap_directions=["남은 gap"],
                accepted_this_cycle=1,
                low_gain_streak=1,
                queries_exhausted=False,
            ),
            (False, None),
        )

    def test_repeated_low_gain_with_open_gap_stops(self) -> None:
        self.assertEqual(
            self.decide(
                kept=10,
                min_docs=9,
                gap_directions=["남은 gap"],
                accepted_this_cycle=1,
                low_gain_streak=2,
                queries_exhausted=False,
            ),
            (True, "low_marginal_gain"),
        )

    def test_low_gain_with_exhausted_queries_stops(self) -> None:
        self.assertEqual(
            self.decide(
                kept=10,
                min_docs=9,
                gap_directions=["남은 gap"],
                accepted_this_cycle=1,
                low_gain_streak=1,
                queries_exhausted=True,
            ),
            (True, "low_marginal_gain"),
        )

    def test_good_gain_never_stops_even_if_queries_exhausted(self) -> None:
        self.assertEqual(
            self.decide(
                kept=10,
                min_docs=9,
                gap_directions=["남은 gap"],
                accepted_this_cycle=4,
                low_gain_streak=0,
                queries_exhausted=True,
            ),
            (False, None),
        )


class RejectedNoteTests(unittest.TestCase):
    def test_note_written_without_kept_slot_or_raw_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStoreService(Path(tmp))
            rid = store.write_rejected_note(
                url="https://e.com/x",
                title="제목",
                domain="e.com",
                search_query="q",
                reason="off_topic",
                score=0,
            )
            self.assertTrue(rid.startswith("rejected_"))
            # No kept record, so no maxDocs slot consumed.
            self.assertEqual(len(store.list_non_duplicate_records()), 0)
            note = (store.paths.summary_dir / f"{rid}.md").read_text(encoding="utf-8")
            self.assertIn("off_topic", note)
            self.assertNotIn("body", note.lower())  # metadata only, no raw text


_SAMSUNG_TOPIC = build_topic_terms(
    user_request="삼성전자 작년 4분기 잠정 실적 알려줘",
    plan={
        "topic": "삼성전자 4분기 실적",
        "keywords": ["삼성전자", "4분기", "실적", "매출", "영업이익"],
    },
    query="삼성전자 4분기 실적",
    anchor_terms=["삼성전자"],
)
_SAMSUNG_BODY = (
    "삼성전자는 2024년 4분기 잠정 실적을 발표했다. 매출 70조원, 영업이익 4조원을 기록했다."
)
# Different company, same generic financial vocabulary, never names 삼성전자.
_WRONG_ENTITY_BODY = (
    "쿠팡은 2024년 4분기 실적을 공개했다. 매출 88억 달러, 영업이익은 적자로 전환했다."
)


class EntityAnchorFetchTests(unittest.TestCase):
    def test_wrong_entity_body_is_rejected_and_keeps_no_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wf, store = _workflow(tmp, _FakeFetched(_WRONG_ENTITY_BODY))
            result = wf._fetch_one(
                title_hint="쿠팡 실적",
                url="https://e.com/coupang-q4",
                query="4분기 실적",
                topic=_SAMSUNG_TOPIC,
            )
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(len(store.list_non_duplicate_records()), 0)

    def test_right_entity_body_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wf, store = _workflow(tmp, _FakeFetched(_SAMSUNG_BODY))
            result = wf._fetch_one(
                title_hint="삼성전자 실적",
                url="https://e.com/samsung-q4",
                query="4분기 실적",
                topic=_SAMSUNG_TOPIC,
            )
            self.assertEqual(result["status"], "fetched")
            self.assertEqual(len(store.list_non_duplicate_records()), 1)


class FetchRejectionTests(unittest.TestCase):
    def test_offtopic_body_is_rejected_and_keeps_no_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wf, store = _workflow(tmp, _FakeFetched(_OFF_TOPIC_BODY))
            result = wf._fetch_one(
                title_hint="x", url="https://e.com/a", query="q", topic=_TOPIC
            )
            self.assertEqual(result["status"], "rejected")
            self.assertTrue(result["doc_id"].startswith("rejected_"))
            self.assertEqual(len(store.list_non_duplicate_records()), 0)

    def test_ontopic_body_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wf, store = _workflow(tmp, _FakeFetched(_ON_TOPIC_BODY))
            result = wf._fetch_one(
                title_hint="x", url="https://e.com/a", query="q", topic=_TOPIC
            )
            self.assertEqual(result["status"], "fetched")
            self.assertEqual(len(store.list_non_duplicate_records()), 1)

    def test_no_topic_skips_rejection(self) -> None:
        # Reference-site fetches pass topic=None and are never rejected.
        with tempfile.TemporaryDirectory() as tmp:
            wf, store = _workflow(tmp, _FakeFetched(_OFF_TOPIC_BODY))
            result = wf._fetch_one(title_hint="x", url="https://e.com/a", query="q")
            self.assertEqual(result["status"], "fetched")
            self.assertEqual(len(store.list_non_duplicate_records()), 1)


if __name__ == "__main__":
    unittest.main()
