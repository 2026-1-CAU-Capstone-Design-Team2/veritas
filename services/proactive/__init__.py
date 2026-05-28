"""Proactive intervention pipeline (services/proactive/).

After the rule-based pivot (see ``veritas_proactive_rule_based_reimplementation.md``)
the public surface is:

- ``models``                — observation / feedback dataclasses
- ``anchors``               — ActiveAnchor extraction + confidence bands
- ``proposal_models``       — ProactiveTask / NullPrediction / SurfaceCapabilities
- ``features``              — primitive feature math (kept; helper for candidates)
- ``candidates``            — deterministic CandidateFactory (no LLM)
- ``evaluator``             — hard gates + rubric score
- ``adaptation``            — UserAdaptationMemory (threshold/cooldown/suppression)
- ``context_selector``      — anchor-relative ContextBundle materialization
- ``policy_store``          — JSONL append-only logs + adaptation glue
- ``timeout_monitor``       — render timeouts (no policy updates)
- ``null_outcome_monitor``  — TN/FN proxy classification for null decisions
- ``orchestrator``          — observe / record_feedback / explain / reset
- ``generator``             — ProactiveTask + ContextBundle → SSE
- ``screen_bridge``         — ChatAgent screen pipeline glue
- ``telemetry``             — console + per-workspace log file
- ``reward``                — canonical feedback mapping (incl. wrong_anchor)

Frozen modules — kept for reference only:
- ``legacy_bandit.policies.*`` — Action-Centered Engage + LinUCB (do not import
  in production).

The API layer (``api/api_routes/proactive.py``,
``api/services/proactive_service.py``) is the only place that translates
between Pydantic request shapes and these dataclasses.
"""
from __future__ import annotations

from .anchors import ActiveAnchor
from .models import (
    CanonicalFeedback,
    ContextScope as LegacyContextScope,  # alias to avoid shadowing proposal_models
    EngageAction,
    FeatureSnapshot,
    FeedbackRecord,
    ProactiveDecision,
    ProactiveObservation,
    RenderMode as LegacyRenderMode,
    Surface,
    SuggestionType as LegacySuggestionType,
)
from .proposal_models import (
    ContextScope,
    NullPrediction,
    Prediction,
    ProactiveTask,
    RenderMode,
    SurfaceCapabilities,
    TaskType,
    is_null,
    is_task,
)

__all__ = [
    "ActiveAnchor",
    "CanonicalFeedback",
    "ContextScope",
    "EngageAction",
    "FeatureSnapshot",
    "FeedbackRecord",
    "NullPrediction",
    "Prediction",
    "ProactiveDecision",
    "ProactiveObservation",
    "ProactiveTask",
    "RenderMode",
    "Surface",
    "SurfaceCapabilities",
    "TaskType",
    "is_null",
    "is_task",
    # Legacy aliases kept for any straggling import sites — prefer
    # proposal_models.* in new code.
    "LegacyContextScope",
    "LegacyRenderMode",
    "LegacySuggestionType",
]
