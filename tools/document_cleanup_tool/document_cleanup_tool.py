"""Per-document cleanup: identify boilerplate paragraphs + extract keywords/key-points.

Replaces the previous per-doc summarize pass. For each kept document:

1. Read ``raw_md/<doc_id>.md`` (Crawl4AI's pre-cleanup output).
2. Split into paragraphs and prefix each with ``[P0]``, ``[P1]`` … so the LLM
   can reference them by index in its response.
3. Call ``LLMClient.ask`` *once* with :data:`core.prompts.DOCUMENT_CLEANUP_PROMPT`.
   The LLM returns a plain-text three-section response
   (``BOILERPLATE_PARAGRAPHS`` / ``KEYWORDS`` / ``KEY_POINTS``) — *not* JSON.
   The body of many non-English documents is full of quotes / commas that
   break JSON escaping; plain-text sections sidestep that entirely, and the
   :mod:`response_parser` is tolerant enough that partial / missing sections
   degrade gracefully instead of failing the whole document.
4. Drop the flagged paragraphs and write the result to
   ``clean_md/<doc_id>.md`` (the *real* clean source that downstream
   consumers read).
5. Write ``summary/doc_<doc_id>.md`` directly from the workspace index +
   the cleanup output (no second LLM call).

The tool emits per-document progress so the API ring buffer drives the
research progress bar exactly the way it did for the old summarize loop.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from core.prompts import DOCUMENT_CLEANUP_PROMPT
from services.document_cleanup_tool_funcs import (
    annotate_paragraphs,
    apply_boilerplate_removal,
    parse_cleanup_response,
    split_paragraphs,
    write_doc_metadata,
)
from tools.tool import BaseTool, ToolResult


_PROGRESS_KIND_START = "doc_cleanup_started"
_PROGRESS_KIND_DONE = "doc_cleanup_done"
_PROGRESS_KIND_FAILED = "doc_cleanup_failed"


class DocumentCleanupTool(BaseTool):
    """LLM-driven raw_md → clean_md cleanup + doc metadata writer."""

    def __init__(
        self,
        schema: dict[str, Any],
        llm=None,
        run_store_service=None,
    ) -> None:
        super().__init__(schema=schema)
        self._llm = llm
        self._run_store = run_store_service

    @property
    def name(self) -> str:
        return "document_cleanup"

    def run(
        self,
        doc_ids: list[str] | None = None,
        overwrite: bool = False,
        progress_callback: Callable[..., None] | None = None,
    ) -> ToolResult:
        if self._llm is None:
            return ToolResult(
                success=False,
                error="document_cleanup requires an LLM client.",
            )
        if self._run_store is None:
            return ToolResult(
                success=False,
                error="document_cleanup requires a RunStoreService.",
            )

        records = self._kept_records()
        if doc_ids is not None:
            target_set = {str(d).strip() for d in doc_ids if str(d).strip()}
            if not target_set:
                return ToolResult(success=True, data=self._empty_summary())
            records = [r for r in records if str(r.get("doc_id") or "") in target_set]

        cleaned_doc_ids: list[str] = []
        skipped_existing: list[str] = []
        failed_documents: list[dict[str, str]] = []

        for record in records:
            doc_id = str(record.get("doc_id") or "").strip()
            if not doc_id:
                continue

            clean_path = self._run_store.clean_md_dir / f"{doc_id}.md"
            if clean_path.exists() and not overwrite:
                skipped_existing.append(doc_id)
                continue

            raw_text = self._run_store.read_raw_md(doc_id)
            if not raw_text.strip():
                failed_documents.append(
                    {
                        "docId": doc_id,
                        "title": str(record.get("title") or doc_id),
                        "reason": "raw_md 파일이 비어 있거나 없습니다.",
                    }
                )
                self._emit(progress_callback, _PROGRESS_KIND_FAILED, doc_id=doc_id, record=record)
                continue

            self._emit(progress_callback, _PROGRESS_KIND_START, doc_id=doc_id, record=record)

            try:
                payload = self._call_llm(raw_text)
            except Exception as exc:
                failed_documents.append(
                    {
                        "docId": doc_id,
                        "title": str(record.get("title") or doc_id),
                        "reason": f"LLM 정제 호출 실패: {exc}",
                    }
                )
                self._emit(progress_callback, _PROGRESS_KIND_FAILED, doc_id=doc_id, record=record, error=str(exc))
                continue

            paragraphs = split_paragraphs(raw_text)
            boilerplate = self._safe_index_list(payload.get("boilerplate_paragraphs"))
            keywords = self._safe_string_list(payload.get("keywords"), max_items=10)
            key_points = self._safe_string_list(payload.get("key_points"), max_items=7)

            clean_body = apply_boilerplate_removal(paragraphs, boilerplate)
            if not clean_body.strip():
                # LLM nuked everything — keep the raw body so downstream
                # consumers still have *something* to read, and log the
                # failure so the operator notices.
                clean_body = raw_text
                failed_documents.append(
                    {
                        "docId": doc_id,
                        "title": str(record.get("title") or doc_id),
                        "reason": "정제 결과가 비어 raw 본문을 그대로 사용합니다.",
                    }
                )

            self._run_store.write_clean_md(doc_id, clean_body)

            summary_path = self._run_store.paths.summary_path_for(int(doc_id))
            try:
                write_doc_metadata(
                    summary_path=summary_path,
                    record=record,
                    keywords=keywords,
                    key_points=key_points,
                )
            except Exception as exc:
                failed_documents.append(
                    {
                        "docId": doc_id,
                        "title": str(record.get("title") or doc_id),
                        "reason": f"doc_*.md 작성 실패: {exc}",
                    }
                )
                self._emit(progress_callback, _PROGRESS_KIND_FAILED, doc_id=doc_id, record=record, error=str(exc))
                continue

            cleaned_doc_ids.append(doc_id)
            self._emit(
                progress_callback,
                _PROGRESS_KIND_DONE,
                doc_id=doc_id,
                record=record,
                paragraphs=len(paragraphs),
                dropped=len(boilerplate),
                keywords=keywords,
                key_points=key_points,
            )

        return ToolResult(
            success=True,
            content=(
                f"Cleaned {len(cleaned_doc_ids)} document(s); "
                f"skipped {len(skipped_existing)} existing; "
                f"{len(failed_documents)} failure(s)."
            ),
            data={
                "cleaned_doc_ids": cleaned_doc_ids,
                "skipped_existing_doc_ids": skipped_existing,
                "failed_documents": failed_documents,
            },
        )

    # -- internals -----------------------------------------------------------

    def _kept_records(self) -> list[dict[str, Any]]:
        try:
            index_path = self._run_store.paths.index_path
        except AttributeError:
            return []
        if not index_path.exists():
            return []
        # Narrow exception list — earlier we caught ``Exception`` here, which
        # silently swallowed a ``NameError`` from a missing ``import json``
        # and returned an empty record list. Only catch the *actual* failure
        # modes (missing file / unreadable / non-JSON content) so any code
        # bug surfaces instead of hiding.
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        records = payload.get("records") if isinstance(payload, dict) else []
        if not isinstance(records, list):
            return []
        return [
            record
            for record in records
            if isinstance(record, dict)
            and record.get("doc_id")
            and not record.get("duplicate_of")
        ]

    def _call_llm(self, raw_text: str) -> dict[str, Any]:
        paragraphs = split_paragraphs(raw_text)
        if not paragraphs:
            return {"boilerplate_paragraphs": [], "keywords": [], "key_points": []}
        user_payload = annotate_paragraphs(paragraphs)
        # ``ask`` rather than ``ask_json``: the response format is plain-text
        # sections (see DOCUMENT_CLEANUP_PROMPT). JSON failed across Korean
        # bodies because LLMs do not reliably escape body quotes/commas; the
        # plain-text format + tolerant parser removes that whole failure
        # mode. ``parse_cleanup_response`` produces the exact same dict shape
        # the previous JSON path returned, so the caller is unaffected.
        text = self._llm.ask(
            DOCUMENT_CLEANUP_PROMPT,
            user_payload,
            reasoning=False,
            stream=False,
            stream_label="document-cleanup",
        )
        return parse_cleanup_response(text or "")

    @staticmethod
    def _safe_index_list(value: Any) -> list[int]:
        if not isinstance(value, list):
            return []
        out: list[int] = []
        for item in value:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _safe_string_list(value: Any, *, max_items: int) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = " ".join(str(item or "").split()).strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= max_items:
                break
        return out

    @staticmethod
    def _emit(
        progress_callback: Callable[..., None] | None,
        kind: str,
        **info: Any,
    ) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(kind, **info)
        except TypeError:
            try:
                progress_callback(kind)
            except Exception:
                pass
        except Exception:
            pass

    @staticmethod
    def _empty_summary() -> dict[str, Any]:
        return {
            "cleaned_doc_ids": [],
            "skipped_existing_doc_ids": [],
            "failed_documents": [],
        }


__all__ = ["DocumentCleanupTool"]
