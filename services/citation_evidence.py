"""Citation-evidence atoms — the cross-language bridge for citation preview.

A document summary is written in the *user request* language (often Korean)
while its source document is frequently English, so the final report's Korean
claims cannot be matched back to an English source sentence by raw lexical
overlap. To bridge that gap without any extra LLM call, the existing
per-document summary call also emits, for its key supported claims, an
``evidence`` list of ``{claim, quote}`` pairs where:

* ``claim`` is the localized claim (same language as the summary), and
* ``quote`` is a short **verbatim** span copied from the document body
  (original language).

:func:`build_evidence_atoms` then *deterministically* verifies each ``quote``
against the document's ``clean_md`` (the same source the popup reads) and keeps
only the ones that anchor to a real source sentence. Each kept atom stores the
localized claim plus the verified source sentence + offsets. At click time
:func:`match_claim_to_evidence` matches the clicked (localized) final claim to
an atom's localized claim — a same-language comparison — and the atom already
carries the resolved source sentence.

No keyword lists, no site/language rules, no sentinel values — only the shared
lexical scorers in :mod:`services.citation_match` and continuous thresholds.
"""

from __future__ import annotations

from typing import Any

from services.citation_match import (
    claim_overlap,
    confidence_for,
    match_claim_in_source,
    tokenize,
)


# A verbatim quote should land on its source sentence with a strong score
# (an exact normalized substring scores ~0.85+); below this the "quote" was not
# really copied from the body, so the atom is dropped rather than mis-anchored.
_VERIFY_MIN_SCORE = 0.50
# At click time, the clicked final claim must share at least this fraction of
# its tokens with an atom's localized claim to adopt that atom — same-language,
# so a real paraphrase clears it while an unrelated claim does not.
_CLAIM_MIN_OVERLAP = 0.40
# Bound the text persisted per atom; offsets + a sentence-sized snippet only,
# never a raw body.
_MAX_TEXT_CHARS = 500


def build_evidence_atoms(
    doc_id: str, payload: Any, source_text: str
) -> list[dict[str, Any]]:
    """Verify a summary payload's ``evidence`` items against ``source_text``.

    Returns the list of verified atoms (possibly empty). Each atom:
    ``evidenceId`` / ``docId`` / ``localizedClaim`` / ``sourceQuote`` /
    ``text`` / ``paragraphText`` / ``paragraphIndex`` / ``sentenceIndex`` /
    ``score`` / ``confidence``. Pure and defensive — any malformed input yields
    no atoms rather than raising, so a model that omits/garbles the field simply
    falls back to direct/batch matching downstream.
    """
    raw = payload.get("evidence") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not str(source_text or "").strip():
        return []

    canonical = f"doc_{doc_id}"
    atoms: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        claim = str(item.get("claim") or "").strip()
        # Both are required: the quote anchors the source sentence, the localized
        # claim is the same-language key the click is matched against.
        if not quote or not claim:
            continue
        match = match_claim_in_source(quote, source_text)
        if match is None or match.get("score", 0.0) < _VERIFY_MIN_SCORE:
            continue
        atoms.append(
            {
                "evidenceId": f"{canonical}-e{len(atoms)}",
                "docId": canonical,
                "localizedClaim": claim[:_MAX_TEXT_CHARS],
                "sourceQuote": quote[:_MAX_TEXT_CHARS],
                "text": match["text"],
                "paragraphText": match["paragraphText"],
                "paragraphIndex": match["paragraphIndex"],
                "sentenceIndex": match["sentenceIndex"],
                "score": match["score"],
                "confidence": match["confidence"],
            }
        )
    return atoms


def load_atoms_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Extract the atom list from a persisted sidecar payload, defensively.

    Accepts either ``{"atoms": [...]}`` or a bare list, and keeps only dict
    entries that carry a localized claim (the field the click matcher needs).
    """
    if isinstance(payload, dict):
        raw = payload.get("atoms")
    else:
        raw = payload
    if not isinstance(raw, list):
        return []
    return [
        item
        for item in raw
        if isinstance(item, dict) and str(item.get("localizedClaim") or "").strip()
    ]


def match_claim_to_evidence(
    claim: str, atoms: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Pick the atom whose localized claim best overlaps the clicked *claim*.

    Same-language comparison (final claim ↔ localized claim), so it works even
    when the source sentence the atom points at is in another language. Returns
    ``None`` when there is no atom or none clears the overlap threshold.
    """
    if not claim or not atoms:
        return None
    content, numbers = tokenize(claim)
    if not content and not numbers:
        return None

    best: dict[str, Any] | None = None
    best_overlap = -1.0
    for atom in atoms:
        overlap = claim_overlap(content, numbers, str(atom.get("localizedClaim") or ""))
        if overlap > best_overlap:
            best_overlap, best = overlap, atom
    if best is None or best_overlap < _CLAIM_MIN_OVERLAP:
        return None
    # Surface the measured overlap (rounded) without mutating the stored atom.
    resolved = dict(best)
    resolved["claimOverlap"] = round(best_overlap, 4)
    if not resolved.get("confidence"):
        resolved["confidence"] = confidence_for(resolved.get("score", 0.0))
    return resolved


__all__ = [
    "build_evidence_atoms",
    "load_atoms_from_payload",
    "match_claim_to_evidence",
]
