"""Per-document cleanup: identify boilerplate paragraphs + extract summary/keywords/key-points.

Replaces the previous per-doc summarize pass. For each kept document:

1. Read ``raw_md/<doc_id>.md`` (Crawl4AI's pre-cleanup output).
2. Split into paragraphs and prefix each with ``[P0]``, ``[P1]`` … so the LLM
   can reference them by index in its response.
3. Call ``LLMClient.ask`` *once* with :data:`core.prompts.DOCUMENT_CLEANUP_PROMPT`.
   The LLM returns a plain-text four-section response
   (``BOILERPLATE_PARAGRAPHS`` / ``SUMMARY`` / ``KEYWORDS`` / ``KEY_POINTS``) — *not* JSON.
   The body of many non-English documents is full of quotes / commas that
   break JSON escaping; plain-text sections sidestep that entirely, and the
   :mod:`response_parser` is tolerant enough that partial / missing sections
   degrade gracefully instead of failing the whole document.
4. Check the boilerplate retention ratio. When it is an outlier — the LLM
   either dropped almost nothing (>95% kept, likely missed obvious chrome)
   or dropped almost everything (<15% kept, likely over-zealous) — make
   one retry with a "be more careful" nudge in the user payload. Whichever
   attempt lands in a sane band wins; if both are extreme, prefer the
   *more conservative* (higher-retention) result so we never silently
   delete the body.
5. Drop the flagged paragraphs and write the result to
   ``clean_md/<doc_id>.md`` (the *real* clean source that downstream
   consumers read).
6. Write ``summary/doc_<doc_id>.md`` directly from the workspace index +
   the cleanup output (no second LLM call).

The tool emits per-document progress so the API ring buffer drives the
research progress bar exactly the way it did for the old summarize loop;
``doc_cleanup_done`` carries ``summary_path`` so the frontend can flip the
document card to its "ready" state the moment cleanup lands, without
waiting for the batch summary step.

When the LLM flags so many paragraphs that the cleaned body is empty, we
fall back to the raw body and emit a ``fallback_documents`` notice — *not*
a ``failed_documents`` entry. The document is still readable and downstream
(batch / RAG / verify) consumers see the raw body; surfacing this as a
hard failure in the UI mis-classifies a degraded-but-usable run.
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

# Boilerplate retention thresholds. A *kept* paragraph is one the LLM did
# NOT flag as boilerplate. Outside this band, we make one retry with a
# nudge — the LLM has either missed obvious chrome (≥0.95 kept) or been
# over-zealous (≤0.15 kept). The band is intentionally wide: only the
# clearly-degenerate cases trigger a retry, since the average document
# legitimately keeps 50~85% of its paragraphs.
_RETENTION_RETRY_HIGH = 0.95
_RETENTION_RETRY_LOW = 0.15


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

        # Each document is cleaned end-to-end by ``_process_record`` and is
        # fully independent (its own raw_md input, its own clean_md / doc_<id>.md
        # outputs, no shared mutable state), so the per-document loop fans out
        # through ``LLMClient.map_parallel``. With ``max_parallel == 1`` this is
        # the original sequential loop; with a higher value the documents are
        # cleaned concurrently against llama-server's parallel slots. Outcomes
        # come back in input order, so the aggregate lists stay deterministic
        # regardless of which document's LLM call finished first.
        outcomes = self._llm.map_parallel(
            records,
            lambda record: self._process_record(
                record,
                overwrite=overwrite,
                progress_callback=progress_callback,
            ),
            label="cleanup",
        )

        cleaned_doc_ids: list[str] = []
        skipped_existing: list[str] = []
        failed_documents: list[dict[str, str]] = []
        fallback_documents: list[dict[str, str]] = []
        for outcome in outcomes:
            if not outcome:
                continue
            if "cleaned_doc_id" in outcome:
                cleaned_doc_ids.append(outcome["cleaned_doc_id"])
            if "skipped_existing_doc_id" in outcome:
                skipped_existing.append(outcome["skipped_existing_doc_id"])
            if outcome.get("failed"):
                failed_documents.append(outcome["failed"])
            if outcome.get("fallback"):
                fallback_documents.append(outcome["fallback"])

        return ToolResult(
            success=True,
            content=(
                f"Cleaned {len(cleaned_doc_ids)} document(s); "
                f"skipped {len(skipped_existing)} existing; "
                f"{len(fallback_documents)} fallback(s); "
                f"{len(failed_documents)} failure(s)."
            ),
            data={
                "cleaned_doc_ids": cleaned_doc_ids,
                "skipped_existing_doc_ids": skipped_existing,
                "failed_documents": failed_documents,
                "fallback_documents": fallback_documents,
            },
        )

    # -- internals -----------------------------------------------------------

    def _process_record(
        self,
        record: dict[str, Any],
        *,
        overwrite: bool,
        progress_callback: Callable[..., None] | None,
    ) -> dict[str, Any]:
        """Clean one document end-to-end and return a structured outcome.

        Self-contained: reads ``raw_md``, runs the LLM cleanup pass, writes
        ``clean_md/<id>.md`` + ``summary/doc_<id>.md``, and emits the
        start/done/failed progress events. It **never raises** — every failure
        mode is captured into the returned outcome dict — so it is safe to fan
        out across worker threads via :meth:`LLMClient.map_parallel`. The
        caller merges the (input-ordered) outcomes back into the aggregate
        result lists.

        Returned keys (any subset; an empty dict means "silently skipped"):
        ``cleaned_doc_id`` / ``skipped_existing_doc_id`` / ``failed`` /
        ``fallback``. A fallback document sets *both* ``cleaned_doc_id`` (it
        still produced usable output) and ``fallback`` (the degraded-path
        notice), matching the previous inline behavior.
        """
        doc_id = str(record.get("doc_id") or "").strip()
        if not doc_id:
            return {}

        clean_path = self._run_store.clean_md_dir / f"{doc_id}.md"
        if clean_path.exists() and not overwrite:
            return {"skipped_existing_doc_id": doc_id}

        raw_text = self._run_store.read_raw_md(doc_id)
        if not raw_text.strip():
            self._emit(progress_callback, _PROGRESS_KIND_FAILED, doc_id=doc_id, record=record)
            return {
                "failed": {
                    "docId": doc_id,
                    "title": str(record.get("title") or doc_id),
                    "reason": "raw_md 파일이 비어 있거나 없습니다.",
                }
            }

        self._emit(progress_callback, _PROGRESS_KIND_START, doc_id=doc_id, record=record)

        paragraphs = split_paragraphs(raw_text)
        try:
            payload = self._cleanup_with_retry(
                doc_id=doc_id,
                raw_text=raw_text,
                paragraphs=paragraphs,
            )
        except Exception as exc:
            self._emit(progress_callback, _PROGRESS_KIND_FAILED, doc_id=doc_id, record=record, error=str(exc))
            return {
                "failed": {
                    "docId": doc_id,
                    "title": str(record.get("title") or doc_id),
                    "reason": f"LLM 정제 호출 실패: {exc}",
                }
            }

        boilerplate = self._safe_index_list(payload.get("boilerplate_paragraphs"))
        summary_text = self._safe_text(payload.get("summary"))
        keywords = self._safe_string_list(payload.get("keywords"), max_items=10)
        key_points = self._safe_string_list(payload.get("key_points"), max_items=7)

        clean_body = apply_boilerplate_removal(paragraphs, boilerplate)
        used_fallback = False
        fallback_entry: dict[str, str] | None = None
        if not clean_body.strip():
            # LLM nuked everything — keep the raw body so downstream
            # consumers still have *something* to read. This is a degraded
            # path, not a failure: surface it as a fallback notice so the UI
            # can render the document normally without mis-classifying the
            # run as failed.
            clean_body = raw_text
            used_fallback = True
            fallback_entry = {
                "docId": doc_id,
                "title": str(record.get("title") or doc_id),
                "reason": "정제 결과가 비어 raw 본문을 그대로 사용했습니다.",
            }

        self._run_store.write_clean_md(doc_id, clean_body)

        summary_path = self._run_store.paths.summary_path_for(int(doc_id))
        try:
            write_doc_metadata(
                summary_path=summary_path,
                record=record,
                summary=summary_text,
                keywords=keywords,
                key_points=key_points,
            )
        except Exception as exc:
            self._emit(progress_callback, _PROGRESS_KIND_FAILED, doc_id=doc_id, record=record, error=str(exc))
            return {
                "failed": {
                    "docId": doc_id,
                    "title": str(record.get("title") or doc_id),
                    "reason": f"doc_*.md 작성 실패: {exc}",
                }
            }

        self._emit(
            progress_callback,
            _PROGRESS_KIND_DONE,
            doc_id=doc_id,
            record=record,
            paragraphs=len(paragraphs),
            dropped=len(boilerplate),
            summary=summary_text,
            keywords=keywords,
            key_points=key_points,
            summary_path=str(summary_path),
            used_fallback=used_fallback,
        )

        outcome: dict[str, Any] = {"cleaned_doc_id": doc_id}
        if fallback_entry is not None:
            outcome["fallback"] = fallback_entry
        return outcome

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

    def _cleanup_with_retry(
        self,
        *,
        doc_id: str,
        raw_text: str,
        paragraphs: list[str],
    ) -> dict[str, Any]:
        """Run the LLM cleanup pass with one retry on retention-ratio outliers.

        First attempt uses the default prompt. If the *boilerplate retention
        ratio* (paragraphs kept / paragraphs total) lands outside the
        ``[_RETENTION_RETRY_LOW, _RETENTION_RETRY_HIGH]`` band, we make ONE
        retry with a short nudge appended to the user payload — telling the
        model whether it kept too much or too little so it can self-correct.

        We pick the better of the two attempts. "Better" = closer to the
        sane band's centre; if both fall outside the band we prefer the
        *more conservative* (higher-retention) one — keeping noisy chrome
        is far less harmful than deleting body content. Exceptions on the
        retry are swallowed: the first attempt is still usable.
        """
        if not paragraphs:
            return {
                "boilerplate_paragraphs": [],
                "summary": "",
                "keywords": [],
                "key_points": [],
            }

        total = len(paragraphs)
        first = self._call_llm(paragraphs)
        first_retention = self._retention_ratio(first, total)
        if self._retention_in_band(first_retention):
            return first

        nudge = self._retry_nudge(first_retention)
        print(
            f"[cleanup][retry] doc_id={doc_id} retention={first_retention:.2f} "
            f"nudge={nudge!r}"
        )
        try:
            second = self._call_llm(paragraphs, extra_user_hint=nudge)
        except Exception as exc:
            print(f"[cleanup][retry-failed] doc_id={doc_id} reason={exc}")
            return first
        second_retention = self._retention_ratio(second, total)
        return self._pick_better(
            first=first,
            first_retention=first_retention,
            second=second,
            second_retention=second_retention,
        )

    def _call_llm(
        self,
        paragraphs: list[str],
        *,
        extra_user_hint: str | None = None,
    ) -> dict[str, Any]:
        user_payload = annotate_paragraphs(paragraphs)
        if extra_user_hint:
            user_payload = f"{extra_user_hint}\n\n{user_payload}"
        # ``ask`` rather than ``ask_json``: the response format is plain-text
        # sections (see DOCUMENT_CLEANUP_PROMPT). JSON failed across Korean
        # bodies because LLMs do not reliably escape body quotes/commas; the
        # plain-text format + tolerant parser removes that whole failure
        # mode. ``parse_cleanup_response`` returns ``{boilerplate_paragraphs,
        # summary, keywords, key_points}`` — missing sections come back as
        # empty values.
        text = self._llm.ask(
            DOCUMENT_CLEANUP_PROMPT,
            user_payload,
            reasoning=False,
            stream=False,
            stream_label="document-cleanup",
        )
        return parse_cleanup_response(text or "")

    @staticmethod
    def _retention_ratio(payload: dict[str, Any], total: int) -> float:
        """How much of the body the LLM kept — 1.0 = kept everything, 0.0 = dropped everything."""
        if total <= 0:
            return 1.0
        indices = payload.get("boilerplate_paragraphs") or []
        valid = {
            int(i)
            for i in indices
            if isinstance(i, int) and 0 <= int(i) < total
        }
        return max(0.0, min(1.0, (total - len(valid)) / total))

    @staticmethod
    def _retention_in_band(retention: float) -> bool:
        return _RETENTION_RETRY_LOW <= retention <= _RETENTION_RETRY_HIGH

    @staticmethod
    def _retry_nudge(first_retention: float) -> str:
        """One-line hint prepended to the retry user payload."""
        if first_retention > _RETENTION_RETRY_HIGH:
            return (
                "이전 시도에서는 거의 모든 단락을 본문으로 분류했습니다 — "
                "내비게이션, 푸터, 메뉴, 광고, 쿠키 안내 같은 명백한 chrome "
                "단락은 더 적극적으로 BOILERPLATE_PARAGRAPHS에 포함해 주세요."
            )
        return (
            "이전 시도에서는 너무 많은 단락을 boilerplate로 분류했습니다 — "
            "본문 단락(정의, 설명, 예시, 수치, 표, 인용)은 BOILERPLATE_PARAGRAPHS에 "
            "포함하지 마세요. 명백히 chrome인 단락만 골라 주세요."
        )

    @classmethod
    def _pick_better(
        cls,
        *,
        first: dict[str, Any],
        first_retention: float,
        second: dict[str, Any],
        second_retention: float,
    ) -> dict[str, Any]:
        """Return whichever retry attempt looks more sensible.

        Prefer the attempt whose retention lands inside the sane band; when
        both miss, prefer the more conservative (higher-retention) one so
        we never silently delete body text.
        """
        first_ok = cls._retention_in_band(first_retention)
        second_ok = cls._retention_in_band(second_retention)
        if first_ok and not second_ok:
            return first
        if second_ok and not first_ok:
            return second
        if not first_ok and not second_ok:
            return first if first_retention >= second_retention else second
        # Both inside the band — pick the one closer to mid-band (0.55).
        target = (_RETENTION_RETRY_LOW + _RETENTION_RETRY_HIGH) / 2.0
        first_dist = abs(first_retention - target)
        second_dist = abs(second_retention - target)
        return first if first_dist <= second_dist else second

    @staticmethod
    def _safe_text(value: Any, *, max_chars: int = 2000) -> str:
        """Coerce the parsed summary into a trimmed string.

        The parser already returns a string for ``summary``; this just
        defends against an LLM that wrapped the section in a list (the
        legacy bullet parser path can produce that for an over-formatted
        summary) and caps the length so a runaway model can't blow up the
        doc_*.md file.
        """
        if isinstance(value, list):
            value = " ".join(str(item).strip() for item in value if item)
        text = str(value or "").strip()
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        return text

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
            "fallback_documents": [],
        }


__all__ = ["DocumentCleanupTool"]
