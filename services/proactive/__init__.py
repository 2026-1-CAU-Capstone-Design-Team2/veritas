"""Proactive intervention bandit (services/proactive/).

Owns the *decision* of when and what to suggest while the user is writing —
native editor or external Windows app — and the *learning* signal that closes
the loop with canonical user feedback.

Layout (per veritas_bandit_policy_implementation_guide.md):

- ``models``             — pure dataclasses (observation/decision/feedback) +
                           Literal types shared across surfaces.
- ``features``           — primitive telemetry extractor + engage / suggest
                           feature vectors. No LLM, no I/O.
- ``action_space``       — suggestion-type action mask from primitives.
- ``context_selector``   — default per-action context scope + extraction.
- ``reward``             — surface-specific feedback → canonical reward.
- ``policies/``          — Disjoint Discounted LinUCB (suggestion candidate)
                           and Action-Centered Engage Policy (intervene/no-op).
- ``policy_store``       — per-workspace JSON state + JSONL append logs.
- ``timeout_monitor``    — backend timeout sweeper for rendered interventions
                           and no-op outcome horizon.
- ``orchestrator``       — observe / record_feedback wiring; the one entry
                           point for both native and external surfaces.
- ``generator``          — SSE generator that maps suggestion_type +
                           selected_context onto the shared LLM pipeline.

The API layer (``api/api_routes/proactive.py``,
``api/services/proactive_service.py``) is a thin adapter over the orchestrator
so this package stays UI- and HTTP-agnostic.
"""
from __future__ import annotations

from .models import (
    CanonicalFeedback,
    ContextScope,
    EngageAction,
    FeatureSnapshot,
    FeedbackRecord,
    ProactiveDecision,
    ProactiveObservation,
    RenderMode,
    Surface,
    SuggestionType,
)

__all__ = [
    "CanonicalFeedback",
    "ContextScope",
    "EngageAction",
    "FeatureSnapshot",
    "FeedbackRecord",
    "ProactiveDecision",
    "ProactiveObservation",
    "RenderMode",
    "Surface",
    "SuggestionType",
]
