from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.prompts import (
    BATCH_SUMMARY_PROMPT,
    DOC_CHUNK_NOTES_PROMPT,
    DOC_SUMMARY_PROMPT,
    DOC_SUMMARY_REDUCE_PROMPT,
)
from tools.tool import BaseTool, ToolResult


class DocumentSummarizeTool(BaseTool):
    # Safety bound on the number of chunks for the long-document map-reduce
    # path. With a context-sized single-pass budget this is essentially never
    # reached; it only guards against pathologically large inputs.
    _MAX_DOC_CHUNKS = 16

    # The single-pass budget is derived from the llama-server context window:
    #   n_ctx (tokens) * _CHARS_PER_TOKEN * _INPUT_CONTEXT_FRACTION
    # The remainder of the window is reserved for the system prompt and the
    # generated summary. _CHARS_PER_TOKEN is deliberately conservative for
    # mixed Korean/English text. This keeps ordinary long articles on the fast
    # single-pass path; map-reduce only triggers for documents that genuinely
    # cannot fit the model context.
    _CHARS_PER_TOKEN = 2.5
    _INPUT_CONTEXT_FRACTION = 0.5
    _MAX_SINGLE_PASS_CHARS = 200000

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
        summarize_docs: bool = True,
    ) -> ToolResult:
        """Summarize collected documents.

        ``summarize_docs`` controls per-document summaries (``summary/doc_*.md``);
        ``rebuild_batches`` controls batch summaries. Both read the same source —
        each document's clean Markdown (``clean_md/<doc_id>.md``) — so per-doc and
        batch summary are independent consumers of clean_md, not a chain.
        """
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
                        "failed_documents": [],
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
            # Per-document failure detail (doc_id + reason) so the UI can show
            # exactly which documents could not be summarized and why, instead
            # of treating the whole run as a failure.
            failed_documents: list[dict[str, str]] = []

            single_pass_budget = self._single_pass_budget()
            records_to_summarize = valid_records if summarize_docs else []
            if records_to_summarize:
                print(
                    f"[summarize] single-pass budget={single_pass_budget} chars "
                    f"(n_ctx={getattr(self._llm, 'n_ctx', 'unknown')}); "
                    "documents above this size use chunked map-reduce"
                )

            for record in records_to_summarize:
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
                    summary_payload = self._summarize_document(
                        record, text, single_pass_budget
                    )
                except Exception as e:
                    reason = f"{type(e).__name__}: {e}"
                    print(f"[summarize][failed] doc_id={record.doc_id} reason={reason}")
                    failed_doc_ids.append(record.doc_id)
                    failed_documents.append(
                        {
                            "docId": record.doc_id,
                            "title": record.title or record.doc_id,
                            "reason": reason[:300],
                        }
                    )
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
                    "failed_documents": failed_documents,
                    "batch_result": batch_result,
                },
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to summarize documents: {e}")

    def _single_pass_budget(self) -> int:
        """Character budget for summarizing a document in a single LLM call.

        Derived from the llama-server context window when available, so the
        map-reduce path only triggers for documents that genuinely cannot fit
        the model context — not for ordinary long articles. Falls back to the
        configured ``max_context`` when the window size is unknown.
        """
        n_ctx = getattr(self._llm, "n_ctx", 0) or 0
        if n_ctx > 0:
            derived = int(n_ctx * self._CHARS_PER_TOKEN * self._INPUT_CONTEXT_FRACTION)
            budget = max(self._max_context, derived)
        else:
            budget = self._max_context
        return max(2000, min(budget, self._MAX_SINGLE_PASS_CHARS))

    def _summarize_document(self, record, text: str, budget: int) -> dict[str, Any]:
        """Produce a document-summary payload, choosing single-pass or map-reduce.

        Documents that fit within ``budget`` are summarized in one LLM call.
        Longer documents are chunked, each chunk is turned into compact notes,
        and the notes are reduced into one summary so that no part of an
        over-long document is silently truncated away.
        """
        text = text or ""
        if len(text) <= budget:
            return self._summarize_single_pass(record, text, budget)
        return self._summarize_map_reduce(record, text, budget)

    def _summarize_single_pass(self, record, text: str, budget: int) -> dict[str, Any]:
        return self._llm.ask_json(
            DOC_SUMMARY_PROMPT,
            json.dumps(
                {
                    "title_hint": record.title,
                    "url": record.url,
                    "final_url": record.final_url,
                    "domain": record.domain,
                    "title": record.title,
                    "text": text[:budget],
                },
                ensure_ascii=False,
                indent=2,
            ),
            reasoning=False,
            max_retries=self._json_retries,
            stream=getattr(self._llm, "stream_summary", False),
            stream_label=f"summary:{record.doc_id}",
        )

    def _summarize_map_reduce(self, record, text: str, budget: int) -> dict[str, Any]:
        overlap = max(200, budget // 10)
        chunks = self._chunk_text(text, budget, overlap)
        print(
            f"[summarize][map-reduce] doc_id={record.doc_id} "
            f"chunks={len(chunks)} chars={len(text)}"
        )

        notes: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_input = json.dumps(
                {
                    "title": record.title,
                    "url": record.url,
                    "domain": record.domain,
                    "chunk_index": index,
                    "chunk_total": len(chunks),
                    "text": chunk,
                },
                ensure_ascii=False,
                indent=2,
            )
            note = self._llm.ask(
                DOC_CHUNK_NOTES_PROMPT,
                chunk_input,
                reasoning=False,
                stream=getattr(self._llm, "stream_summary", False),
                stream_label=f"summary:{record.doc_id}:chunk{index}/{len(chunks)}",
            )
            note = (note or "").strip()
            if note and note.lower() != "(no substantive content)":
                notes.append(f"[Part {index}/{len(chunks)}]\n{note}")

        if not notes:
            raise RuntimeError("map-reduce produced no usable chunk notes")

        joined_notes = "\n\n".join(notes)
        # Notes are already compressed; if they still exceed the budget for a
        # pathologically long document, truncate here. This is far less harmful
        # than truncating raw body text would be.
        if len(joined_notes) > budget:
            joined_notes = joined_notes[:budget]

        reduce_input = json.dumps(
            {
                "title_hint": record.title,
                "url": record.url,
                "final_url": record.final_url,
                "domain": record.domain,
                "title": record.title,
                "chunk_count": len(chunks),
                "notes": joined_notes,
            },
            ensure_ascii=False,
            indent=2,
        )
        try:
            return self._llm.ask_json(
                DOC_SUMMARY_REDUCE_PROMPT,
                reduce_input,
                reasoning=False,
                max_retries=self._json_retries,
                stream=getattr(self._llm, "stream_summary", False),
                stream_label=f"summary:{record.doc_id}:reduce",
            )
        except Exception as e:
            # The reduce JSON failed even on compact notes. Rather than losing a
            # successfully-read long document, assemble a payload directly from
            # the per-chunk notes.
            print(f"[summarize][reduce-fallback] doc_id={record.doc_id} reason={e}")
            return self._payload_from_notes(record, notes)

    def _chunk_text(self, text: str, size: int, overlap: int) -> list[str]:
        """Split text into overlapping chunks, preferring clean text boundaries."""
        text = text or ""
        if len(text) <= size:
            return [text] if text.strip() else []

        size = max(size, 1000)
        overlap = min(max(overlap, 0), size // 2)

        chunks: list[str] = []
        n = len(text)
        start = 0
        last_end = 0
        while start < n and len(chunks) < self._MAX_DOC_CHUNKS:
            hard_end = min(start + size, n)
            end = hard_end
            if hard_end < n:
                # Prefer to break on a paragraph/line/sentence boundary within
                # the last fifth of the window so chunks are not cut mid-thought.
                window_start = start + (size * 4) // 5
                cut = max(
                    text.rfind("\n\n", window_start, hard_end),
                    text.rfind("\n", window_start, hard_end),
                    text.rfind(". ", window_start, hard_end),
                )
                if cut > start:
                    end = cut
            chunks.append(text[start:end])
            last_end = end
            if end >= n:
                break
            start = end - overlap

        # Safety net: if the chunk cap was reached before covering the whole
        # document, fold the remaining tail into a final chunk so nothing is lost.
        if last_end < n:
            chunks.append(text[last_end:])

        return [chunk.strip() for chunk in chunks if chunk.strip()]

    def _payload_from_notes(self, record, notes: list[str]) -> dict[str, Any]:
        """Build a doc-summary payload from chunk notes when reduce-JSON fails."""
        note_lines: list[str] = []
        for block in notes:
            for line in block.splitlines():
                stripped = line.strip().lstrip("-*•").strip()
                if stripped and not stripped.startswith("[Part "):
                    note_lines.append(stripped)

        return {
            "title": record.title or "Untitled",
            "source_type": "",
            "summary": " ".join(note_lines[:5]),
            "key_points": note_lines[:8],
            "reliability_notes": [
                "Auto-assembled from per-chunk notes; reduce-step JSON synthesis failed.",
            ],
            "keywords": [],
        }

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

    def _batch_candidate_records(self):
        """Non-duplicate records that have usable clean Markdown on disk."""
        return [
            record
            for record in self._run_store_service.list_non_duplicate_records()
            if not self._run_store_service.is_invalid_document_record(record)
        ]

    def _read_batch_documents(self, batch_records) -> list[str]:
        """Read each batch document's clean Markdown, capped so the whole batch
        fits the model context.

        The batch summary is built directly from clean_md (not from per-document
        summaries), so the per-document slice is the context budget divided
        across the batch.
        """
        per_doc_cap = max(
            2000, self._single_pass_budget() // max(1, self._batch_size)
        )
        documents: list[str] = []
        for record in batch_records:
            content = self._run_store_service.read_text_file(record.text_path)
            documents.append((content or "")[:per_doc_cap])
        return documents

    def _rebuild_batch_summaries(self) -> dict[str, Any]:
        records = sorted(self._batch_candidate_records(), key=lambda r: r.doc_id)

        self._run_store_service.clear_batch_summaries()

        if not records:
            return {"batch_files": [], "count": 0}

        batch_files: list[str] = []

        for start in range(0, len(records), self._batch_size):
            batch_records = records[start : start + self._batch_size]
            batch_number = (start // self._batch_size) + 1
            batch_path = self._run_store_service.get_batch_summary_path(batch_number)

            documents = self._read_batch_documents(batch_records)
            batch_markdown = self._llm.ask(
                BATCH_SUMMARY_PROMPT,
                self._build_batch_prompt_input(documents),
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

        cycle_records = sorted(
            (
                record
                for record in self._batch_candidate_records()
                if record.doc_id in cycle_doc_ids
            ),
            key=lambda r: r.doc_id,
        )

        if not cycle_records:
            return {"batch_files": [], "count": 0}

        batch_files: list[str] = []
        next_batch_number = int(self._run_store_service.batch_counter or 0)

        for start in range(0, len(cycle_records), self._batch_size):
            batch_records = cycle_records[start : start + self._batch_size]
            next_batch_number += 1
            batch_path = self._run_store_service.get_batch_summary_path(next_batch_number)

            documents = self._read_batch_documents(batch_records)
            batch_markdown = self._llm.ask(
                BATCH_SUMMARY_PROMPT,
                self._build_batch_prompt_input(documents),
                reasoning=False,
                stream=getattr(self._llm, "stream_summary", False),
                stream_label=f"batch:{next_batch_number:03d}",
            )
            self._run_store_service.write_batch_summary(next_batch_number, batch_markdown)
            self._run_store_service.set_batch_counter_from_count(next_batch_number)
            batch_files.append(str(batch_path))

        return {"batch_files": batch_files, "count": len(batch_files)}

    def _build_batch_prompt_input(self, documents: list[str]) -> str:
        try:
            user_request = self._run_store_service.load_request()
        except Exception:
            user_request = ""

        sections = [
            "Original User Request:",
            user_request or "(missing)",
            "",
            "Document Contents (clean Markdown):",
            "\n\n---\n\n".join(documents),
        ]
        return "\n".join(sections).strip() + "\n"