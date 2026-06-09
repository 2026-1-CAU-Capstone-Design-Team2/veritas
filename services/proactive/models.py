"""Input data shape for the proactive pipeline.

After the bandit→rule-based pivot this module was a graveyard of unused
dataclasses (``ProactiveDecision`` / ``FeatureSnapshot`` / ``FeedbackRecord``)
and overlapping ``Literal`` types (``RenderMode`` / ``ContextScope`` /
``SuggestionType``) that had moved to ``proposal_models.py``. We've trimmed
it down to the one thing the orchestrator actually consumes: the per-tick
observation from the editor / screen pipeline.

The API layer's HTTP schema (``api/api_models.py``) is intentionally
separate so the policy core stays Pydantic-free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Surface = Literal["native_editor", "external_screen"]


@dataclass
class ProactiveObservation:
    """One observation tick from a surface — native editor cursor moves and
    external screen captures both flow in as this."""

    surface: Surface
    workspace_id: str
    document_key: str
    document_id: str = ""
    source_app: str = ""
    window_title: str = ""

    # Full text may be present in memory only — the orchestrator passes it to
    # the generator via an in-memory decision cache; persistent state must
    # never carry it. See ``policy_store.ProactiveStore`` invariants.
    text: str = ""
    # ``cursor_index`` is the caret offset within ``text`` (which may be a
    # truncated prefix/suffix window) — features.py reads it as a position
    # *relative to the window*, so it must stay window-scoped. ``doc_cursor`` is
    # the caret's offset in the WHOLE document; the orchestrator uses it for
    # anchor identity / reject-ladder locality, where a window-clamped value
    # would make every deep cursor position collapse to one spot. None for
    # surfaces (external capture) that can't supply a reliable global offset.
    cursor_index: int | None = None
    doc_cursor: int | None = None
    prefix: str = ""
    suffix: str = ""
    current_sentence: str = ""
    current_paragraph: str = ""
    previous_paragraph: str = ""
    changed_text: str = ""
    confidence: float = 0.0
    captured_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
