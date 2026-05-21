"""LLM scenario router for screen interventions.

Replaces the CFS scheduler's *selection* step (which ranked ready scenarios by
vruntime — a fairness proxy that optimizes "whose turn is it" rather than "what
is most useful now"). Given the ready candidates the rule fan-out produced plus
their evidence, this asks a small LLM call to pick the single most useful one for
the current on-screen situation — or to decline entirely when nothing genuinely
helps. The rules stay as a cheap recall filter; the LLM provides precision and
intent reading. CFS's cheap reflexes (global throttle, recency) are kept around
the router by the caller.

Gated by ``VERITAS_SCREEN_ROUTER`` in :mod:`...intervention_detector`; when off,
the detector falls back to CFS selection unchanged.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


# One-line *situation* meaning per scenario (distinct from the OUTPUT guidance in
# core.prompts.SCREEN_SCENARIO_GUIDANCE). The router reads these to judge which
# candidate matches the user's current need. Unknown names fall back to the name.
SCENARIO_SITUATIONS: dict[str, str] = {
    "idle_after_writing": "User paused mid-paragraph; writing flow still warm, may want the next sentence.",
    "whole_document_review": "A substantial draft just went idle; a holistic structure/flow pass could help.",
    "long_static_review": "Document sat unedited a long time; user is likely proofreading.",
    "paragraph_churn": "User keeps rewriting the same paragraph; stuck on phrasing.",
    "blank_document_start": "Document is nearly empty; user is at the very start and may want an opening.",
    "outline_phase": "User is writing in outline form (short lines, bullets); may want item expansion.",
    "acronym_introduced": "An acronym appeared; may need a spelled-out definition on first use.",
    "heading_added": "A section heading was added; user may want help starting that section.",
    "long_paragraph_written": "Current paragraph grew very long; may want a split point.",
    "numbered_list_growth": "User is building a numbered list; may want the next item(s).",
    "todo_marker_present": "Explicit TODO/FIXME markers present; user may want them summarized.",
    "many_question_marks": "Several open questions posed; user is framing, may want which to resolve.",
    "code_block_present": "A code block was inserted; may want a one-line description or bug flag.",
    "quote_inserted": "A quoted passage appeared; attribution may be missing.",
    "citation_missing": "Factual claims with stats/years but no citation markers.",
    "factual_claim_made": "A factual claim with numbers/year was just written; may need a source.",
    "repeated_phrase_in_paragraph": "Same short phrase repeats within one paragraph; wording variety may help.",
    "transition_word_overuse": "Recent writing leans heavily on transition words.",
    "weak_modifier_overuse": "Recent writing repeats vague intensity modifiers.",
    "scattered_edits": "Small edits scattered across the doc; a consistency pass may help.",
    "large_deletion": "A large chunk was just deleted; user may want a recovery note.",
    "copy_paste_growth": "A large chunk was just pasted in; may need integration help.",
    "undo_cycle_detected": "User is oscillating between two versions of the same text.",
}

_SYSTEM_PROMPT = (
    "You are the routing brain of a proactive writing assistant. A rule layer has "
    "flagged some candidate situations on the user's screen. Pick the SINGLE candidate "
    "that would most help the user RIGHT NOW, or decline if none genuinely helps. "
    "Prefer declining over interrupting when the text is just placeholders/skeleton, "
    "when the user is in fast flow, or when the same help was just given. "
    "Reply with JSON only: "
    '{"scenario": "<one candidate name or none>", "confidence": <0.0-1.0>, "reason": "<short>"}.'
)


@dataclass
class RouterDecision:
    scenario: str | None  # chosen candidate name, or None to decline
    confidence: float
    reason: str


class ScenarioRouter:
    def __init__(self, llm, *, timeout_sec: float = 30.0) -> None:
        self.llm = llm
        self.timeout_sec = timeout_sec

    @staticmethod
    def enabled() -> bool:
        return os.getenv("VERITAS_SCREEN_ROUTER", "0") == "1"

    def route(
        self,
        *,
        document_type: str,
        recent_text: str,
        focused_text: str,
        candidates: list[tuple[str, list[str]]],
        recent_fired: dict[str, float],
        now: float,
    ) -> RouterDecision:
        """Pick one candidate or decline. ``candidates`` is (name, evidence_reasons)."""
        names = [name for name, _ in candidates]
        if not names:
            return RouterDecision(None, 0.0, "no candidates")

        catalog_lines = []
        for name, reasons in candidates:
            since = ""
            if name in recent_fired:
                since = f" (fired {int(now - recent_fired[name])}s ago)"
            why = f" | evidence: {', '.join(reasons)}" if reasons else ""
            catalog_lines.append(
                f"- {name}: {SCENARIO_SITUATIONS.get(name, name)}{since}{why}"
            )

        user_prompt = (
            f"Document type: {document_type}\n\n"
            f"Latest sentence (focused):\n{focused_text or '(none)'}\n\n"
            f"Recent writing context:\n{recent_text or '(none)'}\n\n"
            f"Candidate situations (choose at most one by exact name):\n"
            + "\n".join(catalog_lines)
            + "\n\nReturn JSON only."
        )
        try:
            data = self.llm.ask_json(
                _SYSTEM_PROMPT,
                user_prompt,
                reasoning=False,
                stream_label="screen_router",
                timeout_sec=self.timeout_sec,
            )
        except Exception as e:  # noqa: BLE001 - router must never break detection
            return RouterDecision(None, 0.0, f"router error: {e}")

        return self._parse(data, names)

    @staticmethod
    def _parse(data: Any, names: list[str]) -> RouterDecision:
        if not isinstance(data, dict):
            return RouterDecision(None, 0.0, "non-dict router output")
        scenario = str(data.get("scenario") or "").strip()
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(data.get("reason") or "")[:200]
        if scenario in ("", "none", "None", "null"):
            return RouterDecision(None, confidence, reason or "declined")
        if scenario not in names:
            # Model named something not in the candidate set — treat as decline.
            return RouterDecision(None, confidence, f"off-list pick '{scenario}'")
        return RouterDecision(scenario, confidence, reason)
