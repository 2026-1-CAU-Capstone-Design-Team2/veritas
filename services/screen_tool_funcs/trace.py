"""Focused screen-intervention trace logging.

Distinct from the verbose ``VERITAS_SCREEN_DEBUG`` mode (which also dumps a
``[screen_context][capture]`` line for *every* poll). ``VERITAS_SCREEN_TRACE``
turns on only the decision-level trace — which scenarios became candidates, what
prompt the LLM received, what the pipeline did — without the per-capture noise.

The launcher's ``--screen-debug`` flag sets ``VERITAS_SCREEN_TRACE=1`` on the API
child and filters its console output down to the ``[screen_debug]`` lines emitted
here. Gating on an env var (read live, not cached) means any component in the
screen pipeline can call :func:`screen_trace` without threading a flag through
its constructor.
"""
from __future__ import annotations

import os
import sys


def screen_trace_enabled() -> bool:
    return os.getenv("VERITAS_SCREEN_TRACE", "0") == "1"


def screen_trace(message: str) -> None:
    """Print a ``[screen_debug]``-prefixed trace when trace mode is on (else no-op).

    Every physical line is prefixed, not just the first: a multi-line message
    (e.g. an LLM prompt) is split into separate lines as it crosses the API
    child's stdout pipe, and the launcher's ``--screen-debug`` filter keeps only
    lines that carry the ``[screen_debug]`` marker — so an unprefixed body line
    would be dropped."""
    if not screen_trace_enabled():
        return
    for line in (str(message).splitlines() or [""]):
        print(f"[screen_debug] {line}", file=sys.stdout, flush=True)
