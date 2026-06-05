from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable

from core.latex_cleanup import clean_latex_in_markdown
from core.prompts import (
    BATCH_SUMMARY_PROMPT,
    DOC_CHUNK_NOTES_PROMPT,
    DOC_SUMMARY_PROMPT,
    DOC_SUMMARY_REDUCE_PROMPT,
)
from services.citation_evidence import build_evidence_atoms
from tools.tool import BaseTool, ToolResult


class DocumentSummarizeTool(BaseTool):
    # Safety bound on the number of chunks for the long-document map-reduce
    # path. With a context-sized single-pass budget this is essentially never
    # reached; it only guards against pathologically large inputs.
    _MAX_DOC_CHUNKS = 16

    # The single-pass budget is derived from the llama-server context window.
    # Reserve tokens for the system prompt, JSON wrapper, and answer, then use
    # a conservative mixed Korean/English chars-per-token estimate. Do not let
    # the legacy max_context floor override a smaller real n_ctx: that can make
    # llama-server reject the request with exceed_context_size_error.
    _SAFE_CHARS_PER_TOKEN = 1.0
    _PROMPT_TOKEN_RESERVE = 1536
    _CONTEXT_TOKEN_HEADROOM = 256
    _TOKENIZE_TIMEOUT_SEC = 0.75
    _MAX_SINGLE_PASS_CHARS = 200000

    # Cap on the number of batch summary files a full rebuild produces. The
    # final report tool reads every batch file and synthesises ``final.md``;
    # too many batch files means the final step over-compresses concrete
    # detail away. Keeping the count modest lets the final step retain more
    # of the per-doc signal that the prompt fidelity rules ask for. A run
    # with fewer documents than the cap simply uses the configured batch
    # size; only large corpora trigger the dynamic up-sizing.
    _MAX_REBUILD_BATCHES = 6

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
        progress_callback: Callable[..., None] | None = None,
    ) -> ToolResult:
        """Summarize collected documents.

        ``summarize_docs`` controls per-document summaries (``summary/doc_*.md``);
        ``rebuild_batches`` controls batch summaries. Both read the same source —
        each document's clean Markdown (``clean_md/<doc_id>.md``) — so per-doc and
        batch summary are independent consumers of clean_md, not a chain.

        ``progress_callback`` (optional) is invoked once as the per-document loop
        starts each document (``"doc_start"``) and once as it finishes
        (``"doc_summarized"`` / ``"doc_failed"``). The per-document loop is the
        long tail of a survey — one LLM call per document — so streaming an event
        per document lets callers advance progress and activate source cards
        one-by-one instead of in a single batch after the whole loop.
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

            # Per-document summaries are independent (each reads its own
            # clean_md and writes its own summary/doc_*.md), so the loop fans
            # out through ``LLMClient.map_parallel``. ``max_parallel == 1``
            # preserves the original sequential behavior; a higher value
            # summarizes documents concurrently against llama-server's parallel
            # slots. A long document still takes the serial map-reduce path
            # *inside* its worker, so there is never more than one in-flight
            # request per document and the chunk loop cannot oversubscribe the
            # slots. Outcomes are returned in input order.
            outcomes = self._llm.map_parallel(
                records_to_summarize,
                lambda record: self._summarize_one_record(
                    record,
                    single_pass_budget=single_pass_budget,
                    target_doc_ids=target_doc_ids,
                    has_cycle_scope=has_cycle_scope,
                    overwrite=overwrite,
                    progress_callback=progress_callback,
                ),
                label="summary",
            )
            for outcome in outcomes:
                if "summarized" in outcome:
                    summarized_doc_ids.append(outcome["summarized"])
                elif "skipped_existing" in outcome:
                    skipped_existing_doc_ids.append(outcome["skipped_existing"])
                elif "skipped_not_in_cycle" in outcome:
                    skipped_not_in_cycle_doc_ids.append(outcome["skipped_not_in_cycle"])
                elif "failed_doc_id" in outcome:
                    failed_doc_ids.append(outcome["failed_doc_id"])
                    failed_documents.append(outcome["failed"])

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

    def _summarize_one_record(
        self,
        record,
        *,
        single_pass_budget: int,
        target_doc_ids: set[str],
        has_cycle_scope: bool,
        overwrite: bool,
        progress_callback: Callable[..., None] | None,
    ) -> dict[str, Any]:
        """Summarize one document and return a structured outcome.

        Self-contained (reads clean_md, runs the LLM, writes
        ``summary/doc_*.md``, emits progress) and **never raises** — failures
        are captured into the outcome dict — so it is safe to fan out across
        worker threads via :meth:`LLMClient.map_parallel`. Returns exactly one
        of: ``{"skipped_not_in_cycle": id}`` / ``{"skipped_existing": id}`` /
        ``{"summarized": id}`` / ``{"failed_doc_id": id, "failed": {...}}``.
        """
        if has_cycle_scope and record.doc_id not in target_doc_ids:
            return {"skipped_not_in_cycle": record.doc_id}

        summary_path = Path(record.summary_path)
        if summary_path.exists() and summary_path.stat().st_size > 0 and not overwrite:
            print(f"[summarize][skip:existing] doc_id={record.doc_id}")
            return {"skipped_existing": record.doc_id}

        text = self._run_store_service.read_text_file(record.text_path)
        print(f"[summarize][new] doc_id={record.doc_id}")
        self._notify_progress(
            progress_callback,
            "doc_start",
            doc_id=record.doc_id,
            title=record.title or record.doc_id,
        )

        try:
            summary_payload = self._summarize_document(record, text, single_pass_budget)
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            print(f"[summarize][failed] doc_id={record.doc_id} reason={reason}")
            self._notify_progress(
                progress_callback,
                "doc_failed",
                doc_id=record.doc_id,
                title=record.title or record.doc_id,
                reason=reason[:300],
            )
            return {
                "failed_doc_id": record.doc_id,
                "failed": {
                    "docId": record.doc_id,
                    "title": record.title or record.doc_id,
                    "reason": reason[:300],
                },
            }

        summary_md = self._render_doc_summary_from_record(record, summary_payload)
        self._run_store_service.write_document_summary(record, summary_md)
        self._persist_citation_evidence(record, summary_payload)
        self._notify_progress(
            progress_callback,
            "doc_summarized",
            doc_id=record.doc_id,
            title=record.title or record.doc_id,
        )
        return {"summarized": record.doc_id}

    def _persist_citation_evidence(self, record, summary_payload: dict[str, Any]) -> None:
        """Verify the summary's evidence quotes against clean_md and persist atoms.

        Best-effort and side-channel: the localized claim + verbatim quote the
        summary already emitted are anchored to a real source sentence in
        ``clean_md`` (the same source the citation popup reads) and only verified
        atoms are kept. A failure here must never fail summarization, so every
        exception is swallowed. No extra LLM call.
        """
        try:
            source_text = self._read_clean_source(record)
            atoms = build_evidence_atoms(record.doc_id, summary_payload, source_text)
            self._run_store_service.write_citation_evidence(record.doc_id, atoms)
        except Exception as e:  # noqa: BLE001 — evidence is an optional anchor
            print(f"[summarize][evidence-skip] doc_id={record.doc_id} reason={e}")

    def _read_clean_source(self, record) -> str:
        """Post-cleanup body for evidence verification, with a raw fallback.

        Mirrors the batch reader: prefer ``clean_md/<id>.md`` (what the popup
        anchors against), falling back to the pre-cleanup ``text_path`` when a
        workspace has not run cleanup yet.
        """
        clean_path = self._run_store_service.clean_md_dir / f"{record.doc_id}.md"
        if clean_path.exists():
            return self._run_store_service.read_text_file(str(clean_path)) or ""
        return self._run_store_service.read_text_file(record.text_path) or ""

    def _notify_progress(
        self,
        callback: Callable[..., None] | None,
        kind: str,
        **info: Any,
    ) -> None:
        """Best-effort per-document progress notification.

        A progress callback is purely a UX side-channel: a failure here must
        never abort summarization, so every exception is swallowed.
        """
        if callback is None:
            return
        try:
            callback(kind, **info)
        except Exception:
            pass

    def _single_pass_budget(self) -> int:
        """Character budget for summarizing a document in a single LLM call.

        Derived from the llama-server context window when available, so the
        map-reduce path only triggers for documents that genuinely cannot fit
        the model context — not for ordinary long articles. Falls back to the
        configured ``max_context`` when the window size is unknown.
        """
        n_ctx = getattr(self._llm, "n_ctx", 0) or 0
        if n_ctx > 0:
            usable_tokens = max(1024, int(n_ctx) - self._PROMPT_TOKEN_RESERVE)
            budget = int(usable_tokens * self._SAFE_CHARS_PER_TOKEN)
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
            try:
                return self._summarize_single_pass(record, text, budget)
            except Exception as e:
                if not self._is_context_overflow_error(e):
                    raise
                retry_budget = max(1000, budget // 2)
                print(
                    f"[summarize][context-retry] doc_id={record.doc_id} "
                    f"budget={budget} retry_budget={retry_budget} reason={e}"
                )
                return self._summarize_map_reduce(record, text, retry_budget)
        return self._summarize_map_reduce(record, text, budget)

    def _summarize_single_pass(self, record, text: str, budget: int) -> dict[str, Any]:
        def build_prompt(body: str) -> str:
            return json.dumps(
                {
                    "title_hint": record.title,
                    "url": record.url,
                    "final_url": record.final_url,
                    "domain": record.domain,
                    "title": record.title,
                    "text": body,
                },
                ensure_ascii=False,
                indent=2,
            )

        user_prompt = self._fit_text_to_context(
            DOC_SUMMARY_PROMPT,
            text,
            build_prompt,
            max_chars=budget,
        )
        return self._llm.ask_json(
            DOC_SUMMARY_PROMPT,
            user_prompt,
            reasoning=False,
            max_retries=self._json_retries,
            stream=getattr(self._llm, "stream_summary", False),
            stream_label=f"summary:{record.doc_id}",
            # Summaries are synthesis work — keep API reasoning models at
            # their default (medium) effort (no-op for the local client).
            reasoning_effort="medium",
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
            def build_chunk_prompt(body: str) -> str:
                return json.dumps(
                    {
                        "title": record.title,
                        "url": record.url,
                        "domain": record.domain,
                        "chunk_index": index,
                        "chunk_total": len(chunks),
                        "text": body,
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            chunk_input = self._fit_text_to_context(
                DOC_CHUNK_NOTES_PROMPT,
                chunk,
                build_chunk_prompt,
                max_chars=budget,
            )
            note = self._llm.ask(
                DOC_CHUNK_NOTES_PROMPT,
                chunk_input,
                reasoning=False,
                stream=getattr(self._llm, "stream_summary", False),
                stream_label=f"summary:{record.doc_id}:chunk{index}/{len(chunks)}",
                reasoning_effort="medium",
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

        def build_reduce_prompt(notes_body: str) -> str:
            return json.dumps(
                {
                    "title_hint": record.title,
                    "url": record.url,
                    "final_url": record.final_url,
                    "domain": record.domain,
                    "title": record.title,
                    "chunk_count": len(chunks),
                    "notes": notes_body,
                },
                ensure_ascii=False,
                indent=2,
            )

        reduce_input = self._fit_text_to_context(
            DOC_SUMMARY_REDUCE_PROMPT,
            joined_notes,
            build_reduce_prompt,
            max_chars=budget,
        )
        try:
            return self._llm.ask_json(
                DOC_SUMMARY_REDUCE_PROMPT,
                reduce_input,
                reasoning=False,
                max_retries=self._json_retries,
                stream=getattr(self._llm, "stream_summary", False),
                stream_label=f"summary:{record.doc_id}:reduce",
                reasoning_effort="medium",
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
        """Read each batch document's *post-cleanup* clean Markdown, capped so
        the whole batch fits the model context.

        ``record.text_path`` points at the raw_md/ file Crawl4AI wrote — that
        was the only Markdown back when ``clean_md`` was the Crawl4AI output.
        Now the document_cleanup tool runs after fetch and writes the real
        cleaned body to ``clean_md/<doc_id>.md``, so the batch summary should
        prefer that path. Falling back to ``text_path`` lets a workspace that
        has not run cleanup yet still produce a (degraded) batch summary
        instead of failing the loop.

        Per-doc cap is redistributed: a flat ``budget // batch_size`` cap
        gives every doc the same room, even when one doc is 2 KB and the
        other is 80 KB. We instead share the budget — short docs free up
        room for longer docs in the same batch — so the LLM sees more of
        the actually-large documents within the same overall context.
        """
        budget = self._single_pass_budget()
        nominal_cap = max(2000, budget // max(1, len(batch_records)))
        documents_raw: list[str] = []
        for record in batch_records:
            clean_path = self._run_store_service.clean_md_dir / f"{record.doc_id}.md"
            if clean_path.exists():
                content = self._run_store_service.read_text_file(str(clean_path))
            else:
                content = self._run_store_service.read_text_file(record.text_path)
            documents_raw.append(content or "")
        return self._redistribute_caps(documents_raw, budget=budget, nominal_cap=nominal_cap)

    @staticmethod
    def _redistribute_caps(
        documents_raw: list[str],
        *,
        budget: int,
        nominal_cap: int,
    ) -> list[str]:
        """Re-share the batch budget so short docs free up room for long ones.

        Two-pass allocation:
        1. Every doc gets ``min(len(doc), nominal_cap)`` — exactly what the
           flat cap would have given it.
        2. The unused remainder (``budget - sum(allocations)``) is split
           proportionally across docs that were *clipped* in step 1, until
           either every clipped doc fits whole or the remainder is exhausted.

        This keeps the total batch input ≤ ``budget`` (so context limits
        still hold) but stops wasting cap on documents that didn't need it.
        """
        sizes = [len(doc) for doc in documents_raw]
        if not sizes:
            return []
        allocations = [min(size, nominal_cap) for size in sizes]
        used = sum(allocations)
        remainder = max(0, budget - used)

        # Distribute remainder to docs still clipped, weighted by how much
        # extra room each one *could* use. Cap loop iterations defensively.
        for _ in range(8):
            shortfalls = [
                max(0, sizes[i] - allocations[i]) for i in range(len(sizes))
            ]
            total_shortfall = sum(shortfalls)
            if total_shortfall == 0 or remainder <= 0:
                break
            for i, want in enumerate(shortfalls):
                if want == 0:
                    continue
                grant = min(want, remainder * want // total_shortfall)
                allocations[i] += grant
                remainder -= grant
                if remainder <= 0:
                    break

        return [doc[:cap] for doc, cap in zip(documents_raw, allocations)]

    def _effective_batch_size_for_rebuild(self, doc_count: int) -> int:
        """Pick the batch size for a *full* rebuild.

        For full rebuilds (``doc_ids=None``), we cap the number of batch
        files at ``_MAX_REBUILD_BATCHES`` so the downstream final report
        does not have to compress 8+ batch notes into one document. When
        the corpus is small enough that the configured batch size already
        produces fewer batches than the cap, we leave it alone — the cap
        only *grows* the batch size for large corpora.
        """
        if doc_count <= 0:
            return self._batch_size
        dynamic = max(1, math.ceil(doc_count / self._MAX_REBUILD_BATCHES))
        return max(self._batch_size, dynamic)

    def _rebuild_batch_summaries(self) -> dict[str, Any]:
        records = sorted(self._batch_candidate_records(), key=lambda r: r.doc_id)

        self._run_store_service.clear_batch_summaries()

        if not records:
            return {"batch_files": [], "count": 0}

        batch_files: list[str] = []
        effective_batch_size = self._effective_batch_size_for_rebuild(len(records))
        if effective_batch_size != self._batch_size:
            print(
                "[summarize][rebuild] "
                f"effective_batch_size={effective_batch_size} "
                f"(configured={self._batch_size}, docs={len(records)}, "
                f"max_batches={self._MAX_REBUILD_BATCHES})"
            )

        for start in range(0, len(records), effective_batch_size):
            batch_records = records[start : start + effective_batch_size]
            batch_number = (start // effective_batch_size) + 1
            batch_path = self._run_store_service.get_batch_summary_path(batch_number)

            documents = self._read_batch_documents(batch_records)
            batch_markdown = self._ask_batch_summary(
                batch_records,
                documents,
                reasoning=False,
                stream=getattr(self._llm, "stream_summary", False),
                stream_label=f"batch:{batch_number:03d}",
                # Batch summaries carry the gap analysis that drives the
                # replan loop — keep API reasoning models at medium effort.
                reasoning_effort="medium",
            )
            # Same LaTeX over-escape fix applied to the final report — batch
            # notes feed final.md, so math expressions inside repeated /
            # new findings must already be canonical before they propagate.
            batch_markdown = clean_latex_in_markdown(batch_markdown)
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
            batch_markdown = self._ask_batch_summary(
                batch_records,
                documents,
                reasoning=False,
                stream=getattr(self._llm, "stream_summary", False),
                stream_label=f"batch:{next_batch_number:03d}",
                # Batch summaries carry the gap analysis that drives the
                # replan loop — keep API reasoning models at medium effort.
                reasoning_effort="medium",
            )
            batch_markdown = clean_latex_in_markdown(batch_markdown)
            self._run_store_service.write_batch_summary(next_batch_number, batch_markdown)
            self._run_store_service.set_batch_counter_from_count(next_batch_number)
            batch_files.append(str(batch_path))

        return {"batch_files": batch_files, "count": len(batch_files)}

    def _ask_batch_summary(
        self,
        batch_records,
        documents: list[str],
        **kwargs: Any,
    ) -> str:
        stream_label = str(kwargs.get("stream_label") or "")
        prompt_input = self._fit_batch_prompt_input(batch_records, documents)
        try:
            return self._llm.ask(
                BATCH_SUMMARY_PROMPT,
                prompt_input,
                reasoning=False,
                stream=getattr(self._llm, "stream_summary", False),
                stream_label=stream_label,
                # Batch summaries carry the gap analysis that drives the
                # replan loop, so keep API reasoning models at medium effort.
                reasoning_effort="medium",
            )
        except Exception as e:
            if not self._is_context_overflow_error(e):
                raise

            total = sum(len(doc or "") for doc in documents)
            retry_budget = max(1000, total // 2)
            nominal_cap = max(1, retry_budget // max(1, len(documents)))
            retry_documents = self._redistribute_caps(
                [doc or "" for doc in documents],
                budget=retry_budget,
                nominal_cap=nominal_cap,
            )
            print(
                f"[summarize][batch-context-retry] label={stream_label} "
                f"chars={total} retry_chars={sum(len(doc) for doc in retry_documents)} "
                f"reason={e}"
            )
            return self._llm.ask(
                BATCH_SUMMARY_PROMPT,
                self._fit_batch_prompt_input(batch_records, retry_documents),
                reasoning=False,
                stream=getattr(self._llm, "stream_summary", False),
                stream_label=stream_label,
                reasoning_effort="medium",
            )

    def _build_batch_prompt_input(self, batch_records, documents: list[str]) -> str:
        """Render the batch summary user prompt.

        Each document is wrapped in a ``=== doc_<id> ===`` header so the LLM
        can cite findings back with ``[doc_<id>]`` markers (the verification
        layer parses those markers to map findings to source documents).
        Title / domain / URL are surfaced alongside the id so the model has
        enough context to attribute claims correctly even when the body is
        clipped.
        """
        try:
            user_request = self._run_store_service.load_request()
        except Exception:
            user_request = ""

        rendered_docs: list[str] = []
        for record, body in zip(batch_records, documents):
            doc_id = getattr(record, "doc_id", "") or ""
            title = getattr(record, "title", "") or ""
            domain = getattr(record, "domain", "") or ""
            url = getattr(record, "url", "") or ""
            header_lines = [f"=== doc_{doc_id} ==="]
            if title:
                header_lines.append(f"Title: {title}")
            if domain:
                header_lines.append(f"Domain: {domain}")
            if url:
                header_lines.append(f"URL: {url}")
            header = "\n".join(header_lines)
            rendered_docs.append(f"{header}\n\n{body or ''}".rstrip())

        sections = [
            "Original User Request:",
            user_request or "(missing)",
            "",
            "Document Contents (clean Markdown):",
            "\n\n---\n\n".join(rendered_docs),
        ]
        return "\n".join(sections).strip() + "\n"

    def _fit_batch_prompt_input(self, batch_records, documents: list[str]) -> str:
        prompt_input = self._build_batch_prompt_input(batch_records, documents)
        if self._prompt_fits_context(BATCH_SUMMARY_PROMPT, prompt_input):
            return prompt_input

        total = sum(len(doc or "") for doc in documents)
        if total <= 0:
            return prompt_input

        low = 0
        high = total
        best = self._build_batch_prompt_input(batch_records, ["" for _ in documents])
        best_chars = 0
        raw_docs = [doc or "" for doc in documents]
        while low <= high:
            mid = (low + high) // 2
            nominal_cap = max(1, mid // max(1, len(raw_docs)))
            candidate_docs = self._redistribute_caps(
                raw_docs,
                budget=mid,
                nominal_cap=nominal_cap,
            )
            candidate = self._build_batch_prompt_input(batch_records, candidate_docs)
            if self._prompt_fits_context(BATCH_SUMMARY_PROMPT, candidate):
                best = candidate
                best_chars = sum(len(doc) for doc in candidate_docs)
                low = mid + 1
            else:
                high = mid - 1

        print(f"[summarize][batch-fit] chars={total} fitted_chars={best_chars}")
        return best

    def _fit_text_to_context(
        self,
        system_prompt: str,
        text: str,
        build_user_prompt: Callable[[str], str],
        *,
        max_chars: int,
    ) -> str:
        text = text or ""
        max_chars = max(0, min(len(text), int(max_chars)))
        user_prompt = build_user_prompt(text[:max_chars])
        if self._prompt_fits_context(system_prompt, user_prompt):
            return user_prompt

        low = 0
        high = max_chars
        best = build_user_prompt("")
        while low <= high:
            mid = (low + high) // 2
            candidate = build_user_prompt(text[:mid])
            if self._prompt_fits_context(system_prompt, candidate):
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best

    def _prompt_fits_context(self, system_prompt: str, user_prompt: str) -> bool:
        n_ctx = getattr(self._llm, "n_ctx", 0) or 0
        if n_ctx <= 0:
            return True

        count_tokens = getattr(self._llm, "tokenize_count", None)
        if not callable(count_tokens):
            return True

        prompt = "\n".join(
            [
                system_prompt.strip(),
                "Return a strict JSON object only.",
                "/no_think",
                user_prompt,
            ]
        )
        try:
            token_count = count_tokens(prompt, timeout_sec=self._TOKENIZE_TIMEOUT_SEC)
        except TypeError:
            token_count = count_tokens(prompt)
        except Exception:
            return True

        if token_count is None:
            return True
        limit = max(1024, int(n_ctx) - self._CONTEXT_TOKEN_HEADROOM)
        return int(token_count) <= limit

    @staticmethod
    def _is_context_overflow_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            "exceed_context_size" in message
            or "exceeds the available context size" in message
            or ("n_prompt_tokens" in message and "n_ctx" in message)
        )
