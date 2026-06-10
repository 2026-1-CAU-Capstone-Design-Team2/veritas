"""ProactiveGenerator — turns a selected ProactiveTask + ContextBundle into
SSE events.

Input shape is no longer "raw observation + scenario arm". The orchestrator
hands the generator:

    task: ProactiveTask        # already evaluator-passed, scope chosen
    context: ContextBundle     # already anchor-local, no whole-doc search
    observation: ProactiveObservation  # for raw prefix and paragraph text

Output SSE events (consumed by ``api/services/proactive_service.py``):

    {"type": "start",  "decisionId": "...", "taskType": "...",
     "renderMode": "...", "contextScope": "...", "cardTone": "..."}
    {"type": "target", "targetAnchorId": "...", "originalText": "..."}
    {"type": "delta",  "text": "..."}     # streaming chunks
    {"type": "done",   "decisionId": "..."}
    {"type": "error",  "error": "..."}

Routing:

- ``next_sentence`` on native_editor → ``ChatAgent.iter_ghostwrite`` (pure
  continuation, no commentary). External next_sentence uses editor_assist
  with the "본문 + 설명:" contract.
- Every other task type → ``ChatAgent.iter_editor_assist`` with a
  task-specific lead-in pulled from :mod:`core.prompts.proactive`. All
  prompt copy lives centrally under ``core/prompts/`` — this module is
  just a router.

Native retry context expansion (see services/proactive/README.md
§"Native reject ladder"):

- reject_level == 0 → ghostwrite path; LLM sees the raw editor prefix.
- reject_level == 1 → editor_assist path; prompt body literally carries
  the last 2-3 sentences before the cursor under a "[직전 N문장]" label.
- reject_level >= 2 → editor_assist path; prompt body literally carries
  ``observation.current_paragraph`` under a "[현재 문단 전체]" label.

The text is injected into the prompt body, not just referenced via an
instruction — the LLM sees the slice in plain text.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Iterator

from core.prompts.proactive import lead_in_for, native_retry_lead_in

from .context_selector import ContextBundle
from .proposal_models import ProactiveTask

log = logging.getLogger(__name__)


# Ghost continuation token budget. The old default (64) routinely truncated
# Korean ghostwrites mid-sentence: 64 tokens is barely one short Korean
# sentence, so a 1~2 sentence continuation hit the cap before the model could
# emit EOS. The model self-limits to 1~2 sentences via ``SUGGEST_SYSTEM_PROMPT``,
# so a generous budget just lets EOS — not the length cap — end generation.
# ``iter_ghostwrite`` clamps to ``min(256, ...)`` so this stays in range.
# Operators can override with ``VERITAS_PROACTIVE_GHOST_MAX_TOKENS``.
DEFAULT_GHOST_MAX_TOKENS: int = 192


# Card tones map onto render_mode. The frontend reads ``cardTone`` to pick
# the chip color; we keep both names in the event payload for clarity.
_CARD_TONE_BY_RENDER: dict[str, str] = {
    "external_card_blue": "blue",
    "external_card_orange": "orange",
    "external_card_red": "red",
    "external_card_green": "green",
    "external_card_gray": "gray",
}


# Prompt templates (lead-ins, format contract) live in
# :mod:`core.prompts.proactive` — see ``lead_in_for(task_type, surface_is_native)``.


# Same sentence-end heuristic as context_selector — kept local so this
# module stays self-contained for the native-retry path. Matches "." "?" "!"
# plus full-width CJK terminators.
_SENT_END_RE = re.compile(r"(?<=[\.\?\!。？！])\s+")


def _split_sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENT_END_RE.split(text) if p.strip()]
    return parts or [text]


def _native_retry_context_block(
    *,
    observation: Any | None,
    reject_level: int,
    fallback_prefix: str,
) -> tuple[str, str]:
    """Return ``(label, text)`` for the explicit context block we inject
    into the native retry prompt.

    - reject_level >= 2 → ``observation.current_paragraph`` (full paragraph).
      Falls back to the last few sentences if the paragraph is empty.
    - reject_level == 1 → last 3 sentences from the prefix.
    - reject_level == 0 → caller should not invoke this (ghostwrite path).
    """
    paragraph = ""
    if observation is not None:
        paragraph = str(getattr(observation, "current_paragraph", "") or "").strip()

    if reject_level >= 2 and paragraph:
        return ("현재 문단 전체", paragraph)

    sents = _split_sentences(fallback_prefix or paragraph)
    tail = sents[-3:] if sents else []
    if tail:
        return (f"직전 {len(tail)}문장", " ".join(tail))

    # Last-ditch: whatever raw text we have.
    return ("직전 문맥", (fallback_prefix or paragraph).strip())


_ASSIST_ACTION: dict[str, str] = {
    "next_sentence": "continue",
    "paragraph_rewrite": "rewrite",
    "local_copyedit": "grammar",
    "logic_flow_review": "polish",
    "evidence_or_citation_prompt": "continue",
    "recovery_or_integration_note": "rewrite",
    "long_paragraph_split": "rewrite",
}


class ProactiveGenerator:
    """Adapts ProactiveTask + ContextBundle onto the underlying ChatAgent
    streaming calls. Behavior is type-routed; the choice of which underlying
    call to make is purely deterministic (no LLM-side dispatch)."""

    def __init__(
        self,
        *,
        ghostwrite_iter: Callable[..., Iterator[str]],
        editor_assist_iter: Callable[..., Iterator[str]],
        workspace_is_active: Callable[[str], bool] | None = None,
        max_tokens_ghost: int = DEFAULT_GHOST_MAX_TOKENS,
        max_tokens_assist: int = 400,
    ) -> None:
        self._ghostwrite_iter = ghostwrite_iter
        self._editor_assist_iter = editor_assist_iter
        self._workspace_is_active = workspace_is_active or (lambda _ws: True)
        self.max_tokens_ghost = int(max_tokens_ghost)
        self.max_tokens_assist = int(max_tokens_assist)

    def stream(
        self,
        *,
        decision_id: str,
        task: ProactiveTask,
        context: ContextBundle,
        workspace_id: str,
        surface: str,
        observation: Any | None = None,
    ) -> Iterator[dict[str, Any]]:
        render_mode = task.render_mode
        start_event: dict[str, Any] = {
            "type": "start",
            "decisionId": decision_id,
            "taskType": task.task_type,
            "renderMode": render_mode,
            "contextScope": task.context_scope,
        }
        tone = _CARD_TONE_BY_RENDER.get(render_mode)
        if tone is not None:
            start_event["cardTone"] = tone
        yield start_event

        if render_mode == "native_inline_diff":
            original = (
                context.text_parts.get("current_paragraph")
                or context.text_parts.get("current_sentence")
                or ""
            )
            yield {
                "type": "target",
                "targetAnchorId": task.target_anchor_id,
                "originalText": original,
            }

        try:
            grounded = bool(self._workspace_is_active(workspace_id))
            for chunk in self._iter_tokens(
                surface=surface,
                task=task,
                context=context,
                observation=observation,
                use_workspace=grounded,
            ):
                if chunk:
                    yield {"type": "delta", "text": chunk}
        except Exception as e:  # noqa: BLE001
            log.warning("[proactive][generator] %s failed: %s", task.task_type, e)
            yield {"type": "error", "error": f"{type(e).__name__}: {e}"}
            return

        yield {"type": "done", "decisionId": decision_id}

    # ----------------------------------------------------------- routing

    def _iter_tokens(
        self,
        *,
        surface: str,
        task: ProactiveTask,
        context: ContextBundle,
        observation: Any | None,
        use_workspace: bool,
    ) -> Iterator[str]:
        surface_is_native = surface == "native_editor"

        # next_sentence on native_editor → ghostwrite (system prompt already
        # forbids meta-commentary). Everything else goes through editor_assist
        # so we can carry the lead-in contract.
        if task.task_type == "next_sentence" and surface_is_native:
            # Use the editor's raw prefix/suffix — see README §"Native ghost
            # context source" for why we don't reconstruct from text_parts.
            prefix = ""
            suffix = ""
            if observation is not None:
                prefix = str(getattr(observation, "prefix", "") or "")
                suffix = str(getattr(observation, "suffix", "") or "")
            if not prefix.strip():
                prefix = (
                    context.text_parts.get("prev_sentence", "")
                    + ("\n" if context.text_parts.get("prev_sentence") else "")
                    + context.text_parts.get("current_fragment", "")
                )
                if not prefix.strip():
                    prefix = context.text_parts.get("current_paragraph", "")

            # Native retry / post-reject path: when the orchestrator attached
            # reject_level >= 1 (or last_rejected_text from a "다시" click)
            # we abandon the plain ghostwrite system prompt. It can't convey
            # "don't repeat this" — and more importantly, we want to EXPAND
            # the context the LLM sees as the user keeps rejecting.
            #
            # Level 1: prompt body literally contains the last 2-3 sentences
            #          before the cursor (extracted from the prefix).
            # Level 2: prompt body literally contains the FULL current
            #          paragraph (from ``observation.current_paragraph``).
            #
            # The point: it's not just an instruction. The paragraph text is
            # right there in the prompt for the LLM to read.
            reject_level = int(task.metadata.get("reject_level") or 0)
            avoid_text = str(task.metadata.get("last_rejected_text") or "")
            if reject_level >= 1 or avoid_text:
                ctx_label, ctx_text = _native_retry_context_block(
                    observation=observation,
                    reject_level=reject_level,
                    fallback_prefix=prefix,
                )
                prompt = native_retry_lead_in(
                    avoid_text=avoid_text,
                    reject_level=reject_level,
                    context_label=ctx_label,
                    context_text=ctx_text,
                )
                yield from self._editor_assist_iter(
                    "continue",
                    prompt,
                    max_tokens=self.max_tokens_assist,
                    use_workspace=use_workspace,
                    # Proactive retry must not hard-fail when the workspace has no
                    # index — ground if available, else fall back to plain.
                    additive_grounding=True,
                )
                return

            # Section heading the cursor sits under (the editor sends it because
            # it knows the whole document, even when the heading scrolled out of
            # the prefix window). Anchors the continuation to the section topic.
            section_heading = ""
            if observation is not None:
                meta = getattr(observation, "metadata", None) or {}
                section_heading = str(meta.get("section_heading") or "").strip()
            yield from self._ghostwrite_iter(
                prefix,
                suffix,
                max_tokens=self.max_tokens_ghost,
                use_workspace=use_workspace,
                section_heading=section_heading,
            )
            return

        lead_in = lead_in_for(task_type=task.task_type, surface_is_native=surface_is_native)
        body = self._compose_body(task=task, context=context, native=surface_is_native)
        text = f"{lead_in}{body}".strip()
        action = _ASSIST_ACTION.get(task.task_type, "rewrite")
        yield from self._editor_assist_iter(
            action,
            text,
            max_tokens=self.max_tokens_assist,
            use_workspace=use_workspace,
            # Proactive suggestions are additive — grounding helps but never
            # hard-gates, so an un-indexed workspace still gets a suggestion.
            additive_grounding=True,
        )

    def _compose_body(
        self,
        *,
        task: ProactiveTask,
        context: ContextBundle,
        native: bool,
    ) -> str:
        parts = context.text_parts
        # Compose in a task-type-specific order so the model sees the most
        # relevant slice first.
        if task.task_type == "next_sentence":
            body = " ".join(
                p for p in (parts.get("prev_sentence"), parts.get("current_fragment")) if p
            )
            return body or parts.get("current_paragraph", "")
        if task.task_type == "paragraph_rewrite":
            return parts.get("current_paragraph", "")
        if task.task_type == "local_copyedit":
            return parts.get("current_sentence") or parts.get("current_paragraph", "")
        if task.task_type == "logic_flow_review":
            chunks = []
            if parts.get("section_heading"):
                chunks.append(f"[섹션] {parts['section_heading']}")
            if parts.get("prev_paragraph"):
                chunks.append(f"[이전 단락]\n{parts['prev_paragraph']}")
            chunks.append(f"[현재 단락]\n{parts.get('current_paragraph', '')}")
            if parts.get("next_paragraph"):
                chunks.append(f"[다음 단락]\n{parts['next_paragraph']}")
            return "\n\n".join(chunks)
        if task.task_type == "evidence_or_citation_prompt":
            claim = parts.get("claim_window") or parts.get("current_sentence", "")
            if context.source_snippets:
                claim += "\n\n[참고 자료]\n" + "\n---\n".join(context.source_snippets)
            return claim
        if task.task_type == "recovery_or_integration_note":
            chunks = []
            if parts.get("diff_region"):
                chunks.append(f"[변경 영역]\n{parts['diff_region']}")
            if parts.get("surrounding_paragraph"):
                chunks.append(f"[주변 단락]\n{parts['surrounding_paragraph']}")
            return "\n\n".join(chunks)
        if task.task_type == "long_paragraph_split":
            return parts.get("current_paragraph", "")
        return parts.get("current_paragraph") or parts.get("current_sentence") or ""
