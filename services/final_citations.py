"""Final-report citation resolution map (``summary/final_citations.json``).

A deterministic postprocessor that runs after ``final.md`` is written. For each
``[doc_NNN]`` marker occurrence in the report body it records how the citation
resolves: to a verified evidence atom (``evidence_anchor``) or to a
document-level fallback (``document_only``). It reuses the *same* matcher the
live citation popup uses (:func:`services.citation_evidence.match_claim_to_evidence`),
so the precomputed map and a live click agree.

This is an audit + preview-confidence artifact; it adds no LLM call and does not
modify ``final.md``. Markers inside fenced code and inside the ``## Source
Notes`` table are treated as document-level: a Source Notes row describes a
document, it is not a source-backed claim.
"""

from __future__ import annotations

import re
from typing import Any

from services.citation_evidence import match_claim_to_evidence


# Bracketed marker occurrence + its digits (the canonical form FINAL_PROMPT and
# the Source Notes normalizer enforce). Bare ``doc_000`` is intentionally not
# resolved here — final.md is normalized to the bracketed form.
_BRACKETED_RE = re.compile(r"\[doc[_-]?(\d+)\]", re.IGNORECASE)
# Strip both bracketed and bare markers when building the per-line claim.
_MARKER_STRIP_RE = re.compile(r"\[?doc[_-]?\d+\]?", re.IGNORECASE)
_LEADING_MD_RE = re.compile(r"^[\s>#*+\-]+")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_SOURCE_NOTES_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+Source Notes\b", re.IGNORECASE)

_MAX_CLAIM_CHARS = 500


def _claim_for_line(line: str) -> str:
    """Strip a line to the prose used as the citation claim (markers removed)."""
    s = _MARKER_STRIP_RE.sub(" ", line or "")
    s = _LEADING_MD_RE.sub(" ", s)
    s = s.replace("|", " ").replace("`", " ")
    return re.sub(r"\s+", " ", s).strip()[:_MAX_CLAIM_CHARS]


def build_final_citations(
    final_markdown: str, evidence_by_doc: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Resolve every body ``[doc_NNN]`` occurrence against per-doc evidence atoms.

    ``evidence_by_doc`` maps a zero-padded stem (``"000"``) to that document's
    verified atoms. Returns ``{"occurrences": [...], "counts": {...}}`` where
    each occurrence carries ``docId`` / ``claim`` / ``resolution`` and, when
    resolved, ``evidenceId`` / ``confidence`` / ``score``. Pure.
    """
    occurrences: list[dict[str, Any]] = []
    in_fence = False
    fence_char = ""
    in_source_notes = False

    for line in str(final_markdown or "").split("\n"):
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0]
            if not in_fence:
                in_fence, fence_char = True, marker
            elif marker == fence_char:
                in_fence, fence_char = False, ""
            continue
        if in_fence:
            continue
        if _HEADING_RE.match(line):
            in_source_notes = bool(_SOURCE_NOTES_HEADING_RE.match(line))

        markers = _BRACKETED_RE.findall(line)
        if not markers:
            continue
        claim = "" if in_source_notes else _claim_for_line(line)
        for digits in markers:
            stem = f"{int(digits):03d}"
            occurrence: dict[str, Any] = {
                "docId": f"doc_{stem}",
                "claim": claim,
                "resolution": "document_only",
            }
            # Source Notes rows and claim-free lines stay document-level.
            atom = (
                match_claim_to_evidence(claim, evidence_by_doc.get(stem) or [])
                if claim
                else None
            )
            if atom is not None:
                occurrence["resolution"] = "evidence_anchor"
                occurrence["evidenceId"] = atom.get("evidenceId", "")
                occurrence["confidence"] = atom.get("confidence", "")
                occurrence["score"] = atom.get("score", 0.0)
            occurrences.append(occurrence)

    counts: dict[str, int] = {"total": len(occurrences)}
    for occurrence in occurrences:
        key = occurrence["resolution"]
        counts[key] = counts.get(key, 0) + 1
    return {"occurrences": occurrences, "counts": counts}


__all__ = ["build_final_citations"]
