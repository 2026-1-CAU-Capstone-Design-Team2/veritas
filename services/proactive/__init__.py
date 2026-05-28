"""Proactive intervention pipeline (services/proactive/).

After the rule-based pivot the public surface is intentionally small:

- ``models``                — ``ProactiveObservation`` (the observe-tick input)
- ``anchors``               — ActiveAnchor extraction + confidence bands
- ``proposal_models``       — ProactiveTask / NullPrediction / SurfaceCapabilities
- ``features``              — primitive feature math (lexical-keyword free)
- ``candidates``            — deterministic CandidateFactory (no LLM)
- ``evaluator``             — hard gates + rubric score
- ``adaptation``            — UserAdaptationMemory (threshold / cooldown / suppression)
- ``context_selector``      — anchor-relative ContextBundle materialization
- ``policy_store``          — JSONL append-only logs + adaptation glue
- ``timeout_monitor``       — render timeouts
- ``null_outcome_monitor``  — TN/FN proxy classification for null decisions
- ``orchestrator``          — observe / record_feedback / explain / reset
- ``generator``             — ProactiveTask + ContextBundle → SSE
- ``screen_bridge``         — ChatAgent screen pipeline glue
- ``telemetry``             — console + per-workspace log file
- ``reward``                — canonical feedback mapping (incl. wrong_anchor)

Frozen reference (do not import in production): ``legacy_bandit/`` —
see ``services/proactive/README.md`` §2.
"""
from __future__ import annotations

from .anchors import ActiveAnchor
from .models import ProactiveObservation, Surface
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
    "ContextScope",
    "NullPrediction",
    "Prediction",
    "ProactiveObservation",
    "ProactiveTask",
    "RenderMode",
    "Surface",
    "SurfaceCapabilities",
    "TaskType",
    "is_null",
    "is_task",
]
