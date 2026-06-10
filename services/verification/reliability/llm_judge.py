"""LLM-backed source reliability judge.

Wraps :data:`core.prompts.RELIABILITY_JUDGE_PROMPT` and renders one prompt per
batch of documents. Batching matters because:

* a small local model judges several docs more consistently when it sees them
  side-by-side (the prompt explicitly forbids cross-batch ranking, but having
  a shared frame stabilizes the bands);
* fewer calls than N cuts the connect-handshake overhead on llama-server;
* the input still fits the default 8K context window comfortably with the
  per-doc caps in :class:`services.verification.models.VerificationConfig`.

Every tunable lives on ``VerificationConfig`` (``reliability_batch_size``,
``reliability_notes_max``, …) so a single ``cfg.fingerprint()`` change
invalidates the on-disk cache when any of them moves.

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

from core.models import ParsedDocRecord

from ..models import VerificationConfig
from .batch_index import BatchMention

logger = logging.getLogger(__name__)

# Vocabulary the prompt commits to. Not knobs — these are the validator's
# contract with the LLM, used to drop unknown values into a known default.
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
    doc: ParsedDocRecord,
    mentions: list[BatchMention],
    cfg: VerificationConfig,
) -> dict[str, Any]:
    """Compact JSON payload for one document inside the batch prompt.

    Only the signal-bearing fields are surfaced; the full clean_md body is
    deliberately left out (the prompt is judging trust *signals*, not
    re-summarizing the content). Per-doc caps come from ``cfg`` so a
    config change invalidates the cache via ``cfg.fingerprint()``.
    """
    return {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "domain": doc.domain,
        "url": doc.url,
        "search_query": doc.search_query,
        "summary": _clip(doc.summary, 600),
        "key_points": [
            _clip(p, 240) for p in doc.key_points[: cfg.reliability_key_points_max]
        ],
        "reliability_notes": [
            _clip(n, 280)
            for n in doc.reliability_notes[: cfg.reliability_notes_max]
        ],
        "batch_mentions": [
            {
                "batch_id": m.batch_id,
                "kind": m.kind,
                "snippet": _clip(m.snippet, cfg.reliability_snippet_max_chars),
            }
            for m in mentions[: cfg.reliability_batch_mentions_max]
        ],
    }


def _build_user_prompt(
    docs: list[ParsedDocRecord],
    mentions_by_doc: dict[str, list[BatchMention]],
    *,
    request_text: str,
    cfg: VerificationConfig,
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
            _doc_payload(doc, mentions_by_doc.get(doc.doc_id, []), cfg)
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


# Signal keys we surface to the rest of the system. Order matters: the
# UI renders them top-down in this order so the most decisive signal
# (request_alignment) reads first.
_SIGNAL_KEYS: tuple[str, ...] = (
    "request_alignment",
    "authority",
    "verifiability",
    "self_consistency",
)


def _derive_level(signals: dict[str, str], llm_level: str) -> str:
    """Re-derive the final level from the 4 sub-signals.

    Pure rule-based — the LLM's own ``level`` is intentionally NOT used as a
    tiebreaker (a drifting small model emits a ``level`` inconsistent with its
    own signals; we trust the signals + this matrix so the verdict matches the
    explanation the user can verify in the breakdown). ``llm_level`` is accepted
    for caller-side logging / debugging only.

    Decision matrix (in order):

    1. ``request_alignment == "weak"`` — **soft** override. Off-topic is a
       strong negative, but a lone weak-alignment signal is exactly what a small
       judge over-emits: it conflates "doesn't fully answer the deliverable"
       with "off-topic", and these documents already passed the collection
       on-topic gate. So a single weak alignment must NOT nuke an otherwise
       credible source to "low" — it caps the verdict at "medium" and only
       reaches "low" when a SECOND signal corroborates the weakness (an
       off-topic *and* low-quality source).

    2. Otherwise, with the remaining three signals:
       * 2+ of {authority, verifiability, self_consistency} are "strong"
         AND none of the three is "weak"             → ``"high"``
       * 2+ of those three are "weak"                → ``"low"``
       * everything else                             → ``"medium"``
    """
    _ = llm_level  # accepted for trace/debug parity, not used in decision
    rest = (
        signals.get("authority", "mixed"),
        signals.get("verifiability", "mixed"),
        signals.get("self_consistency", "mixed"),
    )
    strong = sum(1 for s in rest if s == "strong")
    weak = sum(1 for s in rest if s == "weak")

    if signals.get("request_alignment", "mixed") == "weak":
        # Corroboration required: drop to "low" only if another signal is also
        # weak; a lone weak alignment caps at "medium".
        return "low" if weak >= 1 else "medium"

    if strong >= 2 and weak == 0:
        return "high"
    if weak >= 2:
        return "low"
    return "medium"


def _verdicts_from_response(
    raw: dict[str, Any] | None,
    docs: list[ParsedDocRecord],
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
                    signals={key: "mixed" for key in _SIGNAL_KEYS},
                )
            )
            continue

        signals_raw = entry.get("signals") if isinstance(entry.get("signals"), dict) else {}
        normalized_signals = {
            key: _normalize_signal(signals_raw.get(key)) for key in _SIGNAL_KEYS
        }
        rationale = str(entry.get("rationale", "")).strip()
        # Trim runaway rationales so the UI card stays scannable.
        if len(rationale) > 400:
            rationale = rationale[:399].rstrip() + "…"
        if not rationale:
            rationale = "판정 사유가 비어 있습니다."

        llm_level = _normalize_level(entry.get("level"))
        final_level = _derive_level(normalized_signals, llm_level)

        # Surface the alignment-driven adjustment so the user understands the
        # level. Wording reflects the *actual* outcome of the soft override:
        # a lone weak alignment caps the doc at "medium" (등급 조정), while a
        # corroborated weakness lands it at "low" (등급 하향).
        if normalized_signals["request_alignment"] == "weak":
            note = (
                "사용자 요청 주제와 불일치로 등급을 낮춤. "
                if final_level == "low"
                else "사용자 요청 주제와의 적합성이 낮아 등급을 조정함. "
            )
            if not rationale.startswith(note):
                rationale = note + rationale

        verdicts.append(
            ReliabilityVerdict(
                doc_id=doc.doc_id,
                level=final_level,
                rationale=rationale,
                signals=normalized_signals,
            )
        )
    return verdicts


def _fallback_batch(docs: Iterable[ParsedDocRecord], reason: str) -> list[ReliabilityVerdict]:
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
            signals={key: "mixed" for key in _SIGNAL_KEYS},
        )
        for doc in docs
    ]


def judge_documents(
    docs: list[ParsedDocRecord],
    *,
    llm: Any,
    mentions_by_doc: dict[str, list[BatchMention]],
    cfg: VerificationConfig,
    request_text: str = "",
) -> list[ReliabilityVerdict]:
    """Run the reliability LLM call over ``docs`` in fixed-size batches.

    Returns one verdict per input doc, in the input order. Duplicate
    documents (``ParsedDocRecord.is_duplicate``) are *not* sent to the LLM — the
    pipeline above inherits their verdict from the source instead. Batch
    size and per-doc caps come from ``cfg``.

    Batches are independent (the prompt forbids cross-batch ranking), so they
    fan out through :meth:`LLMClient.map_parallel` — the same single knob
    (``max_parallel`` / ``VERITAS_LLM_PARALLEL`` / 설정 > 병렬 디코딩) used by the
    AutoSurvey cleanup / summarize loops. With ``max_parallel == 1`` this is the
    original sequential loop. Reliability prompts carry only compact per-doc
    signals (summary / key points / notes — not full bodies), so concurrent
    batches stay well within the context window. Results are returned in input
    order; the caller sorts by ``doc_id`` regardless.
    """
    if not docs:
        return []
    if llm is None:
        logger.warning("reliability: no LLM provided; emitting neutral verdicts")
        return _fallback_batch(docs, "LLM 클라이언트가 주입되지 않았습니다")

    batch_size = max(1, int(cfg.reliability_batch_size))
    indexed_batches = [
        (start // batch_size + 1, docs[start : start + batch_size])
        for start in range(0, len(docs), batch_size)
    ]

    def _judge_one_batch(
        item: tuple[int, list[ParsedDocRecord]],
    ) -> list[ReliabilityVerdict]:
        """Judge one independent batch. Never raises — any LLM / parse failure
        degrades to a neutral fallback verdict so verify always finishes (the
        module-level contract), which also makes it safe to run on a worker
        thread."""
        batch_no, batch = item
        prompt = _build_user_prompt(
            batch, mentions_by_doc, request_text=request_text, cfg=cfg
        )
        try:
            response = llm.ask_json(
                RELIABILITY_JUDGE_PROMPT,
                prompt,
                reasoning=False,
                max_retries=2,
                stream=False,
                stream_label=f"reliability:{batch_no:03d}",
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "reliability: judge LLM call failed (batch=%d size=%d): %s",
                batch_no,
                len(batch),
                exc,
            )
            return _fallback_batch(batch, str(exc))
        return _verdicts_from_response(response, batch)

    mapper = getattr(llm, "map_parallel", None)
    if callable(mapper):
        batch_results = mapper(indexed_batches, _judge_one_batch, label="reliability")
    else:
        batch_results = [_judge_one_batch(item) for item in indexed_batches]

    verdicts: list[ReliabilityVerdict] = []
    for batch_verdicts in batch_results:
        verdicts.extend(batch_verdicts)
    return verdicts


__all__ = ["ReliabilityVerdict", "judge_documents"]
