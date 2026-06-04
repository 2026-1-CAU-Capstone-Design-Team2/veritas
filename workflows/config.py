"""AutoSurvey workflow tunables.

Mirrors the ``VerificationConfig`` pattern in
``services/verification/models.py``: one frozen dataclass with flat scalar
fields, so the workflow stays free of magic numbers and the caller has a
single object to thread through.

Resolution order for each field (highest priority first):
1. Explicit caller value (e.g. the research page's ``maxDocs`` request param)
2. ``VERITAS_*`` environment variable
3. Hardcoded default

Step (2) used to be scattered across :class:`AgentRuntime` (one ``os.getenv``
call per knob, inline at every construction site); :meth:`from_env` is the
single place that lookup happens now.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _resolve_int(explicit: int | None, env_key: str, default: int) -> int:
    """Pick the first usable value among (explicit > 0, ``$VERITAS_*``, default).

    A non-positive explicit override is treated as "not provided" so callers
    can pass ``0`` / ``None`` interchangeably to mean "use env or default".
    """
    if explicit is not None and int(explicit) > 0:
        return int(explicit)
    raw = os.getenv(env_key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _resolve_int_from_env_keys(
    explicit: int | None,
    env_keys: tuple[str, ...],
    default: int,
) -> int:
    if explicit is not None and int(explicit) > 0:
        return int(explicit)
    for env_key in env_keys:
        raw = os.getenv(env_key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return default


@dataclass(frozen=True)
class AutoSurveyConfig:
    """Knobs the AutoSurvey workflow lets the caller dial.

    Defaults match the values that lived on
    :class:`AutoSurveyWorkflow.__init__` before the consolidation. Fields are
    clamped in :meth:`__post_init__` so an out-of-range value (``max_docs=0``,
    ``scout_docs > max_docs``) is corrected at construction time rather than
    crashing mid-run.
    """

    max_docs: int = 15
    collect_batch_size: int = 5
    scout_docs: int = 3
    fetch_max_chars: int = 25_000

    def __post_init__(self) -> None:
        # ``object.__setattr__`` is required on a frozen dataclass — assigning
        # via ``self.x = ...`` would raise. The clamp ranges match the
        # defensive bounds that ``AutoSurveyWorkflow.__init__`` used to apply
        # inline.
        object.__setattr__(self, "max_docs", max(1, int(self.max_docs)))
        object.__setattr__(
            self, "collect_batch_size", max(1, int(self.collect_batch_size))
        )
        object.__setattr__(
            self,
            "scout_docs",
            max(1, min(int(self.scout_docs), self.max_docs)),
        )
        object.__setattr__(
            self,
            "fetch_max_chars",
            max(1_000, int(self.fetch_max_chars)),
        )

    @classmethod
    def from_env(
        cls,
        *,
        max_docs: int | None = None,
        collect_batch_size: int | None = None,
        scout_docs: int | None = None,
        fetch_max_chars: int | None = None,
    ) -> "AutoSurveyConfig":
        """Resolve every field via ``_resolve_int`` and clamp via post-init.

        Mirrors what :meth:`AgentRuntime.run_autosurvey` was doing inline,
        but in one place that's reusable from tests and CLI entrypoints.
        """
        provider = os.getenv("VERITAS_AUTOSURVEY_LLM_PROVIDER", "local").strip().lower()
        default_fetch_max_chars = 100_000 if provider == "openai" else 25_000
        return cls(
            max_docs=_resolve_int(max_docs, "VERITAS_MAX_DOCS", 15),
            collect_batch_size=_resolve_int(
                collect_batch_size, "VERITAS_BATCH_SIZE", 5
            ),
            scout_docs=_resolve_int(scout_docs, "VERITAS_SCOUT_DOCS", 3),
            fetch_max_chars=_resolve_int_from_env_keys(
                fetch_max_chars,
                ("VERITAS_AUTOSURVEY_FETCH_MAX_CHARS", "VERITAS_FETCH_MAX_CHARS"),
                default_fetch_max_chars,
            ),
        )


__all__ = ["AutoSurveyConfig"]
