"""Citation source-preview lookup for the document summary UI.

When the user clicks a ``[doc_NNN]`` citation marker rendered in the
``final.md`` summary preview, the frontend sends the *claim text* around the
marker plus the ``docId`` here. This service finds the closest
sentence/paragraph in that document's ``clean_md/<id>.md`` source using
**deterministic lexical matching only** — no LLM call. It exists purely as a
read-only source-preview capability for the document summary page and is
intentionally kept out of the AutoSurvey, verification, and proactive pipelines.

The pure scoring helpers live in :mod:`services.citation_match` (shared with the
AutoSurvey-side evidence builder); this module is the thin filesystem + lookup
layer on top of them.

Resolution order for a click (most to least reliable):

1. **evidence_anchor** — a verified citation-evidence atom whose *localized*
   claim matches the clicked claim. The atom was emitted in the source language
   alongside a verbatim quote during summarization and its quote was confirmed
   against ``clean_md``, so this bridges a Korean final claim to its English
   source sentence that raw lexical matching cannot.
2. **direct** — a strong direct match of the clicked claim against ``clean_md``.
3. **batch_anchor** — a cited batch-summary finding for the doc, used as a
   bridge when the direct match is weak.
4. **document_only** — no reliable sentence; show a document-level note rather
   than highlight an unrelated "closest" sentence.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from services.citation_evidence import load_atoms_from_payload, match_claim_to_evidence
from services.citation_match import (
    CITATION_MARKER_RE,
    claim_overlap,
    match_claim_in_source,
    normalize_text,
    score_sentence,
    split_paragraphs,
    split_sentences,
    tokenize,
)


# A claim is the prose around one citation marker; cap it so a runaway
# table row or merged line can't blow up the matcher or the response.
_MAX_CLAIM_CHARS = 500

# ``doc_000`` / ``000`` / ``doc-7`` → the digit run. Anything with a path
# separator, ``..`` or other noise fails this match and is rejected, which is
# the first half of the path-traversal guard (the second half re-resolves the
# final path and checks it stays under ``clean_md/``).
_DOC_ID_RE = re.compile(r"^(?:doc[_-]?)?(\d+)$", re.IGNORECASE)

# Resolution thresholds for the layered citation lookup. A direct final-claim →
# clean_md match at or above _DIRECT_STRONG_SCORE is trusted as the source
# location. Below it we try a cited batch-summary finding as an anchor; if that
# also fails we resolve at document level instead of highlighting an unrelated
# "closest" sentence (which reads as a wrong highlight).
_DIRECT_STRONG_SCORE = 0.50
_ANCHOR_MIN_SCORE = 0.45          # a batch anchor's source match must be this strong
_ANCHOR_CLAIM_MIN_OVERLAP = 0.20  # batch claim must be this related to the final claim
_BATCH_MARKER_RE = re.compile(r"\[doc[_-]?(\d+)\]", re.IGNORECASE)


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def get_citation(workspace_id: str, doc_id: str, claim: str) -> dict[str, Any]:
    """Resolve a citation marker to its source snippet.

    Always returns a dict with ``docId``/``title``/``url``/``domain``/``claim``
    and a ``match`` field. ``match`` is ``None`` when the document source is
    unavailable or no claim was supplied; otherwise it is the resolved source
    sentence (see the module docstring for the resolution order). Never raises
    for bad input — an invalid or path-traversal id yields ``match=None`` with
    an ``error`` note.
    """
    claim_text = (claim or "").strip()[:_MAX_CLAIM_CHARS]
    result: dict[str, Any] = {
        "docId": str(doc_id or ""),
        "title": "",
        "url": "",
        "domain": "",
        "claim": claim_text,
        "match": None,
        # How the source was resolved: "evidence_anchor" (verified summary-time
        # evidence atom), "direct" (strong final-claim match), "batch_anchor"
        # (bridged via a cited batch finding), or "document_only" (no reliable
        # sentence — show a document-level note).
        "resolution": "document_only",
    }

    stem = _normalize_doc_id(doc_id)
    if stem is None:
        result["error"] = "invalid_doc_id"
        return result

    source_path = _clean_md_path(workspace_id, stem)
    resolved_stem = stem
    if source_path is None:
        # Fall back to the zero-padded form (markers are ``doc_007`` but a bare
        # ``7`` should still resolve to ``007.md``).
        padded = f"{int(stem):03d}"
        if padded != stem:
            source_path = _clean_md_path(workspace_id, padded)
            resolved_stem = padded

    metadata = _lookup_metadata(workspace_id, {stem, resolved_stem})
    result["docId"] = f"doc_{resolved_stem}"
    if metadata:
        result["title"] = str(metadata.get("title") or "")
        result["url"] = str(metadata.get("final_url") or metadata.get("url") or "")
        result["domain"] = str(metadata.get("domain") or "")

    if source_path is None:
        result["error"] = "source_not_found"
        return result

    source_text = _read_text(source_path)
    if not source_text.strip() or not claim_text:
        return result

    # 0) Verified evidence atoms (built at summary time, in the source language,
    #    and confirmed against clean_md). Matching the clicked claim to an
    #    atom's *localized* claim bridges cross-language citations that direct
    #    source matching cannot. This is the most reliable anchor when present.
    atom = match_claim_to_evidence(
        claim_text, _load_evidence_atoms(workspace_id, {stem, resolved_stem})
    )
    if atom is not None:
        result["match"] = _atom_to_match(atom)
        result["resolution"] = "evidence_anchor"
        return result

    # 1) Direct match of the clicked final.md claim against this doc's source.
    direct = match_claim_in_source(claim_text, source_text)
    if direct and direct.get("score", 0.0) >= _DIRECT_STRONG_SCORE:
        direct["matchSource"] = "direct"
        result["match"] = direct
        result["resolution"] = "direct"
        return result

    # 2) Weak direct match — final.md sentences are paraphrased syntheses, so a
    #    cited batch-summary finding for the same doc is often the better bridge
    #    to a real source sentence.
    anchor = _resolve_batch_anchor(workspace_id, resolved_stem, claim_text, source_text)
    if anchor is not None:
        result["match"] = anchor
        result["resolution"] = "batch_anchor"
        return result

    # 3) Neither is strong enough — resolve at document level rather than
    #    highlight an unrelated "closest" sentence (a wrong highlight is worse
    #    than an honest document-level fallback).
    result["resolution"] = "document_only"
    return result


def _atom_to_match(atom: dict[str, Any]) -> dict[str, Any]:
    """Shape a verified evidence atom into a popup ``match`` record."""
    return {
        "text": atom.get("text", ""),
        "paragraphText": atom.get("paragraphText", ""),
        "paragraphIndex": atom.get("paragraphIndex", 0),
        "sentenceIndex": atom.get("sentenceIndex", 0),
        "score": atom.get("score", 0.0),
        # Evidence atoms are verbatim-verified, so present them with confidence
        # even if the verbatim quote was short; the localized claim is the anchor.
        "confidence": atom.get("confidence", "high"),
        "matchSource": "evidence_anchor",
        "anchorClaim": atom.get("localizedClaim", ""),
        "evidenceId": atom.get("evidenceId", ""),
    }


# --------------------------------------------------------------------------
# Filesystem access (kept thin; everything scoring-related is pure)
# --------------------------------------------------------------------------
def _workspace_root() -> Path:
    return Path(os.getenv("VERITAS_OUTPUT_DIR", "runs")).expanduser().resolve()


def _normalize_doc_id(raw: str) -> str | None:
    """``doc_000``/``000``/``doc-7`` → digit string; reject everything else.

    Returning ``None`` for any non-digit payload (slashes, ``..``, letters)
    is the primary path-traversal defence: the file name is built only from a
    validated digit run.
    """
    match = _DOC_ID_RE.match(str(raw or "").strip())
    return match.group(1) if match else None


def _clean_md_path(workspace_id: str, stem: str) -> Path | None:
    """Resolve ``runs/<ws>/clean_md/<stem>.md``, refusing to escape the dir.

    ``workspace_id`` is an attacker-influenceable path param, so beyond the
    digit-only ``stem`` we re-resolve the final path and confirm it still sits
    inside the workspace's ``clean_md`` directory before touching disk.
    """
    if not workspace_id or any(sep in workspace_id for sep in ("/", "\\", "..")):
        return None
    clean_dir = (_workspace_root() / workspace_id / "clean_md").resolve()
    candidate = (clean_dir / f"{stem}.md").resolve()
    if clean_dir != candidate.parent:
        return None
    return candidate if candidate.is_file() else None


def _lookup_metadata(workspace_id: str, stems: set[str]) -> dict[str, Any] | None:
    if not workspace_id or any(sep in workspace_id for sep in ("/", "\\", "..")):
        return None
    index_path = _workspace_root() / workspace_id / "summary" / "index.json"
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt index is non-fatal
        return None
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return None
    wanted = {s.lstrip("0") or "0" for s in stems} | stems
    for record in records:
        if not isinstance(record, dict):
            continue
        rec_id = str(record.get("doc_id") or "").strip()
        if rec_id in stems or (rec_id.lstrip("0") or "0") in wanted:
            return record
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _load_evidence_atoms(workspace_id: str, stems: set[str]) -> list[dict[str, Any]]:
    """Read the verified citation-evidence sidecar for a doc, if any.

    The sidecar (``summary/citation_evidence/<stem>.json``) is written at
    summary time and holds only bounded snippets + offsets, never raw bodies.
    Tries each candidate stem and its zero-padded form; missing/corrupt files
    resolve to an empty list so the caller falls through to direct matching.
    """
    if not workspace_id or any(sep in workspace_id for sep in ("/", "\\", "..")):
        return []
    evidence_dir = (_workspace_root() / workspace_id / "summary" / "citation_evidence").resolve()
    candidates: set[str] = set()
    for stem in stems:
        candidates.add(stem)
        try:
            candidates.add(f"{int(stem):03d}")
        except (TypeError, ValueError):
            continue
    for stem in candidates:
        path = (evidence_dir / f"{stem}.json").resolve()
        if path.parent != evidence_dir or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — missing/corrupt sidecar is non-fatal
            continue
        atoms = load_atoms_from_payload(payload)
        if atoms:
            return atoms
    return []


# --------------------------------------------------------------------------
# Batch-summary anchors — reliability fallback for weak direct matches
# --------------------------------------------------------------------------
def _summary_dir(workspace_id: str) -> Path | None:
    if not workspace_id or any(sep in workspace_id for sep in ("/", "\\", "..")):
        return None
    path = _workspace_root() / workspace_id / "summary"
    return path if path.is_dir() else None


def _strip_markers(line: str) -> str:
    """A batch-summary line with citation markers + leading Markdown removed."""
    s = CITATION_MARKER_RE.sub(" ", line)
    s = re.sub(r"^[\s>#*\-+]+", " ", s).replace("|", " ")
    return re.sub(r"\s+", " ", s).strip()[:_MAX_CLAIM_CHARS]


def _batch_claims_for_doc(workspace_id: str, stem: str) -> list[str]:
    """Marker-stripped batch-summary lines that cite ``stem`` via ``[doc_NNN]``."""
    summary_dir = _summary_dir(workspace_id)
    if summary_dir is None:
        return []
    try:
        target = int(stem)
    except ValueError:
        return []
    claims: list[str] = []
    for path in sorted(summary_dir.glob("batch_*.md")):
        for line in _read_text(path).splitlines():
            ids = {int(m.group(1)) for m in _BATCH_MARKER_RE.finditer(line)}
            if target not in ids:
                continue
            claim = _strip_markers(line)
            if len(claim) >= 12:
                claims.append(claim)
    return claims


def _resolve_batch_anchor(
    workspace_id: str, stem: str, final_claim: str, source_text: str
) -> dict[str, Any] | None:
    """Bridge a weak direct match through a cited batch-summary finding.

    Picks the batch finding (for this doc) most lexically related to the final
    claim, matches *that* finding against the document source, and returns the
    source sentence only when the match is strong enough. Returns ``None`` when
    there is no related, well-anchored finding — the caller then resolves at the
    document level. Pure scoring; no LLM call.
    """
    batch_claims = _batch_claims_for_doc(workspace_id, stem)
    if not batch_claims:
        return None
    final_content, final_numbers = tokenize(final_claim)
    ranked = sorted(
        batch_claims,
        key=lambda claim: claim_overlap(final_content, final_numbers, claim),
        reverse=True,
    )
    if claim_overlap(final_content, final_numbers, ranked[0]) < _ANCHOR_CLAIM_MIN_OVERLAP:
        return None
    best: dict[str, Any] | None = None
    best_claim = ""
    best_combined = -1.0
    for batch_claim in ranked[:3]:
        # Re-check EACH candidate's relatedness to the final claim, not only the
        # top one: a finding can match the source strongly yet be unrelated to
        # what the user actually clicked. Candidates below the overlap threshold
        # are not anchors, and the winner is chosen by a combined score (source
        # match strength + final-claim overlap) rather than source score alone.
        overlap = claim_overlap(final_content, final_numbers, batch_claim)
        if overlap < _ANCHOR_CLAIM_MIN_OVERLAP:
            continue
        candidate = match_claim_in_source(batch_claim, source_text)
        if candidate is None:
            continue
        combined = candidate["score"] + overlap
        if combined > best_combined:
            best, best_claim, best_combined = candidate, batch_claim, combined
    if best is None or best["score"] < _ANCHOR_MIN_SCORE:
        return None
    anchored = dict(best)
    anchored["matchSource"] = "batch_anchor"
    anchored["anchorClaim"] = best_claim
    return anchored


# Pure scoring helpers are re-exported from :mod:`services.citation_match` so
# existing callers/tests that import them from here keep working.
__all__ = [
    "get_citation",
    "normalize_text",
    "tokenize",
    "split_paragraphs",
    "split_sentences",
    "score_sentence",
    "match_claim_in_source",
]
