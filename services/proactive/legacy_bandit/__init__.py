"""Frozen Bandit code — kept for reference / shadow telemetry only.

The production proactive system no longer routes user-visible decisions
through these policies. See ``veritas_proactive_rule_based_reimplementation.md``
for the rationale.

If you import anything from here in a production code path, you have
re-introduced the very behavior the pivot was meant to remove. Use the
rule-based modules in the parent ``services.proactive`` package instead:

    candidates.py       — deterministic candidate generation
    evaluator.py        — hard gates + rubric score
    adaptation.py       — threshold / cooldown / suppression memory
    null_outcome_monitor.py

This package is intentionally not exported from ``services.proactive``.
"""
from __future__ import annotations
