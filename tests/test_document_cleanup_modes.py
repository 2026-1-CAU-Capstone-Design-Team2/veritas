from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from services.run_store_tool_funcs import RunStoreService
from tools.document_cleanup_tool import DocumentCleanupTool
from tools.loader import TOOLS_DIR, load_schema


_LOCAL_CLEANUP_RESPONSE = (
    "BOILERPLATE_PARAGRAPHS\n2\n\n===\n\n"
    "SUMMARY\nLocal cleanup summary.\n\n===\n\n"
    "KEYWORDS\n- alpha\n- beta\n\n===\n\n"
    "KEY_POINTS\n- local point one\n- local point two\n"
)


class FakeLocalLLM:
    """Mimics the local llama-server client — no 'openai' in module/class name."""

    def __init__(self) -> None:
        self.ask_calls = 0
        self.ask_json_calls = 0
        self.n_ctx = 50_000

    def ask(self, system_prompt, user_prompt, reasoning=False, **kwargs):
        self.ask_calls += 1
        return _LOCAL_CLEANUP_RESPONSE

    def ask_json(self, system_prompt, user_prompt, reasoning=False, **kwargs):
        self.ask_json_calls += 1
        return {}

    def map_parallel(self, items, worker, **kwargs):
        return [worker(item) for item in items]


class FakeOpenAIChatLLMClient:
    """Class name contains 'openai' → cleanup_mode 'auto' resolves to batch."""

    def __init__(self, *, fail_json: bool = False) -> None:
        self.ask_calls = 0
        self.ask_json_calls = 0
        self.ask_json_inputs: list[str] = []
        self.fail_json = fail_json
        self.n_ctx = 400_000

    def ask(self, system_prompt, user_prompt, reasoning=False, **kwargs):
        # Only reached when this client is forced onto the per_doc path. Return
        # a well-formed cleanup response (in the retention band) so the
        # per_doc retry logic stays out of the call-count assertions.
        self.ask_calls += 1
        return _LOCAL_CLEANUP_RESPONSE

    def ask_json(self, system_prompt, user_prompt, reasoning=False, **kwargs):
        self.ask_json_calls += 1
        self.ask_json_inputs.append(user_prompt)
        if self.fail_json:
            raise RuntimeError("simulated API failure")
        doc_ids = re.findall(r"=== doc_(\S+) ===", user_prompt)
        return {
            "documents": [
                {
                    "doc_id": doc_id,
                    "summary": f"OpenAI summary for doc {doc_id}.",
                    "keywords": ["hbm", "메모리"],
                    "key_points": [
                        f"Doc {doc_id} key point one.",
                        f"Doc {doc_id} key point two.",
                    ],
                }
                for doc_id in doc_ids
            ]
        }

    def map_parallel(self, items, worker, **kwargs):
        return [worker(item) for item in items]


def make_run_store(root: Path, doc_count: int) -> RunStoreService:
    """Build a workspace with `doc_count` kept records + raw_md bodies."""
    store = RunStoreService(root)
    records = []
    for index in range(1, doc_count + 1):
        doc_id = str(index)
        (store.paths.raw_md_dir / f"{doc_id}.md").write_text(
            f"# Document {doc_id}\n\n"
            f"Body paragraph about topic {doc_id} with real findings.\n\n"
            "Navigation menu | Footer | Cookie banner",
            encoding="utf-8",
        )
        records.append(
            {
                "doc_id": doc_id,
                "title": f"Doc {doc_id}",
                "url": f"https://example.com/{doc_id}",
                "final_url": f"https://example.com/{doc_id}",
                "domain": "example.com",
                "search_query": "test query",
            }
        )
    store.paths.index_path.write_text(
        json.dumps({"records": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    return store


def make_tool(store: RunStoreService, llm, cleanup_mode: str = "auto") -> DocumentCleanupTool:
    return DocumentCleanupTool(
        schema=load_schema(TOOLS_DIR / "document_cleanup_tool" / "tool_schema.json"),
        llm=llm,
        run_store_service=store,
        cleanup_mode=cleanup_mode,
    )


# An article page wrapped in semantic chrome: <nav>/<header>/<aside>/<footer>
# surround the real <article> body. Structural extraction should keep the
# article (heading + paragraphs) and drop the chrome by tag/role alone.
_ARTICLE_HTML = """<html><body>
<nav>Home Products About Contact Sign In</nav>
<header>Site Banner Logo</header>
<article>
<h1>Quantum Widgets Overview</h1>
<p>Quantum widgets achieved a 42 percent efficiency gain in 2026 benchmarks
across twelve datasets, outperforming the classical baseline by a wide margin
in every category that the authors measured during the study.</p>
<p>The architecture uses a layered transition model with explicit state
caching, and the team reports stable convergence within fifty training epochs
on commodity hardware without any specialized accelerators.</p>
</article>
<aside>Related Articles: Foo Bar Baz Newsletter Signup</aside>
<footer>Copyright 2026 Share on Twitter Privacy Policy Cookie Settings</footer>
</body></html>"""


def _write_raw_html(store: RunStoreService, doc_id: str, html: str) -> None:
    store.paths.raw_html_dir.mkdir(parents=True, exist_ok=True)
    (store.paths.raw_html_dir / f"{doc_id}.html").write_text(html, encoding="utf-8")


# Long, low-link article paragraphs so a multi-paragraph body comfortably clears
# the prose-quality gate (real article bodies measured at 1,400-3,000 chars).
_PARA_A = (
    "삼성전자 반도체 부문은 인공지능 가속기에 쓰이는 고대역폭 메모리 수요 급증에 힘입어 분기 사상 최대 영업이익을 기록했다고 회사 측은 설명했다. "
    "서버용 D램 가격이 분기 내내 가파르게 상승하면서 메모리 사업의 수익성이 큰 폭으로 개선되었고, 데이터센터 고객사의 선제적 재고 확보 움직임도 이어졌다. "
    "특히 HBM3E 양산 물량이 본격적으로 확대되면서 고부가 제품 비중이 높아진 점이 실적 개선의 핵심 동력으로 작용했다고 분석된다."
)
_PARA_B = (
    "회사는 다음 분기에도 AI 및 서버 수요를 중심으로 반도체 사업의 성장세가 이어질 것으로 전망하면서도, 글로벌 관세와 거시 경제 불확실성을 예의주시하겠다고 밝혔다. "
    "수익성 확보 중심의 안정적인 경영 기조를 유지하는 한편, 차세대 HBM4 개발과 파운드리 선단 공정 경쟁력 강화에 투자를 집중하겠다는 계획도 함께 제시했다. "
    "증권가는 메모리 슈퍼사이클이 내년까지 이어질 가능성에 무게를 두면서 목표주가를 일제히 상향 조정하는 분위기다."
)


def _nav(n: int) -> str:
    return "<nav>" + "".join(f"<a href='#'>메뉴{i}</a>" for i in range(n)) + "</nav>"


def _footer(n: int) -> str:
    return "<footer>" + "".join(f"<a href='#'>푸터{i}</a>" for i in range(n)) + "</footer>"


def _related_list(n: int) -> str:
    # A trailing related/tags cluster: short link-only list items.
    return "<ul>" + "".join(
        f"<li><a href='#'>관련 기사 링크 {i}</a></li>" for i in range(n)
    ) + "</ul>"


# A real article buried in heavy chrome — the article text is a small fraction
# of the whole page (well under 50% retention vs the noisy raw).
_CHROME_HEAVY_ARTICLE_HTML = (
    "<html><body>"
    + _nav(40)
    + "<div class='content'><h1>삼성전자 HBM 실적 분석</h1>"
    + f"<p>{_PARA_A}</p><p>{_PARA_B}</p></div>"
    + _related_list(12)
    + _footer(40)
    + "</body></html>"
)

# A page that is pure navigation / link lists — no real prose body.
_NAV_ONLY_HTML = (
    "<html><body>"
    + _nav(20)
    + "<ul>" + "".join(f"<li><a href='#'>섹션 {i}</a></li>" for i in range(30)) + "</ul>"
    + _footer(20)
    + "</body></html>"
)

# A related-articles <article> box appears BEFORE the real <main> body.
_RELATED_FIRST_HTML = (
    "<html><body>"
    + "<article><h3>관련 기사</h3>"
    + "<ul>" + "".join(f"<li><a href='#'>관련 링크 {i} 입니다</a></li>" for i in range(8)) + "</ul>"
    + "</article>"
    + f"<main><h1>본문 제목</h1><p>{_PARA_A}</p><p>{_PARA_B}</p></main>"
    + "</body></html>"
)

# A data-table-heavy source (little prose) wrapped in chrome. The table is sized
# like a real quarterly data table so the body clears the absolute-length floor.
def _share_table() -> str:
    rows = ["<tr><th>회사</th><th>Q1 2025</th><th>Q2 2025</th><th>Q3 2025</th><th>Q4 2025</th><th>전년 대비 증감</th></tr>"]
    data = [
        ("SK 하이닉스 메모리 사업부", "35%", "36%", "39%", "34%", "소폭 하락"),
        ("삼성전자 디바이스솔루션", "38%", "34%", "33%", "40%", "반등 성공"),
        ("마이크론 테크놀로지", "20%", "22%", "21%", "19%", "하락 전환"),
        ("기타 군소 업체 합계", "7%", "8%", "7%", "7%", "보합 유지"),
        ("난드 플래시 합산 점유율", "31%", "33%", "34%", "36%", "지속 상승"),
    ]
    for name, *vals in data:
        rows.append("<tr><td>" + name + "</td>" + "".join(f"<td>{v}</td>" for v in vals) + "</tr>")
    return "<table>" + "".join(rows) + "</table>"


_TABLE_HEAVY_HTML = (
    "<html><body>"
    + _nav(30)
    + "<div><h2>전세계 D램 및 HBM 시장 점유율 분기별 데이터</h2>"
    + _share_table()
    + "</div>"
    + _related_list(10)
    + _footer(30)
    + "</body></html>"
)


class CleanupModeResolutionTests(unittest.TestCase):
    def test_auto_resolves_per_doc_for_local_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            llm = FakeLocalLLM()
            tool = make_tool(store, llm, cleanup_mode="auto")

            result = tool.run(doc_ids=["1", "2"])

            self.assertTrue(result.success)
            # per_doc path: one ask() per document, no ask_json().
            self.assertEqual(llm.ask_calls, 2)
            self.assertEqual(llm.ask_json_calls, 0)

    def test_auto_resolves_batch_for_openai_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            llm = FakeOpenAIChatLLMClient()
            tool = make_tool(store, llm, cleanup_mode="auto")

            result = tool.run(doc_ids=["1", "2"])

            self.assertTrue(result.success)
            # batch path: one ask_json() for the whole cycle, no per-doc ask().
            self.assertEqual(llm.ask_json_calls, 1)
            self.assertEqual(llm.ask_calls, 0)

    def test_explicit_mode_overrides_auto_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            # OpenAI-typed client forced onto the per_doc path.
            llm = FakeOpenAIChatLLMClient()
            tool = make_tool(store, llm, cleanup_mode="per_doc")

            result = tool.run(doc_ids=["1", "2"])

            self.assertEqual(llm.ask_calls, 2)
            self.assertEqual(llm.ask_json_calls, 0)
            # Both docs must actually be cleaned — a per-doc exception that
            # halves the call count must not slip through this assertion.
            self.assertEqual(sorted(result.data["cleaned_doc_ids"]), ["1", "2"])
            self.assertEqual(result.data["failed_documents"], [])

        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            # Local-typed client forced onto the batch path.
            llm = FakeLocalLLM()
            tool = make_tool(store, llm, cleanup_mode="batch")

            result = tool.run(doc_ids=["1", "2"])

            self.assertEqual(llm.ask_json_calls, 1)
            self.assertEqual(llm.ask_calls, 0)
            self.assertEqual(sorted(result.data["cleaned_doc_ids"]), ["1", "2"])
            self.assertEqual(result.data["failed_documents"], [])


class BatchModeTests(unittest.TestCase):
    def test_one_llm_call_covers_all_cycle_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=5)
            llm = FakeOpenAIChatLLMClient()
            tool = make_tool(store, llm)

            result = tool.run(doc_ids=["1", "2", "3", "4", "5"])

            self.assertTrue(result.success)
            self.assertEqual(llm.ask_json_calls, 1)
            self.assertEqual(sorted(result.data["cleaned_doc_ids"]), ["1", "2", "3", "4", "5"])
            self.assertEqual(result.data["failed_documents"], [])
            self.assertEqual(result.data["fallback_documents"], [])

    def test_clean_md_falls_back_to_raw_when_no_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=1)  # raw_md only, no raw_html
            tool = make_tool(store, FakeOpenAIChatLLMClient())

            tool.run(doc_ids=["1"])

            raw = (store.paths.raw_md_dir / "1.md").read_text(encoding="utf-8")
            clean = (store.paths.clean_md_dir / "1.md").read_text(encoding="utf-8")
            # No archived raw_html → structural extraction is unavailable, so the
            # batch path conservatively keeps the raw_md body verbatim.
            self.assertEqual(clean, raw)

    def test_clean_md_uses_structural_extraction_when_html_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=1)
            _write_raw_html(store, "1", _ARTICLE_HTML)
            tool = make_tool(store, FakeOpenAIChatLLMClient())

            tool.run(doc_ids=["1"])

            clean = (store.paths.clean_md_dir / "1.md").read_text(encoding="utf-8")
            # Article body preserved...
            self.assertIn("Quantum Widgets Overview", clean)
            self.assertIn("42 percent efficiency", clean)
            # ...page chrome dropped by tag/role (no keyword/selector list).
            self.assertNotIn("Home Products", clean)
            self.assertNotIn("Related Articles", clean)
            self.assertNotIn("Copyright 2026", clean)

    def test_batch_metadata_input_uses_sanitized_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=1)
            _write_raw_html(store, "1", _ARTICLE_HTML)
            llm = FakeOpenAIChatLLMClient()
            tool = make_tool(store, llm)

            tool.run(doc_ids=["1"])

            self.assertTrue(llm.ask_json_inputs)
            metadata_input = llm.ask_json_inputs[0]
            # The sanitized body (not the chrome) is what the LLM sees.
            self.assertIn("Quantum widgets achieved", metadata_input)
            self.assertNotIn("Related Articles", metadata_input)
            self.assertNotIn("Copyright 2026", metadata_input)

    def test_batch_accepts_low_retention_article_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=1)
            # Big noisy raw_md so the clean article is well under 50% retention.
            (store.paths.raw_md_dir / "1.md").write_text("잡음 " * 4000, encoding="utf-8")
            _write_raw_html(store, "1", _CHROME_HEAVY_ARTICLE_HTML)
            tool = make_tool(store, FakeOpenAIChatLLMClient())

            result = tool.run(doc_ids=["1"])

            clean = (store.paths.clean_md_dir / "1.md").read_text(encoding="utf-8")
            self.assertIn("삼성전자 반도체 부문", clean)  # article kept
            self.assertNotIn("메뉴0", clean)              # nav/footer dropped
            prov = {p["docId"]: p for p in result.data["cleanup_provenance"]}
            self.assertTrue(prov["1"]["accepted"])
            self.assertEqual(prov["1"]["reason"], "accepted")
            # Accepted even though far smaller than the noisy raw (no retention gate).
            self.assertLess(prov["1"]["extractedLen"], prov["1"]["rawLen"])

    def test_cleanup_provenance_has_numbers_not_raw_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=1)
            _write_raw_html(store, "1", _CHROME_HEAVY_ARTICLE_HTML)
            tool = make_tool(store, FakeOpenAIChatLLMClient())

            result = tool.run(doc_ids=["1"])

            prov = result.data["cleanup_provenance"][0]
            self.assertEqual(
                set(prov)
                >= {
                    "docId", "accepted", "reason", "rawLen",
                    "extractedLen", "proseLen", "tableCount", "linkDensity",
                },
                True,
            )
            # No raw body text leaks into provenance — only structural numbers.
            for value in prov.values():
                self.assertNotIn("삼성전자 반도체 부문", str(value))

    def test_no_html_falls_back_with_provenance_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=1)  # raw_md only
            tool = make_tool(store, FakeOpenAIChatLLMClient())

            result = tool.run(doc_ids=["1"])

            prov = result.data["cleanup_provenance"][0]
            self.assertFalse(prov["accepted"])
            self.assertEqual(prov["reason"], "no_html")

    def test_doc_metadata_written_from_batch_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            tool = make_tool(store, FakeOpenAIChatLLMClient())

            tool.run(doc_ids=["1", "2"])

            doc_md = store.paths.summary_path_for(1).read_text(encoding="utf-8")
            self.assertIn("OpenAI summary for doc 1.", doc_md)
            self.assertIn("Doc 1 key point one.", doc_md)
            self.assertIn("## Key Points", doc_md)
            self.assertIn("## Keywords", doc_md)
            self.assertIn("hbm", doc_md)

    def test_metadata_call_failure_degrades_to_fallback_not_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            tool = make_tool(store, FakeOpenAIChatLLMClient(fail_json=True))

            result = tool.run(doc_ids=["1", "2"])

            # Still a success: clean_md exists, docs are usable downstream.
            self.assertTrue(result.success)
            self.assertEqual(sorted(result.data["cleaned_doc_ids"]), ["1", "2"])
            self.assertEqual(result.data["failed_documents"], [])
            self.assertEqual(len(result.data["fallback_documents"]), 2)
            # clean_md still written.
            self.assertTrue((store.paths.clean_md_dir / "1.md").exists())
            # doc_<id>.md falls back to the title-caption summary.
            doc_md = store.paths.summary_path_for(1).read_text(encoding="utf-8")
            self.assertIn("Doc 1", doc_md)

    def test_emits_same_progress_events_as_per_doc_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            tool = make_tool(store, FakeOpenAIChatLLMClient())
            events: list[tuple[str, dict]] = []

            tool.run(
                doc_ids=["1", "2"],
                progress_callback=lambda kind, **info: events.append((kind, info)),
            )

            kinds = [kind for kind, _ in events]
            self.assertEqual(kinds.count("doc_cleanup_started"), 2)
            self.assertEqual(kinds.count("doc_cleanup_done"), 2)
            done_events = [info for kind, info in events if kind == "doc_cleanup_done"]
            for info in done_events:
                # The workflow forwards summary_path to the frontend so the
                # document card can flip to its ready state.
                self.assertTrue(str(info.get("summary_path") or ""))
                self.assertFalse(info.get("used_fallback"))

    def test_existing_clean_md_skipped_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            store.write_clean_md("1", "previously cleaned body")
            llm = FakeOpenAIChatLLMClient()
            tool = make_tool(store, llm)

            result = tool.run(doc_ids=["1", "2"])

            self.assertEqual(result.data["skipped_existing_doc_ids"], ["1"])
            self.assertEqual(result.data["cleaned_doc_ids"], ["2"])
            # Skipped doc keeps its existing clean_md untouched.
            self.assertEqual(
                (store.paths.clean_md_dir / "1.md").read_text(encoding="utf-8"),
                "previously cleaned body",
            )

    def test_empty_raw_md_reported_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=2)
            (store.paths.raw_md_dir / "2.md").write_text("", encoding="utf-8")
            tool = make_tool(store, FakeOpenAIChatLLMClient())

            result = tool.run(doc_ids=["1", "2"])

            self.assertEqual(result.data["cleaned_doc_ids"], ["1"])
            self.assertEqual(len(result.data["failed_documents"]), 1)
            self.assertEqual(result.data["failed_documents"][0]["docId"], "2")

    def test_large_cycle_chunks_into_multiple_calls(self) -> None:
        doc_count = DocumentCleanupTool._BATCH_METADATA_MAX_DOCS + 3
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=doc_count)
            llm = FakeOpenAIChatLLMClient()
            tool = make_tool(store, llm)

            result = tool.run(doc_ids=[str(i) for i in range(1, doc_count + 1)])

            self.assertEqual(llm.ask_json_calls, 2)
            self.assertEqual(len(result.data["cleaned_doc_ids"]), doc_count)


class PerDocModeRegressionTests(unittest.TestCase):
    def test_per_doc_mode_behavior_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_run_store(Path(tmp), doc_count=3)
            llm = FakeLocalLLM()
            tool = make_tool(store, llm)

            result = tool.run(doc_ids=["1", "2", "3"])

            self.assertTrue(result.success)
            self.assertEqual(llm.ask_calls, 3)
            self.assertEqual(sorted(result.data["cleaned_doc_ids"]), ["1", "2", "3"])
            # per_doc mode actually strips the flagged boilerplate paragraph.
            clean = (store.paths.clean_md_dir / "1.md").read_text(encoding="utf-8")
            self.assertNotIn("Navigation menu", clean)
            self.assertIn("Body paragraph about topic 1", clean)
            # doc_<id>.md carries the cleanup-pass metadata.
            doc_md = store.paths.summary_path_for(1).read_text(encoding="utf-8")
            self.assertIn("Local cleanup summary.", doc_md)
            self.assertIn("local point one", doc_md)


class HtmlBodyExtractorTests(unittest.TestCase):
    def test_drops_chrome_keeps_article_body(self) -> None:
        from services.document_cleanup_tool_funcs import extract_main_text

        out = extract_main_text(_ARTICLE_HTML)
        self.assertIn("Quantum Widgets Overview", out)
        self.assertIn("42 percent efficiency", out)
        self.assertNotIn("Home Products", out)
        self.assertNotIn("Related Articles", out)
        self.assertNotIn("Copyright 2026", out)

    def test_empty_html_returns_empty(self) -> None:
        from services.document_cleanup_tool_funcs import extract_main_text

        self.assertEqual(extract_main_text(""), "")
        self.assertEqual(extract_main_text("   "), "")

    def test_preserves_substantial_list_items_and_table_rows(self) -> None:
        from services.document_cleanup_tool_funcs import extract_main_text

        html = (
            "<main>"
            "<ul>"
            "<li>첫 번째 항목은 실제 본문 근거를 담은 충분히 긴 내용으로 작성되어 있습니다 여기에 더 많은 단어</li>"
            "<li>두 번째 항목 역시 구체적인 데이터 포인트를 자세히 설명하는 충분히 긴 본문 항목입니다 추가 단어</li>"
            "</ul>"
            "<table>"
            "<tr><th>지역</th><th>점유율</th><th>전년 대비</th></tr>"
            "<tr><td>아시아 태평양 시장 전체</td><td>사십이 퍼센트 수준</td><td>상승 추세 지속</td></tr>"
            "<tr><td>북미 시장 합산 비중</td><td>이십팔 퍼센트 안팎</td><td>보합세 유지함</td></tr>"
            "</table>"
            "</main>"
        )
        out = extract_main_text(html)
        self.assertIn("첫 번째 항목은 실제 본문", out)
        self.assertIn("| 지역 | 점유율 | 전년 대비 |", out)
        self.assertIn("아시아 태평양 시장 전체", out)

    def test_accepts_article_below_half_retention(self) -> None:
        # The real article is a small slice of a chrome-heavy page; the
        # quality gate accepts it regardless of how small vs the raw page.
        from services.document_cleanup_tool_funcs import extract_main_text_with_stats

        r = extract_main_text_with_stats(_CHROME_HEAVY_ARTICLE_HTML)
        self.assertTrue(r.accepted, r.reason)
        self.assertIn("삼성전자 반도체 부문", r.text)
        self.assertNotIn("메뉴0", r.text)       # nav dropped
        self.assertNotIn("관련 기사 링크", r.text)  # trailing related cluster trimmed
        self.assertNotIn("푸터0", r.text)        # footer dropped

    def test_rejects_navigation_only_page(self) -> None:
        from services.document_cleanup_tool_funcs import extract_main_text_with_stats

        r = extract_main_text_with_stats(_NAV_ONLY_HTML)
        self.assertFalse(r.accepted)
        self.assertIn(r.reason, ("empty", "low_quality", "too_short"))

    def test_picks_body_over_leading_related_article(self) -> None:
        from services.document_cleanup_tool_funcs import extract_main_text

        out = extract_main_text(_RELATED_FIRST_HTML)
        self.assertIn("삼성전자 반도체 부문", out)  # the real <main> body
        self.assertNotIn("관련 링크", out)          # the leading related <article> box

    def test_table_heavy_source_accepted_despite_low_prose(self) -> None:
        from services.document_cleanup_tool_funcs import extract_main_text_with_stats

        r = extract_main_text_with_stats(_TABLE_HEAVY_HTML)
        self.assertTrue(r.accepted, r.reason)
        self.assertGreaterEqual(r.table_count, 1)
        self.assertIn("| 회사 | Q1 2025 | Q2 2025 | Q3 2025 | Q4 2025 | 전년 대비 증감 |", r.text)
        self.assertIn("| 삼성전자 디바이스솔루션 |", r.text)
        self.assertNotIn("관련 기사 링크", r.text)


if __name__ == "__main__":
    unittest.main()
