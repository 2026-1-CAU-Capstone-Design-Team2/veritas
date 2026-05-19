"""LLM-backed source reliability judge.

Wraps :data:`core.prompts.RELIABILITY_JUDGE_PROMPT` and renders one prompt per
batch of documents (default 5 per call). Batching matters because:

* a small local model judges several docs more consistently when it sees them
  side-by-side (the prompt explicitly forbids cross-batch ranking, but having
  a shared frame stabilizes the bands);
* 5 calls instead of N calls cuts the connect-handshake overhead 5x on
  llama-server;
* the input still fits the default 8K context window comfortably
  (~1.1K tokens per doc × 5 + 500-token prompt ≈ 6K tokens).

The judge is *defensive*: any failure to call the LLM, parse JSON, or match
the returned ``doc_id`` to an input falls back to a neutral ``"medium"``
verdict rather than raising — verify must always finish and persist
*something* so the UI does not enter an error-state forever.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable

from core.prompts import RELIABILITY_JUDGE_PROMPT

from ..models import DocRecord
from .batch_index import BatchMention

logger = logging.getLogger(__name__)

# Per-batch input cap. Bigger batches risk context exhaustion on small local
# servers; smaller batches lose the consistency benefit. Five matches the
# autosurvey batch_size default so the same batch grouping logic applies.
_DEFAULT_BATCH_SIZE = 5

# Per-doc text caps. Reliability judgement does not need full body — title +
# domain + reliability_notes + a few key_points are enough signal, and keeping
# the per-doc payload short means a 5-doc batch is always well under context.
_RELIABILITY_NOTES_MAX = 6
_KEY_POINTS_MAX = 5
_BATCH_MENTIONS_MAX = 4
_SNIPPET_MAX_CHARS = 240

_LEVEL_VALUES = {"high", "medium", "low"}
_SIGNAL_VALUES = {"strong", "mixed", "weak"}


@dataclass
class ReliabilityVerdict:
    """One LLM verdict for one document.

    Mirrors the prompt's JSON schema, but normalized: every field is
    guaranteed-valid (level ∈ ``_LEVEL_VALUES``, every signal known) so
    downstream code never has to defensively re-check.
    """

    doc_id: str
    level: str
    rationale: str
    signals: dict[str, str]


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _doc_payload(
    doc: DocRecord,
    mentions: list[BatchMention],
) -> dict[str, Any]:
    """Compact JSON payload for one document inside the batch prompt.

    Only the signal-bearing fields are surfaced; the full clean_md body is
    deliberately left out (the prompt is judging trust *signals*, not
    re-summarizing the content).
    """
    return {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "domain": doc.domain,
        "url": doc.url,
        "search_query": doc.search_query,
        "summary": _clip(doc.summary, 600),
        "key_points": [_clip(p, 240) for p in doc.key_points[:_KEY_POINTS_MAX]],
        "reliability_notes": [
            _clip(n, 280) for n in doc.reliability_notes[:_RELIABILITY_NOTES_MAX]
        ],
        "batch_mentions": [
            {
                "batch_id": m.batch_id,
                "kind": m.kind,
                "snippet": _clip(m.snippet, _SNIPPET_MAX_CHARS),
            }
            for m in mentions[:_BATCH_MENTIONS_MAX]
        ],
    }


def _build_user_prompt(
    docs: list[DocRecord],
    mentions_by_doc: dict[str, list[BatchMention]],
    *,
    request_text: str,
) -> str:
    """Render the user-side payload for one batch of docs.

    Carries an ``original_user_request`` block so the judge can grade
    authority *for the topic at hand* (a chatpaper.ai mirror is more
    authoritative for an RL paper than for, say, a cooking recipe). The
    documents list keeps the same order as ``docs`` so the judge's
    "same order as input" rule has an unambiguous anchor.
    """
    payload = {
        "original_user_request": _clip(request_text, 1200),
        "documents": [
            _doc_payload(doc, mentions_by_doc.get(doc.doc_id, []))
            for doc in docs
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_signal(value: Any, default: str = "mixed") -> str:
    if isinstance(value, str) and value.strip().lower() in _SIGNAL_VALUES:
        return value.strip().lower()
    return default


def _normalize_level(value: Any, default: str = "medium") -> str:
    if isinstance(value, str) and value.strip().lower() in _LEVEL_VALUES:
        return value.strip().lower()
    return default


def _verdicts_from_response(
    raw: dict[str, Any] | None,
    docs: list[DocRecord],
) -> list[ReliabilityVerdict]:
    """Pull verdicts out of one LLM response, in the input ``docs`` order.

    The LLM is asked to keep the input order, but small local models drift.
    We match returned items to input docs by ``doc_id`` first; docs the LLM
    failed to mention fall back to a neutral ``medium`` verdict with a
    rationale that names the failure mode (so the UI can still display
    *something* without lying about the LLM's certainty).
    """
    items_raw = []
    if isinstance(raw, dict):
        candidate = raw.get("items")
        if isinstance(candidate, list):
            items_raw = candidate

    by_doc: dict[str, dict[str, Any]] = {}
    for entry in items_raw:
        if not isinstance(entry, dict):
            continue
        doc_id = str(entry.get("doc_id", "")).strip().zfill(3) if str(
            entry.get("doc_id", "")
        ).strip().isdigit() else str(entry.get("doc_id", "")).strip()
        if doc_id:
            by_doc[doc_id] = entry

    verdicts: list[ReliabilityVerdict] = []
    for doc in docs:
        entry = by_doc.get(doc.doc_id)
        if entry is None:
            verdicts.append(
                ReliabilityVerdict(
                    doc_id=doc.doc_id,
                    level="medium",
                    rationale="자동 판정 결과를 받지 못해 중간으로 표시했습니다.",
                    signals={
                        "authority": "mixed",
                        "verifiability": "mixed",
                        "self_consistency": "mixed",
                    },
                )
            )
            continue

        signals_raw = entry.get("signals") if isinstance(entry.get("signals"), dict) else {}
        rationale = str(entry.get("rationale", "")).strip()
        # Trim runaway rationales so the UI card stays scannable.
        if len(rationale) > 400:
            rationale = rationale[:399].rstrip() + "…"
        if not rationale:
            rationale = "판정 사유가 비어 있습니다."

        verdicts.append(
            ReliabilityVerdict(
                doc_id=doc.doc_id,
                level=_normalize_level(entry.get("level")),
                rationale=rationale,
                signals={
                    "authority": _normalize_signal(signals_raw.get("authority")),
                    "verifiability": _normalize_signal(signals_raw.get("verifiability")),
                    "self_consistency": _normalize_signal(signals_raw.get("self_consistency")),
                },
            )
        )
    return verdicts


def _fallback_batch(docs: Iterable[DocRecord], reason: str) -> list[ReliabilityVerdict]:
    """Build a neutral ``medium`` verdict for every doc when the LLM call fails.

    Verify must always finish and persist *something* — a workspace whose
    LLM server briefly went down should still see a verify result, just
    with the rationale telling them the judgement is provisional.
    """
    note = "자동 판정에 실패하여 임시로 중간 등급을 적용했습니다."
    if reason:
        note = f"{note} (사유: {reason[:120]})"
    return [
        ReliabilityVerdict(
            doc_id=doc.doc_id,
            level="medium",
            rationale=note,
            signals={
                "authority": "mixed",
                "verifiability": "mixed",
                "self_consistency": "mixed",
            },
        )
        for doc in docs
    ]


def judge_documents(
    docs: list[DocRecord],
    *,
    llm: Any,
    mentions_by_doc: dict[str, list[BatchMention]],
    request_text: str = "",
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> list[ReliabilityVerdict]:
    """Run the reliability LLM call over ``docs`` in fixed-size batches.

    Returns one verdict per input doc, in the input order. Duplicate
    documents (``DocRecord.is_duplicate``) are *not* sent to the LLM — the
    pipeline above inherits their verdict from the source instead.
    """
    if not docs:
        return []
    if llm is None:
        logger.warning("reliability: no LLM provided; emitting neutral verdicts")
        return _fallback_batch(docs, "LLM 클라이언트가 주입되지 않았습니다")

    verdicts: list[ReliabilityVerdict] = []
    batch_size = max(1, int(batch_size))
    for start in range(0, len(docs), batch_size):
        batch = docs[start : start + batch_size]
        prompt = _build_user_prompt(batch, mentions_by_doc, request_text=request_text)
        try:
            response = llm.ask_json(
                RELIABILITY_JUDGE_PROMPT,
                prompt,
                reasoning=False,
                max_retries=2,
                stream=False,
                stream_label=f"reliability:{start // batch_size + 1:03d}",
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "reliability: judge LLM call failed (start=%d size=%d): %s",
                start,
                len(batch),
                exc,
            )
            verdicts.extend(_fallback_batch(batch, str(exc)))
            continue
        verdicts.extend(_verdicts_from_response(response, batch))
    return verdicts


__all__ = ["ReliabilityVerdict", "judge_documents"]
