"""Glue between the existing ChatAgent screen-intervention pipeline and the
proactive bandit.

The existing screen pipeline already decides *whether* to surface an
intervention via the scenario scheduler and produces an LLM answer. To learn
from that surface without rewriting the pipeline, we:

1. After ChatAgent fires an intervention and produces its first answer chunk,
   build a ``ProactiveObservation`` from the intervention's writing/app
   context and call ``orchestrator.observe`` — the orchestrator's engage
   policy still rolls intervene/no-op, but we **force** intervene since the
   scenario scheduler already committed to showing the card. The bandit then
   records the candidate suggestion type and feature vectors for learning.
2. We rewrite the intervention's ``event_id`` to the proactive ``decisionId``
   (``pd_*``) so the frontend's SuggestionCard renders the spec MVP buttons
   (복사 / 거절 / 다시) and feedback routes through the canonical layer.
3. The bandit cannot meaningfully *gate* the screen surface in this MVP
   (that's a follow-up — replacing the scenario scheduler entirely) but it
   *learns* from every shown card's accept/reject/retry feedback.

Gated by ``VERITAS_PROACTIVE_SCREEN=0`` to opt out, defaulting on.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from .models import ProactiveObservation

log = logging.getLogger(__name__)


def proactive_screen_enabled() -> bool:
    """Return False only when the user explicitly opts out via env."""
    return os.getenv("VERITAS_PROACTIVE_SCREEN", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def _document_key(intervention: dict[str, Any]) -> str:
    """Pick a stable per-document key from the intervention payload.

    Falls back to window_title when no explicit document id is known —
    that matches the rolling-telemetry granularity the bandit's
    ``_DocumentTrack`` is keyed on.
    """
    app_context = intervention.get("app_context") or intervention.get("app") or {}
    if isinstance(app_context, dict):
        doc = (
            app_context.get("document_id")
            or app_context.get("document_path")
            or app_context.get("title")
            or app_context.get("window_title")
        )
        if doc:
            return str(doc)[:200]
    writing = intervention.get("writing_context") or {}
    if isinstance(writing, dict) and writing.get("paragraph_source"):
        return str(writing.get("paragraph_source"))[:200]
    return "external_default"


def _intervention_text(intervention: dict[str, Any]) -> tuple[str, str, str, str]:
    """Best-effort (text, current_paragraph, current_sentence, prev_paragraph)."""
    writing = intervention.get("writing_context") or {}
    if not isinstance(writing, dict):
        writing = {}
    focused = str(writing.get("focused_sentence") or "")
    recent = str(writing.get("recent_sentences") or "")
    previous = str(writing.get("previous_paragraph") or "")
    text = "\n\n".join([previous, recent]).strip() or focused
    current_paragraph = recent or focused
    return text, current_paragraph, focused, previous


def observe_screen_intervention(
    *,
    orchestrator: Any,
    intervention: dict[str, Any],
    workspace_id: str,
) -> str | None:
    """Run one proactive observe for an in-flight screen intervention.

    Returns the rewritten ``decisionId`` (``pd_*``) on success — the caller
    swaps it into the intervention dict so the frontend card uses the
    proactive feedback path. Returns ``None`` if proactive screen mode is
    disabled or anything fails (we never want to break the legacy screen
    pipeline because of a bandit hiccup).
    """
    if not proactive_screen_enabled():
        return None
    try:
        text, current_paragraph, focused, previous = _intervention_text(intervention)
        if not text and not current_paragraph and not focused:
            return None  # nothing to observe
        observation = ProactiveObservation(
            surface="external_screen",
            workspace_id=workspace_id,
            document_key=_document_key(intervention),
            document_id=str(intervention.get("event_id") or ""),
            source_app=str(
                (intervention.get("app_context") or {}).get("process_name") or ""
            ),
            window_title=str(
                (intervention.get("app_context") or {}).get("title")
                or (intervention.get("app_context") or {}).get("window_title")
                or ""
            ),
            text=text,
            cursor_index=None,
            prefix="",
            suffix="",
            current_sentence=focused,
            current_paragraph=current_paragraph,
            previous_paragraph=previous,
            changed_text="",
            confidence=0.0,
        )
        result = orchestrator.observe(observation)
        # Rule-based orchestrator returns a dict {decision_id, prediction, ...}.
        # We treat both task and null as "observed" — the screen bridge's job
        # is to wire the bandit/adaptation learning loop into the existing
        # screen pipeline, not to gate the legacy scenario scheduler's cards.
        return str(result.get("decision_id") or "") or None
    except Exception as exc:  # noqa: BLE001 — never break the screen surface
        log.warning("[proactive][screen_bridge] observe failed: %s", exc)
        return None
