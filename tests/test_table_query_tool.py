from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from services.local_corpus import (
    LocalCorpusService,
    ManifestRepository,
    TableQueryError,
    TableQueryService,
)
from tools.loader import TOOLS_DIR, load_schema
from tools.table_query_tool import TableQueryTool

try:
    import openpyxl  # noqa: F401

    HAS_OPENPYXL = True
except Exception:
    HAS_OPENPYXL = False


class FakeLLM:
    def embed(self, _text):
        return [0.0, 1.0]

    def embed_batch(self, texts):
        return [[0.0, float(index + 1)] for index, _ in enumerate(texts)]


class FakeVectorStore:
    def __init__(self):
        self.upserts: list[dict] = []
        self.deleted_where: list[dict] = []

    def add_documents(self, doc_ids, contents, embeddings=None, metadatas=None):
        self.upserts.append(
            {
                "doc_ids": list(doc_ids),
                "contents": list(contents),
                "embeddings": embeddings,
                "metadatas": list(metadatas or []),
            }
        )

    def delete_where(self, where):
        self.deleted_where.append(dict(where))

    def delete_documents(self, doc_ids):
        pass

    def query(self, **_kwargs):
        return []

    def get_all(self, where=None):
        return []


def write_sales_csv(path: Path, rows: int = 1000) -> int:
    """Write a CSV with `rows` data rows. Returns the exact total amount.

    Amounts use Korean formatting ("1,234원") to exercise numeric coercion.
    Even-indexed rows are 서울, odd-indexed rows are 부산.
    """
    total = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["month", "region", "amount"])
        for index in range(rows):
            amount = (index + 1) * 10
            total += amount
            writer.writerow(
                [
                    (index % 12) + 1,
                    "서울" if index % 2 == 0 else "부산",
                    f"{amount:,}원",
                ]
            )
    return total


def index_workspace(root: Path, input_dir: Path) -> TableQueryService:
    """Register `input_dir` as a local access folder for workspace ws1 and
    return a TableQueryService bound to that workspace root."""
    corpus = LocalCorpusService(
        output_root=root / "runs",
        llm=FakeLLM(),
        manifest_repository=ManifestRepository(root / "runs"),
        vector_store=FakeVectorStore(),
    )
    corpus.index_workspace_sources("ws1", [str(input_dir)])
    return TableQueryService(root / "runs" / "ws1")


class TableQueryServiceTests(unittest.TestCase):
    def test_aggregate_reads_all_rows_without_loss(self) -> None:
        # 1000 data rows — far beyond the 200-row cap of the indexed profile.
        # The profile (RAG path) is lossy by design; table_query must not be.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            expected_total = write_sales_csv(input_dir / "sales.csv", rows=1000)
            service = index_workspace(root, input_dir)

            result = service.query(
                "sales.csv",
                aggregate=[
                    {"column": "amount", "func": "sum"},
                    {"column": "*", "func": "count"},
                ],
            )

            self.assertEqual(result["total_rows"], 1000)
            self.assertEqual(result["matched_rows"], 1000)
            self.assertEqual(result["rows"][0]["sum(amount)"], float(expected_total))
            self.assertEqual(result["rows"][0]["count(*)"], 1000)

    def test_where_filter_with_numeric_coercion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            write_sales_csv(input_dir / "sales.csv", rows=1000)
            service = index_workspace(root, input_dir)

            # 서울 rows are even indices; amount = (index + 1) * 10 > 5000
            # → even index >= 500 → 500, 502, ..., 998 → 250 rows.
            result = service.query(
                "sales.csv",
                where=[
                    {"column": "region", "op": "==", "value": "서울"},
                    {"column": "amount", "op": ">", "value": "5000"},
                ],
                columns=["month", "amount"],
            )

            self.assertEqual(result["matched_rows"], 250)
            self.assertEqual(set(result["rows"][0].keys()), {"month", "amount"})

    def test_group_by_aggregate_and_sort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            write_sales_csv(input_dir / "sales.csv", rows=1000)
            service = index_workspace(root, input_dir)

            result = service.query(
                "sales.csv",
                group_by=["region"],
                aggregate=[
                    {"column": "amount", "func": "sum"},
                    {"column": "*", "func": "count"},
                ],
                sort_by="sum(amount)",
                descending=True,
            )

            # 서울 = even indices: 10 * (1 + 3 + ... + 999) = 10 * 500^2 = 2,500,000
            # 부산 = odd indices:  10 * (2 + 4 + ... + 1000) = 10 * 500 * 501 = 2,505,000
            self.assertEqual(len(result["rows"]), 2)
            self.assertEqual(result["rows"][0]["region"], "부산")
            self.assertEqual(result["rows"][0]["sum(amount)"], 2505000.0)
            self.assertEqual(result["rows"][0]["count(*)"], 500)
            self.assertEqual(result["rows"][1]["region"], "서울")
            self.assertEqual(result["rows"][1]["sum(amount)"], 2500000.0)

    def test_row_query_respects_limit_and_reports_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            write_sales_csv(input_dir / "sales.csv", rows=1000)
            service = index_workspace(root, input_dir)

            result = service.query("sales.csv", limit=10)

            self.assertEqual(result["returned_rows"], 10)
            self.assertEqual(len(result["rows"]), 10)
            self.assertTrue(result["truncated"])
            self.assertEqual(result["matched_rows"], 1000)

    def test_list_tables_and_describe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            write_sales_csv(input_dir / "sales.csv", rows=300)
            # Non-table files must not appear in list_tables.
            (input_dir / "notes.md").write_text("# notes", encoding="utf-8")
            service = index_workspace(root, input_dir)

            tables = service.list_tables()
            self.assertEqual(tables["table_count"], 1)
            self.assertEqual(tables["tables"][0]["file_name"], "sales.csv")
            self.assertEqual(tables["tables"][0]["columns"], ["month", "region", "amount"])

            described = service.describe("sales.csv")
            self.assertEqual(described["total_rows"], 300)
            names = [column["name"] for column in described["columns"]]
            self.assertEqual(names, ["month", "region", "amount"])
            amount_column = described["columns"][2]
            # "1,234원" values must still be recognized as numeric.
            self.assertEqual(amount_column["inferred_type"], "number")
            self.assertEqual(len(described["sample_rows"]), 5)

    def test_unknown_table_column_and_operator_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            write_sales_csv(input_dir / "sales.csv", rows=10)
            service = index_workspace(root, input_dir)

            with self.assertRaises(TableQueryError):
                service.query("missing.csv")
            with self.assertRaises(TableQueryError):
                service.query(
                    "sales.csv",
                    where=[{"column": "없는열", "op": "==", "value": "x"}],
                )
            with self.assertRaises(TableQueryError):
                service.query(
                    "sales.csv",
                    where=[{"column": "month", "op": "like", "value": "1"}],
                )
            with self.assertRaises(TableQueryError):
                service.query(
                    "sales.csv",
                    aggregate=[{"column": "amount", "func": "median"}],
                )

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl is required for .xlsx queries")
    def test_xlsx_multi_sheet_query_without_loss(self) -> None:
        import openpyxl

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()

            workbook = openpyxl.Workbook()
            first = workbook.active
            first.title = "1분기"
            first.append(["item", "qty"])
            for index in range(300):  # beyond the 200-row profiling cap
                first.append([f"item{index}", index + 1])
            second = workbook.create_sheet("2분기")
            second.append(["item", "qty"])
            second.append(["only", 99])
            workbook.save(input_dir / "inventory.xlsx")
            workbook.close()

            service = index_workspace(root, input_dir)

            result = service.query(
                "inventory.xlsx",
                sheet_name="1분기",
                aggregate=[{"column": "qty", "func": "sum"}],
            )
            self.assertEqual(result["total_rows"], 300)
            self.assertEqual(result["rows"][0]["sum(qty)"], float(sum(range(1, 301))))

            # Default sheet is the first one.
            default_sheet = service.describe("inventory.xlsx")
            self.assertEqual(default_sheet["sheet_name"], "1분기")

            other = service.query("inventory.xlsx", sheet_name="2분기")
            self.assertEqual(other["total_rows"], 1)
            self.assertEqual(other["rows"][0]["item"], "only")

            listed = service.list_tables()
            sheets = listed["tables"][0]["sheets"]
            self.assertEqual([sheet["sheet_name"] for sheet in sheets], ["1분기", "2분기"])

    def test_files_skipped_by_indexer_remain_queryable(self) -> None:
        # Querying reads the original file from the manifest; it must not
        # depend on the parse/index status (e.g. files too large to index).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            write_sales_csv(input_dir / "sales.csv", rows=20)
            service = index_workspace(root, input_dir)

            manifest_path = root / "runs" / "ws1" / "local" / "manifest.json"
            payload = manifest_path.read_text(encoding="utf-8").replace(
                '"indexed"', '"skipped_too_large"'
            )
            manifest_path.write_text(payload, encoding="utf-8")

            result = service.query(
                "sales.csv",
                aggregate=[{"column": "*", "func": "count"}],
            )
            self.assertEqual(result["rows"][0]["count(*)"], 20)


class TableQueryToolTests(unittest.TestCase):
    def _build_tool(self, root: Path, input_dir: Path, llm=None) -> TableQueryTool:
        service = index_workspace(root, input_dir)
        schema = load_schema(TOOLS_DIR / "table_query_tool" / "tool_schema.json")
        return TableQueryTool(schema=schema, table_query_service=service, llm=llm)

    def test_tool_runs_query_and_returns_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            expected_total = write_sales_csv(input_dir / "sales.csv", rows=500)
            tool = self._build_tool(root, input_dir)

            listed = tool.run(operation="list_tables")
            self.assertTrue(listed.success)
            self.assertEqual(listed.data["table_count"], 1)

            result = tool.run(
                operation="query",
                file_name="sales.csv",
                aggregate=[{"column": "amount", "func": "sum"}],
            )
            self.assertTrue(result.success)
            self.assertEqual(result.data["rows"][0]["sum(amount)"], float(expected_total))

    def test_tool_rejects_invalid_operation_and_unknown_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            write_sales_csv(input_dir / "sales.csv", rows=5)
            tool = self._build_tool(root, input_dir)

            invalid_op = tool.run(operation="drop_table")
            self.assertFalse(invalid_op.success)

            unknown_file = tool.run(operation="query", file_name="missing.csv")
            self.assertFalse(unknown_file.success)
            self.assertIn("Available", unknown_file.error or "")

    def test_tool_refuses_external_llm_consumer(self) -> None:
        # Same local-privacy contract as RAGService._ensure_local_generation_allowed:
        # local table contents must never flow into an OpenAI-backed pipeline.
        class OpenAIChatLLMClient:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            write_sales_csv(input_dir / "sales.csv", rows=5)
            tool = self._build_tool(root, input_dir, llm=OpenAIChatLLMClient())

            result = tool.run(operation="list_tables")
            self.assertFalse(result.success)
            self.assertIn("local LLM", result.error or "")

    def test_chat_agent_allowlist_exposes_table_query(self) -> None:
        from agent.chat_agent import ChatAgent

        self.assertIn("table_query", ChatAgent.DEFAULT_OPTIONAL_TOOL_NAMES)


if __name__ == "__main__":
    unittest.main()
