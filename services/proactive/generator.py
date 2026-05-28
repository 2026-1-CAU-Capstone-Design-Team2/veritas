"""ProactiveGenerator — suggestion_type → SSE event stream.

Both surfaces (native_editor / external_screen) come through here. The
orchestrator decides *what* to suggest and *what context to use*; this
generator decides *which LLM call shape* to issue and emits the SSE-shaped
event dicts the API layer wraps into ``event: {name}\ndata: {json}\n\n``.

Event shapes (consumed by ``api/services/proactive_service.py``):

    {"type": "start",  "decisionId": "...", "suggestionType": "...",
     "renderMode": "...", "contextScope": "...", "cardTone": "..."}
    {"type": "target", "targetStart": int, "targetEnd": int,
     "originalText": "..."}                            # inline-diff only
    {"type": "delta",  "text": "..."}                  # streaming chunks
    {"type": "done",   "decisionId": "..."}            # terminal
    {"type": "error",  "error": "..."}                 # terminal

The underlying generation uses the existing ChatAgent surfaces:

- ``next_sentence``                       → ``iter_ghostwrite``
- everything else                         → ``iter_editor_assist`` (with the
                                           closest existing action label
                                           polished by a type-specific lead-in)

We deliberately reuse those rather than minting a third LLM entry point —
keeping the prompt catalog under ``core/prompts/editor.py`` as the one source
of truth for tone and the same conversational/RAG guard the chat surface uses.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterator

log = logging.getLogger(__name__)


# External-card colour palette per suggestion type (§7.2). The frontend uses
# the tone string to pick a CSS class — the value here is just a tag.
CARD_TONE: dict[str, str] = {
    "next_sentence": "blue",
    "paragraph_rewrite": "orange",
    "local_copyedit": "red",
    "logic_flow_review": "purple",
    "evidence_citation_prompt": "green",
    "recovery_integration_note": "gray",
}


# Suggestion type → editor_assist action label. We pick the closest existing
# action so the prompt catalog stays unchanged; the type-specific lead-in is
# prepended to the input text so the model still gets the intent.
_ASSIST_ACTION: dict[str, str] = {
    "paragraph_rewrite": "rewrite",
    "local_copyedit": "grammar",
    "logic_flow_review": "polish",
    "evidence_citation_prompt": "continue",
    "recovery_integration_note": "rewrite",
}


# External-card format contract. SuggestionCard splits the model output on
# "설명:" — everything *before* lands in the bold black body (the
# copy-target), everything *after* lands in the muted gray note. The model
# was emitting commentary mixed into the body ("...로 바꾸는 것이 좋아
# 보입니다."), which made the copy button useless because the user would have
# to manually strip the commentary. This system block forces the split.
_FORMAT_CONTRACT_EXTERNAL = (
    "[응답 형식 — 반드시 준수]\n"
    "1) 첫 부분: 사용자가 그대로 복사-붙여넣기할 본문만. 메타 발언/이유 금지.\n"
    "2) 빈 줄 후 '설명:' 으로 시작하는 한두 줄로 이유/포인트를 별도 작성.\n"
    "[금지] 본문 안에 '추천합니다', '~하는 것이 좋아 보입니다', '권장' 같은 메타 발언.\n"
    "[금지] 본문 앞 머리말 ('아래와 같이...', '다음 문장이 적절합니다:' 등).\n"
)


# Per-suggestion lead-ins. Each ends with the input text marker the model
# will see — keep it short so the model doesn't drown in instructions and
# start producing meta-commentary anyway.
_LEAD_IN_EXTERNAL: dict[str, str] = {
    "next_sentence": (
        _FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 문맥 다음에 이어질 한 문장을 본문으로 작성.\n\n"
        + "[직전 문맥]\n"
    ),
    "paragraph_rewrite": (
        _FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 단락을 의미 보존하며 더 명확하고 자연스럽게 다시 써서 본문으로 출력. 새로운 사실 추가 금지.\n\n"
        + "[원문 단락]\n"
    ),
    "local_copyedit": (
        _FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 문장의 문법/맞춤법/표현만 최소 수정한 *완전한 문장*을 본문으로 출력.\n\n"
        + "[원문 문장]\n"
    ),
    "logic_flow_review": (
        _FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 단락에서 가장 시급한 문제 한 가지를 *고친 결과 단락*을 본문으로 출력. "
        + "원인은 설명 부분에만.\n\n"
        + "[원문]\n"
    ),
    "evidence_citation_prompt": (
        _FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 주장에 필요한 근거 한 줄(예: '2024년 통계청 자료에 따르면, ...')을 본문으로 출력. "
        + "근거가 모호하면 '[근거 필요: XX 통계]' 같은 placeholder 한 줄.\n\n"
        + "[주장]\n"
    ),
    "recovery_integration_note": (
        _FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 변경/삭제된 영역을 원문 흐름에 맞게 복구한 *완성된 문장 또는 단락*을 본문으로 출력.\n\n"
        + "[변경 영역]\n"
    ),
}


# Native lead-ins (for the day native_inline_diff renderer ships). Currently
# the native mask only emits next_sentence, which already uses ghostwrite —
# this dict is exercised only via tests until the inline-diff renderer lands.
_LEAD_IN_NATIVE: dict[str, str] = {
    "next_sentence": "",  # ghostwrite path skips lead-ins
    "paragraph_rewrite": (
        "[과업] 아래 단락을 의미 보존하며 다시 쓰되, 응답은 다시 쓴 단락 전체만 출력. "
        "어떠한 머리말/꼬리말/설명도 포함하지 말 것 (인라인 diff 렌더러가 *원문 통째로* 교체함).\n\n"
        "[원문]\n"
    ),
    "local_copyedit": (
        "[과업] 아래 문장의 오류만 고친 한 문장을 출력. 다른 어떤 텍스트도 추가하지 말 것.\n\n"
        "[원문]\n"
    ),
    "logic_flow_review": (
        "[과업] 아래 단락에서 핵심 문제 한 가지를 *반영한 단락 전체*만 출력. 설명/머리말 금지.\n\n"
        "[원문]\n"
    ),
    "evidence_citation_prompt": (
        "[과업] 다음 주장 직후에 삽입할 *근거 한 줄만* 출력 (또는 '[근거 필요]' placeholder 한 줄). "
        "어떤 설명도 덧붙이지 말 것.\n\n"
        "[주장]\n"
    ),
    "recovery_integration_note": (
        "[과업] 아래 변경 영역을 원문 흐름에 맞춰 복구한 *문장/단락만* 출력. 머리말/설명 금지.\n\n"
        "[변경 영역]\n"
    ),
}


def _resolve_assist_action(suggestion_type: str) -> str:
    return _ASSIST_ACTION.get(suggestion_type, "rewrite")


def _resolve_lead_in(suggestion_type: str, *, surface_is_native: bool) -> str:
    """Pick the surface-specific lead-in.

    External lead-ins carry the bold "본문 + 설명:" contract so SuggestionCard
    can split the result into copy-target + gray reason. Native lead-ins
    forbid any wrapping prose because the inline-diff renderer would otherwise
    drop commentary into the document body when the user accepts.
    """
    table = _LEAD_IN_NATIVE if surface_is_native else _LEAD_IN_EXTERNAL
    return table.get(suggestion_type, "")


class ProactiveGenerator:
    """Wraps the runtime's ChatAgent stream calls in the proactive event shape.

    Accepts callables (rather than the ChatAgent directly) so the test suite
    can substitute a fake stream — no need to spin up llama-server. The
    runtime adapter constructs this with the real bound methods.
    """

    def __init__(
        self,
        *,
        ghostwrite_iter: Callable[..., Iterator[str]],
        editor_assist_iter: Callable[..., Iterator[str]],
        workspace_is_active: Callable[[str], bool] | None = None,
        max_tokens_ghost: int = 64,
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
        decision: Any,
        observation: Any,
        selected_context: Any | None,
    ) -> Iterator[dict[str, Any]]:
        suggestion_type = str(decision.suggestion_type or "")
        render_mode = str(decision.render_mode or "none")
        scope = str(decision.context_scope or "none")
        card_tone = CARD_TONE.get(suggestion_type, "blue")

        start_event: dict[str, Any] = {
            "type": "start",
            "decisionId": decision.decision_id,
            "suggestionType": suggestion_type,
            "renderMode": render_mode,
            "contextScope": scope,
        }
        if render_mode == "external_card":
            start_event["cardTone"] = card_tone
        yield start_event

        if render_mode == "native_inline_diff" and selected_context is not None:
            yield {
                "type": "target",
                "targetStart": int(getattr(selected_context, "target_start", 0)),
                "targetEnd": int(getattr(selected_context, "target_end", 0)),
                "originalText": str(getattr(selected_context, "original_text", "") or ""),
            }

        try:
            grounded = bool(self._workspace_is_active(observation.workspace_id))
            for chunk in self._iter_tokens(
                suggestion_type=suggestion_type,
                observation=observation,
                selected_context=selected_context,
                use_workspace=grounded,
            ):
                if not chunk:
                    continue
                yield {"type": "delta", "text": chunk}
        except Exception as e:  # noqa: BLE001 — surfaced as SSE error
            log.warning("[proactive][generator] %s failed: %s", suggestion_type, e)
            yield {"type": "error", "error": f"{type(e).__name__}: {e}"}
            return

        yield {"type": "done", "decisionId": decision.decision_id}

    # ----------------------------------------------------------- routing

    def _iter_tokens(
        self,
        *,
        suggestion_type: str,
        observation: Any,
        selected_context: Any | None,
        use_workspace: bool,
    ) -> Iterator[str]:
        surface_is_native = (
            str(getattr(observation, "surface", "")) == "native_editor"
        )

        # next_sentence on native always goes through the ghostwrite system
        # prompt (which is already tuned for pure continuation with no
        # meta-commentary). External next_sentence goes through editor_assist
        # so we can carry the "본문 + 설명:" format contract via the lead-in.
        if suggestion_type == "next_sentence" and surface_is_native:
            prefix = str(getattr(observation, "prefix", "") or "")
            suffix = str(getattr(observation, "suffix", "") or "")
            if not prefix.strip() and selected_context is not None:
                prefix = str(getattr(selected_context, "text", "") or "")
            yield from self._ghostwrite_iter(
                prefix,
                suffix,
                max_tokens=self.max_tokens_ghost,
                use_workspace=use_workspace,
            )
            return

        action = _resolve_assist_action(suggestion_type) if suggestion_type != "next_sentence" else "continue"
        lead_in = _resolve_lead_in(suggestion_type, surface_is_native=surface_is_native)
        if selected_context is not None:
            body = str(getattr(selected_context, "text", "") or "")
        else:
            body = str(getattr(observation, "current_paragraph", "") or "")
        text = f"{lead_in}{body}".strip()
        yield from self._editor_assist_iter(
            action,
            text,
            max_tokens=self.max_tokens_assist,
            use_workspace=use_workspace,
        )


__all__ = ["CARD_TONE", "ProactiveGenerator"]
