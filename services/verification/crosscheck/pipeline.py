"""Cross-check pipeline — compare external (web) claims against local (private) claims.

Pure-algorithm comparison (no LLM calls): claims are sentences carrying numbers;
two claims are compared only when they share enough content tokens, and the
relation is decided by their *measurement* values.

**Generalizability rule (VERIFY_DESIGN.md §1.9 spirit): no language- or
domain-specific keywords anywhere in this module.** Every signal must come from
*structure*: number formats, punctuation, markdown syntax, calendar/value
ranges, and (when injected) morphological POS analysis. A regression-guard test
(``tests/test_crosscheck_pipeline.py::NoHardcodedKeywordGuardTests``) fails if
a Korean/natural-language word literal or a stopword list enters this file.

Key design points (each fixes a real false-positive/false-negative class):

1. **Numbers are classified structurally before comparison.** A *bare* 4-digit
   integer in the calendar range (1900-2100, no separators / decimal / percent)
   and numbers inside ISO-date spans (2025-01) are *period identifiers*, not
   measurements. Only measurements participate in the match/mismatch decision.

2. **Different explicit years are never conflicts.** If both claims carry
   year-like identifiers and they do not overlap, the pair is skipped — figures
   from different periods differing is expected, not a contradiction.

3. **Low-information numbers are never evidence.** A single-digit bare integer
   (ordinal, counter, list index — e.g. the "4" in a quarter expression) matches
   across unrelated sentences too easily; such numbers can neither confirm
   agreement (supports) nor form a conflicting pair.

4. **A mismatch requires the same metric, not just similar magnitudes.** A
   conflicting pair must (a) share a *label* — the content words immediately
   before each number — (b) be the same kind (both percentages or both absolute
   values), and (c) sit within ``_CONFLICT_MAX_RATIO`` of each other.

5. **Confirmed internal values are never conflicts.** If an internal number is
   numerically confirmed by *any* external claim (a supports relation), other
   external claims citing different numbers are an external-vs-external
   disagreement — the consensus task's territory, not an internal↔external
   mismatch. Only internal values that no external source confirms get flagged.

6. **Tokenization honors Korean morphology when available.** The verification
   service injects its Kiwi-based ``HybridTokenizer`` (POS-tag-driven, not
   keyword-driven) so particles/endings never break token overlap. Without a
   tokenizer (tests, non-Korean corpora) a regex fallback applies.

7. **Claim extraction prefers numeric sentences** (the whole point of
   cross-checking is number comparison), skips markdown table rows, and joins
   single newlines so PDF line-wrapping does not split sentences mid-claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

from core.knowledge_models import KnowledgeSourceRecord, SourceScope
from core.models import ParsedDocRecord
from core.verification_crosscheck_models import (
    CrossCheckArtifact,
    CrossCheckClaim,
    CrossCheckRelation,
)

Tokenizer = Callable[[str], list[str]]

_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?%?")
# Unicode word characters (any script) — structural, no language assumption.
_TOKEN_RE = re.compile(r"\w[\w\-]{1,}", re.UNICODE)
# Sentence boundaries: closing punctuation, or a blank line. A single newline is
# NOT a boundary — PDF extraction wraps lines mid-sentence.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。？！])\s+|\n{2,}")
_SINGLE_NEWLINE_RE = re.compile(r"(?<!\n)\n(?!\n)")

# ISO-style date notation (2025-01, 2025-01-22). Structural: digits + dashes.
# Numbers inside a date span are period identifiers, not measurements.
_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})(?:-\d{2})?(?!\d)")

# Calendar-year detection is structural, not lexical: a *bare* integer (no
# thousands separators, no decimal part, no percent sign, no sign) whose value
# falls in this range is treated as a period identifier in any language.
# "2025" → year-like; "2,025" / "2025.5" / "2025%" stay measurements.
_YEAR_MIN = 1900
_YEAR_MAX = 2100

# A conflicting pair must be the same kind of value and within this ratio.
# Conflicting reports of the *same* metric (accounting-basis differences,
# internal vs published estimates) typically disagree by well under 20%;
# similar-magnitude numbers further apart are almost always different metrics.
_CONFLICT_MAX_RATIO = 1.2
# Label window: content tokens taken from this many characters before a number
# act as the number's metric label ("영업이익은 15.8조원" → label {영업, 이익}).
_LABEL_WINDOW_CHARS = 24
_LABEL_MAX_TOKENS = 3


@dataclass(frozen=True)
class _Comparison:
    """Internal result of one external↔local claim comparison."""

    relation: CrossCheckRelation
    # Values both sides agree on (supports only).
    common_values: frozenset[str] = frozenset()
    # (external_value, local_value) for numeric_mismatch only.
    conflict_pair: tuple[str, str] | None = None


def run_crosscheck_pipeline(
    *,
    external_docs: list[ParsedDocRecord],
    local_sources: list[KnowledgeSourceRecord],
    local_documents: dict[str, str],
    max_claims_per_source: int = 12,
    tokenizer: Tokenizer | None = None,
) -> CrossCheckArtifact:
    external_claims = _claims_from_external(external_docs, max_claims_per_source)
    local_claims = _claims_from_local(local_sources, local_documents, max_claims_per_source)
    claims = external_claims + local_claims

    # Pass 1 — compare every external↔local pair. Collect relations, the set of
    # internal values that some external source CONFIRMS (supports), and the
    # mismatch candidates (resolved later against the confirmed set).
    relations: list[CrossCheckRelation] = []
    confirmed_internal_values: set[tuple[str, str]] = set()  # (local source_id, value)
    mismatch_candidates: list[tuple[CrossCheckClaim, CrossCheckClaim, _Comparison]] = []

    for external in external_claims:
        for local in local_claims:
            comparison = _compare_claims(external, local, tokenizer=tokenizer)
            if comparison is None:
                continue
            relations.append(comparison.relation)
            if comparison.relation.relation == "supports":
                for value in comparison.common_values:
                    confirmed_internal_values.add((local.source_id, value))
            elif comparison.relation.relation in {"numeric_mismatch", "contradicts"}:
                mismatch_candidates.append((external, local, comparison))

    # Pass 2 — flags. A mismatch is only user-facing when the internal value is
    # NOT confirmed by any external source (otherwise the disagreement is
    # between external sources, which the consensus task covers), deduped by
    # (local claim, conflicting value pair).
    flags: list[dict] = []
    seen_flag_keys: set[tuple] = set()
    for external, local, comparison in mismatch_candidates:
        conflict_pair = comparison.conflict_pair or ("", "")
        _, local_value = conflict_pair
        if (local.source_id, local_value) in confirmed_internal_values:
            continue
        flag_key = (local.claim_id, comparison.relation.relation, conflict_pair)
        if flag_key in seen_flag_keys:
            continue
        seen_flag_keys.add(flag_key)
        flags.append(
            {
                "relation": comparison.relation.relation,
                "severity": comparison.relation.severity,
                "claimA": comparison.relation.claim_a,
                "claimB": comparison.relation.claim_b,
                "message": comparison.relation.reason,
            }
        )

    return CrossCheckArtifact(claims=claims, relations=relations, flags=flags)


# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------


def _claims_from_external(
    docs: list[ParsedDocRecord],
    max_claims_per_source: int,
) -> list[CrossCheckClaim]:
    claims: list[CrossCheckClaim] = []
    for doc in docs:
        source_id = f"external:{doc.doc_id}"
        candidates = []
        candidates.extend(doc.key_points or [])
        if doc.summary:
            candidates.extend(_sentences(doc.summary))
        selected = _select_claims(candidates, max_claims_per_source)
        for index, text in enumerate(selected):
            claims.append(
                CrossCheckClaim(
                    claim_id=f"{source_id}:claim_{index:03d}",
                    source_id=str(doc.doc_id),
                    source_scope=SourceScope.EXTERNAL,
                    text=text,
                    claim_type=_claim_type(text),
                    evidence_span=text,
                    metadata={"title": doc.title, "domain": doc.domain, "url": doc.url},
                )
            )
    return claims


def _claims_from_local(
    sources: list[KnowledgeSourceRecord],
    documents: dict[str, str],
    max_claims_per_source: int,
) -> list[CrossCheckClaim]:
    claims: list[CrossCheckClaim] = []
    for source in sources:
        text = documents.get(source.source_id, "")
        candidates = _sentences(text)
        selected = _select_claims(candidates, max_claims_per_source)
        for index, claim_text in enumerate(selected):
            claims.append(
                CrossCheckClaim(
                    claim_id=f"local:{source.source_id}:claim_{index:03d}",
                    source_id=source.source_id,
                    source_scope=SourceScope.LOCAL,
                    text=claim_text,
                    claim_type=_claim_type(claim_text),
                    evidence_span=claim_text,
                    metadata={
                        "title": source.title,
                        "display_path": source.display_path,
                        "privacy_label": source.privacy_label.value,
                    },
                )
            )
    return claims


def _select_claims(candidates: Iterable[str], max_claims: int) -> list[str]:
    """Pick up to ``max_claims`` candidates, numeric sentences first.

    Cross-checking compares numbers, so sentences that carry measurements are
    worth claim slots far more than banner/heading prose. ``sorted`` is stable,
    so document order is preserved within each group.
    """
    deduped = _dedupe(candidates)
    prioritized = sorted(
        deduped,
        key=lambda text: 0 if _extract_measurements(text)[0] else 1,
    )
    return prioritized[:max_claims]


# ---------------------------------------------------------------------------
# Claim comparison
# ---------------------------------------------------------------------------


def _compare_claims(
    external: CrossCheckClaim,
    local: CrossCheckClaim,
    *,
    tokenizer: Tokenizer | None = None,
) -> _Comparison | None:
    shared = _important_tokens(external.text, tokenizer) & _important_tokens(
        local.text, tokenizer
    )
    if len(shared) < 2:
        return None

    ext_meas, ext_years = _extract_measurements(external.text)
    loc_meas, loc_years = _extract_measurements(local.text)

    # Different explicit years → the numbers describe different periods;
    # a difference is expected, not a conflict. Skip the pair entirely.
    if ext_years and loc_years and not (ext_years & loc_years):
        return None

    if not ext_meas or not loc_meas:
        return _Comparison(
            relation=CrossCheckRelation(
                claim_a=external.claim_id,
                claim_b=local.claim_id,
                relation="partially_supports",
                severity="low",
                reason="External and local claims share key terms; no numeric conflict was detected.",
            )
        )

    # Agreement must rest on informative values — single-digit bare integers
    # (ordinals/counters) coincide across unrelated sentences too easily.
    common = {
        value for value in (ext_meas & loc_meas) if _is_informative(value)
    }
    if common:
        return _Comparison(
            relation=CrossCheckRelation(
                claim_a=external.claim_id,
                claim_b=local.claim_id,
                relation="supports",
                severity="low",
                reason=(
                    "External and local claims agree on "
                    f"{', '.join(sorted(common)[:5])} for overlapping terms "
                    f"({', '.join(sorted(shared)[:5])})."
                ),
            ),
            common_values=frozenset(common),
        )

    conflict = _find_conflicting_pair(
        _labeled_measurements(external.text, tokenizer),
        _labeled_measurements(local.text, tokenizer),
    )
    if conflict is not None:
        external_value, local_value, label = conflict
        return _Comparison(
            relation=CrossCheckRelation(
                claim_a=external.claim_id,
                claim_b=local.claim_id,
                relation="numeric_mismatch",
                severity="high",
                reason=(
                    f"External and local claims cite different values for the same "
                    f"metric ({', '.join(sorted(label)[:3])}): "
                    f"external={external_value}, local={local_value}."
                ),
            ),
            conflict_pair=(external_value, local_value),
        )

    # Disjoint numbers but no same-metric pair — the claims mention different
    # kinds of figures (e.g. a share % vs an absolute amount, or different
    # business units). Not a conflict.
    return _Comparison(
        relation=CrossCheckRelation(
            claim_a=external.claim_id,
            claim_b=local.claim_id,
            relation="partially_supports",
            severity="low",
            reason=(
                "External and local claims share terms but cite different kinds of "
                "figures; no same-metric conflict was detected."
            ),
        )
    )


def _find_conflicting_pair(
    external_labeled: list[tuple[str, frozenset[str]]],
    local_labeled: list[tuple[str, frozenset[str]]],
) -> tuple[str, str, frozenset] | None:
    """Return (external_value, local_value, shared_label) that genuinely disagrees.

    A genuine disagreement requires ALL of:

    * **same metric** — the labels (content words right before each number)
      overlap: "영업이익 16.4조원" conflicts with "영업이익은 15.8조원", but not
      with "매출 44조원";
    * **same kind** — both percentages or both absolute values;
    * **same scale** — within ``_CONFLICT_MAX_RATIO`` (conflicting reports of
      one metric differ by basis/estimation, not by multiples).

    The closest such pair (by ratio) is returned.
    """
    best: tuple[str, str, frozenset] | None = None
    best_ratio = float("inf")
    for external_raw, external_label in external_labeled:
        for local_raw, local_label in local_labeled:
            # Low-information numbers (single-digit ordinals/counters) cannot
            # form a meaningful head-to-head disagreement.
            if not _is_informative(external_raw) or not _is_informative(local_raw):
                continue
            shared_label = external_label & local_label
            if not shared_label:
                continue
            is_pct_external = external_raw.endswith("%")
            is_pct_local = local_raw.endswith("%")
            if is_pct_external != is_pct_local:
                continue
            try:
                external_value = abs(float(external_raw.rstrip("%")))
                local_value = abs(float(local_raw.rstrip("%")))
            except ValueError:
                continue
            if external_value == local_value:
                continue
            low, high = sorted((external_value, local_value))
            if low <= 0:
                continue
            ratio = high / low
            if ratio <= _CONFLICT_MAX_RATIO and ratio < best_ratio:
                best_ratio = ratio
                best = (external_raw, local_raw, frozenset(shared_label))
    return best


def _labeled_measurements(
    text: str,
    tokenizer: Tokenizer | None = None,
) -> list[tuple[str, frozenset[str]]]:
    """[(value, label_tokens), ...] — each measurement with its metric label.

    The label is the content tokens in the ``_LABEL_WINDOW_CHARS`` characters
    immediately before the number, keeping only the ``_LABEL_MAX_TOKENS``
    closest ones: for "4분기 영업이익은 15.8조원", the label of 15.8 is
    {영업, 이익} (with a Kiwi tokenizer) — the metric the number quantifies.
    """
    text = str(text or "")
    measurements, _ = _extract_measurements(text)
    labeled: list[tuple[str, frozenset[str]]] = []
    for match in _NUMBER_RE.finditer(text):
        value = match.group(0).replace(",", "")
        if value not in measurements:
            continue
        window_start = max(0, match.start() - _LABEL_WINDOW_CHARS)
        prefix = text[window_start : match.start()]
        labeled.append((value, _label_tokens(prefix, tokenizer)))
    return labeled


def _label_tokens(prefix: str, tokenizer: Tokenizer | None = None) -> frozenset[str]:
    """Ordered content tokens of a label window — the last few before a number."""
    if tokenizer is not None:
        try:
            ordered = list(tokenizer(str(prefix or "")))
        except Exception:
            ordered = []
    else:
        ordered = _TOKEN_RE.findall(str(prefix or ""))
    filtered = [
        token.lower()
        for token in ordered
        if len(token) >= 2 and not _is_pure_number(token)
    ]
    return frozenset(filtered[-_LABEL_MAX_TOKENS:])


# ---------------------------------------------------------------------------
# Text analysis helpers
# ---------------------------------------------------------------------------


def _claim_type(text: str) -> str:
    if _NUMBER_RE.search(text):
        return "numeric"
    if re.search(r"\b(19|20)\d{2}\b", text):
        return "date"
    return "general"


def _sentences(text: str) -> list[str]:
    # Join single newlines first: PDF/text extraction wraps lines mid-sentence,
    # and treating every newline as a boundary truncates claims half-way.
    normalized = _SINGLE_NEWLINE_RE.sub(" ", str(text or ""))
    raw = _SENTENCE_SPLIT_RE.split(normalized)
    sentences = []
    for item in raw:
        item = re.sub(r"\s+", " ", item).strip(" -*\t")
        if len(item) < 30:
            continue
        # Markdown table rows are data, not prose claims — table values are
        # queried through table_query / table profiles, not sentence matching.
        if item.count("|") >= 3:
            continue
        sentences.append(item[:600])
    return sentences


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _extract_measurements(text: str) -> tuple[set[str], set[str]]:
    """Split a claim's numbers into (measurements, year_like_identifiers).

    Structural classification only:

    * numbers inside an ISO-date span (``2025-01``) → period identifiers;
    * bare calendar-range integers (``_YEAR_MIN``..``_YEAR_MAX``, no separators
      / decimals / percent / sign) → year-like period identifiers;
    * everything else → measurements.
    """
    text = str(text or "")
    date_spans: list[tuple[int, int]] = []
    years: set[str] = set()

    for match in _DATE_RE.finditer(text):
        year_part = match.group(1)
        if _YEAR_MIN <= int(year_part) <= _YEAR_MAX:
            date_spans.append(match.span())
            years.add(year_part)

    measurements: set[str] = set()
    for match in _NUMBER_RE.finditer(text):
        inside_date = any(
            span_start <= match.start() < span_end
            for span_start, span_end in date_spans
        )
        if inside_date:
            continue
        raw = match.group(0)
        if _is_year_like(raw):
            years.add(raw)
            continue
        measurements.add(raw.replace(",", ""))

    return measurements, years


def _is_year_like(raw_number: str) -> bool:
    """Bare 4-digit integer in the calendar range — structural year detection."""
    if not raw_number.isdigit() or len(raw_number) != 4:
        return False
    return _YEAR_MIN <= int(raw_number) <= _YEAR_MAX


def _is_informative(value: str) -> bool:
    """Whether a number carries enough information to evidence (dis)agreement.

    Multi-digit values, decimals, and percentages are informative; bare
    single-digit integers (ordinals, counters, list indices) are not — they
    coincide across unrelated sentences far too easily.
    """
    stripped = value.lstrip("-").rstrip("%")
    if "." in stripped:
        return True
    if value.endswith("%"):
        return True
    return len(stripped) >= 2


def _important_tokens(text: str, tokenizer: Tokenizer | None = None) -> set[str]:
    """Content tokens used for the "are these claims about the same thing?" gate.

    With an injected tokenizer (Kiwi HybridTokenizer from the verification
    service) particles/endings are stripped by POS analysis, so "영업이익은"
    and "영업이익" share a token. Pure numbers are excluded — numeric agreement
    is judged separately on measurements. The regex fallback applies only a
    structural length filter (no stopword list — that would be a hard-coded
    vocabulary).
    """
    if tokenizer is not None:
        try:
            tokens = tokenizer(str(text or ""))
        except Exception:
            tokens = []
        return {
            token.lower()
            for token in tokens
            if len(token) >= 2 and not _is_pure_number(token)
        }

    tokens = [token.lower() for token in _TOKEN_RE.findall(str(text or ""))]
    return {
        token
        for token in tokens
        if len(token) >= 3 and not _is_pure_number(token)
    }


def _is_pure_number(token: str) -> bool:
    return bool(re.fullmatch(r"-?[\d,.]+%?", token))


__all__ = ["run_crosscheck_pipeline"]
