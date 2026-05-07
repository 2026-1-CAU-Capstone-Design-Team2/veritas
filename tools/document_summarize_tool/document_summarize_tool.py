from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.prompts import DOC_SUMMARY_PROMPT, BATCH_SUMMARY_PROMPT
from tools.tool import BaseTool, ToolResult


class DocumentSummarizeTool(BaseTool):
    def __init__(
        self,
        schema: dict[str, Any],
        llm,
        run_store_service,
        batch_size: int = 5,
        max_context: int = 16384,
        json_retries: int = 2,
    ) -> None:
        super().__init__(schema=schema)
        self._llm = llm
        self._run_store_service = run_store_service
        self._batch_size = batch_size
        self._max_context = max_context
        self._json_retries = max(0, int(json_retries))

    @property
    def name(self) -> str:
        return "document_summarize"

    def run(
        self,
        overwrite: bool = False,
        doc_ids: list[str] | None = None,
        rebuild_batches: bool = True,
    ) -> ToolResult:
        try:
            target_doc_ids = {
                str(doc_id).strip()
                for doc_id in (doc_ids or [])
                if str(doc_id).strip()
            }
            has_cycle_scope = doc_ids is not None

            if has_cycle_scope and not target_doc_ids:
                print("[summarize][skip:no-new-docs] no doc ids provided for this cycle")
                return ToolResult(
                    success=True,
                    content="No cycle documents to summarize.",
                    data={
                        "summarized_doc_ids": [],
                        "skipped_existing_doc_ids": [],
                        "skipped_invalid_doc_ids": [],
                        "skipped_duplicate_doc_ids": [],
                        "skipped_not_in_cycle_doc_ids": [],
                        "failed_doc_ids": [],
                        "batch_result": {"batch_files": [], "count": 0},
                    },
                )

            kept_records = self._run_store_service.list_non_duplicate_records()
            skipped_duplicates = self._run_store_service.list_duplicate_records()

            invalid_empty = [
                r for r in kept_records
                if self._run_store_service.is_invalid_document_record(r)
            ]
            valid_records = [
                r for r in kept_records
                if not self._run_store_service.is_invalid_document_record(r)
            ]

            summarized_doc_ids: list[str] = []
            skipped_existing_doc_ids: list[str] = []
            skipped_invalid_doc_ids = [r.doc_id for r in invalid_empty]
            skipped_duplicate_doc_ids = [r.doc_id for r in skipped_duplicates]
            skipped_not_in_cycle_doc_ids: list[str] = []
            failed_doc_ids: list[str] = []

            for record in valid_records:
                if has_cycle_scope and record.doc_id not in target_doc_ids:
                    skipped_not_in_cycle_doc_ids.append(record.doc_id)
                    continue

                summary_path = Path(record.summary_path)
                if summary_path.exists() and summary_path.stat().st_size > 0 and not overwrite:
                    print(f"[summarize][skip:existing] doc_id={record.doc_id}")
                    skipped_existing_doc_ids.append(record.doc_id)
                    continue

                text = self._run_store_service.read_text_file(record.text_path)
                print(f"[summarize][new] doc_id={record.doc_id}")

                try:
                    summary_payload = self._llm.ask_json(
                        DOC_SUMMARY_PROMPT,
                        json.dumps(
                            {
                                "title_hint": record.title,
                                "url": record.url,
                                "final_url": record.final_url,
                                "domain": record.domain,
                                "title": record.title,
                                "text": text[: self._max_context],
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        reasoning=False,
                        max_retries=self._json_retries,
                        stream=getattr(self._llm, "stream_summary", False),
                        stream_label=f"summary:{record.doc_id}",
                    )
                except Exception as e:
                    print(f"[summarize][failed] doc_id={record.doc_id} reason={e}")
                    failed_doc_ids.append(record.doc_id)
                    continue

                summary_md = self._render_doc_summary_from_record(record, summary_payload)
                self._run_store_service.write_document_summary(record, summary_md)
                summarized_doc_ids.append(record.doc_id)

            if skipped_not_in_cycle_doc_ids:
                print(
                    "[summarize][skip:not-in-cycle] "
                    f"count={len(skipped_not_in_cycle_doc_ids)}"
                )

            batch_result = {"batch_files": [], "count": 0}
            if rebuild_batches:
                if has_cycle_scope:
                    batch_result = self._write_cycle_batch_summaries(target_doc_ids)
                else:
                    batch_result = self._rebuild_batch_summaries()

            return ToolResult(
                success=True,
                content=f"Summarized {len(summarized_doc_ids)} documents.",
                data={
                    "summarized_doc_ids": summarized_doc_ids,
                    "skipped_existing_doc_ids": skipped_existing_doc_ids,
                    "skipped_invalid_doc_ids": skipped_invalid_doc_ids,
                    "skipped_duplicate_doc_ids": skipped_duplicate_doc_ids,
                    "skipped_not_in_cycle_doc_ids": skipped_not_in_cycle_doc_ids,
                    "failed_doc_ids": failed_doc_ids,
                    "batch_result": batch_result,
                },
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to summarize documents: {e}")

    def _render_doc_summary_from_record(self, record, payload: dict[str, Any]) -> str:
        lines = [
            f"# Document {record.doc_id}",
            "",
            f"- Title: {payload.get('title') or record.title or 'Untitled'}",
            f"- URL: {record.url}",
            f"- Final URL: {record.final_url}",
            f"- Domain: {record.domain}",
            f"- Search Query: {record.search_query}",
            f"- Source Type: {payload.get('source_type', '')}",
            "",
            "## Summary",
            payload.get("summary", ""),
            "",
            "## Key Points",
        ]
        for point in payload.get("key_points", []):
            lines.append(f"- {point}")

        lines.extend(["", "## Reliability Notes"])
        for note in payload.get("reliability_notes", []):
            lines.append(f"- {note}")

        lines.extend(["", "## Keywords"])
        for keyword in payload.get("keywords", []):
            lines.append(f"- {keyword}")

        return "\n".join(lines).strip() + "\n"

    def _rebuild_batch_summaries(self) -> dict[str, Any]:
        summarized_records = self._run_store_service.list_summarized_non_duplicate_records()
        summarized_records = sorted(summarized_records, key=lambda r: r.doc_id)

        self._run_store_service.clear_batch_summaries()

        if not summarized_records:
            return {"batch_files": [], "count": 0}

        batch_files: list[str] = []

        for start in range(0, len(summarized_records), self._batch_size):
            batch_records = summarized_records[start : start + self._batch_size]
            batch_number = (start // self._batch_size) + 1
            batch_path = self._run_store_service.get_batch_summary_path(batch_number)

            summaries = [
                self._run_store_service.read_text_file(record.summary_path)
                for record in batch_records
            ]
            batch_markdown = self._llm.ask(
                BATCH_SUMMARY_PROMPT,
                self._build_batch_prompt_input(summaries),
                reasoning=False,
                stream=getattr(self._llm, "stream_summary", False),
                stream_label=f"batch:{batch_number:03d}",
            )
            self._run_store_service.write_batch_summary(batch_number, batch_markdown)
            batch_files.append(str(batch_path))
            self._run_store_service.set_batch_counter_from_count(batch_number)

        return {"batch_files": batch_files, "count": len(batch_files)}

    def _write_cycle_batch_summaries(self, cycle_doc_ids: set[str]) -> dict[str, Any]:
        if not cycle_doc_ids:
            return {"batch_files": [], "count": 0}

        summarized_records = self._run_store_service.list_summarized_non_duplicate_records()
        summarized_records = sorted(summarized_records, key=lambda r: r.doc_id)
        cycle_records = [record for record in summarized_records if record.doc_id in cycle_doc_ids]

        if not cycle_records:
            return {"batch_files": [], "count": 0}

        batch_files: list[str] = []
        next_batch_number = int(self._run_store_service.batch_counter or 0)

        for start in range(0, len(cycle_records), self._batch_size):
            batch_records = cycle_records[start : start + self._batch_size]
            next_batch_number += 1
            batch_path = self._run_store_service.get_batch_summary_path(next_batch_number)

            summaries = [
                self._run_store_service.read_text_file(record.summary_path)
                for record in batch_records
            ]
            batch_markdown = self._llm.ask(
                BATCH_SUMMARY_PROMPT,
                self._build_batch_prompt_input(summaries),
                reasoning=False,
                stream=getattr(self._llm, "stream_summary", False),
                stream_label=f"batch:{next_batch_number:03d}",
            )
            self._run_store_service.write_batch_summary(next_batch_number, batch_markdown)
            self._run_store_service.set_batch_counter_from_count(next_batch_number)
            batch_files.append(str(batch_path))

        return {"batch_files": batch_files, "count": len(batch_files)}

    def _build_batch_prompt_input(self, summaries: list[str]) -> str:
        try:
            user_request = self._run_store_service.load_request()
        except Exception:
            user_request = ""

        sections = [
            "Original User Request:",
            user_request or "(missing)",
            "",
            "Document Summaries:",
            "\n\n---\n\n".join(summaries),
        ]
        return "\n".join(sections).strip() + "\n"