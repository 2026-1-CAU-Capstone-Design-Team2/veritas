"""ProactiveGenerator — turns a selected ProactiveTask + ContextBundle into
SSE events.

Input shape is no longer "raw observation + scenario arm". The orchestrator
hands the generator:

    task: ProactiveTask        # already evaluator-passed, scope chosen
    context: ContextBundle     # already anchor-local, no whole-doc search

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
  task-specific lead-in. Native lead-ins (for the inline-diff renderer)
  forbid wrapping prose. External lead-ins enforce the body/explanation
  split so SuggestionCard's body / note labels work.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterator

from .context_selector import ContextBundle
from .proposal_models import ProactiveTask

log = logging.getLogger(__name__)


# Card tones map onto render_mode. The frontend reads ``cardTone`` to pick
# the chip color; we keep both names in the event payload for clarity.
_CARD_TONE_BY_RENDER: dict[str, str] = {
    "external_card_blue": "blue",
    "external_card_orange": "orange",
    "external_card_red": "red",
    "external_card_green": "green",
    "external_card_gray": "gray",
}


# Output format contract for external cards. Keep in sync with
# SuggestionCard's "설명:" split in document_assist_window.py.
_FORMAT_CONTRACT_EXTERNAL = (
    "[응답 형식 — 반드시 준수]\n"
    "1) 첫 부분: 사용자가 그대로 복사-붙여넣기할 본문만. 메타 발언/이유 금지.\n"
    "2) 빈 줄 후 '설명:' 으로 시작하는 한두 줄로 이유/포인트를 별도 작성.\n"
    "[금지] 본문 안에 '추천합니다', '~하는 것이 좋아 보입니다', '권장' 같은 메타 발언.\n"
    "[금지] 본문 앞 머리말 ('아래와 같이...', '다음 문장이 적절합니다:' 등).\n"
)


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
        + "[과업] 아래 문장/문단의 문법, 맞춤법, 반복 표현만 최소 수정한 *완전한 문장(들)*을 본문으로 출력.\n\n"
        + "[원문]\n"
    ),
    "logic_flow_review": (
        _FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 단락에서 가장 시급한 흐름 문제 한 가지를 *고친 결과 단락*을 본문으로 출력. "
        + "원인은 설명 부분에만.\n\n"
        + "[원문]\n"
    ),
    "evidence_or_citation_prompt": (
        _FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 주장에 필요한 근거 한 줄(예: '2024년 통계청 자료에 따르면, ...')을 본문으로 출력. "
        + "근거가 모호하면 '[근거 필요: XX 통계]' 같은 placeholder 한 줄.\n\n"
        + "[주장]\n"
    ),
    "recovery_or_integration_note": (
        _FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 변경/삭제된 영역을 원문 흐름에 맞게 복구한 *완성된 문장 또는 단락*을 본문으로 출력.\n\n"
        + "[변경 영역]\n"
    ),
    "long_paragraph_split": (
        _FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 단락을 2~3개의 더 짧은 단락으로 나눈 *최종 결과*만 본문으로 출력. "
        + "분리 기준은 설명 부분에 짧게.\n\n"
        + "[원문]\n"
    ),
}


_LEAD_IN_NATIVE: dict[str, str] = {
    "next_sentence": "",  # uses ghostwrite system prompt — already optimized
    "paragraph_rewrite": (
        "[과업] 아래 단락을 의미 보존하며 다시 쓰되, 응답은 다시 쓴 단락 전체만 출력. "
        "어떠한 머리말/꼬리말/설명도 포함하지 말 것 (inline-diff renderer가 원문 통째로 교체함).\n\n"
        "[원문]\n"
    ),
    "local_copyedit": (
        "[과업] 아래 문장의 오류만 고친 한 문장(또는 문단)을 출력. 다른 어떤 텍스트도 추가하지 말 것.\n\n"
        "[원문]\n"
    ),
    "logic_flow_review": (
        "[과업] 아래 단락에서 핵심 문제 한 가지를 *반영한 단락 전체*만 출력. 설명/머리말 금지.\n\n"
        "[원문]\n"
    ),
    "evidence_or_citation_prompt": (
        "[과업] 다음 주장 직후에 삽입할 *근거 한 줄만* 출력 (또는 '[근거 필요]' placeholder 한 줄). "
        "어떤 설명도 덧붙이지 말 것.\n\n"
        "[주장]\n"
    ),
    "recovery_or_integration_note": (
        "[과업] 아래 변경 영역을 원문 흐름에 맞춰 복구한 *문장/단락만* 출력. 머리말/설명 금지.\n\n"
        "[변경 영역]\n"
    ),
    "long_paragraph_split": (
        "[과업] 아래 단락을 2~3개로 나눈 *완성된 단락 모음만* 출력. 설명/머리말 금지.\n\n"
        "[원문]\n"
    ),
}


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
            # IMPORTANT: use the editor's raw prefix/suffix here, NOT the
            # context_selector's reconstructed (prev_sentence + current_fragment).
            # The reconstruction often produces too-short text (e.g. when the
            # paragraph has only one sentence) which then fails ChatAgent's
            # ``_is_continuation_moment`` (min ~8 chars after normalization) and
            # the ghost LLM silently declines. Falling back to the bundle parts
            # only when raw prefix is unavailable (e.g. screen-bridge captures).
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
            yield from self._ghostwrite_iter(
                prefix,
                suffix,
                max_tokens=self.max_tokens_ghost,
                use_workspace=use_workspace,
            )
            return

        lead_in_table = _LEAD_IN_NATIVE if surface_is_native else _LEAD_IN_EXTERNAL
        lead_in = lead_in_table.get(task.task_type, "")
        body = self._compose_body(task=task, context=context, native=surface_is_native)
        text = f"{lead_in}{body}".strip()
        action = _ASSIST_ACTION.get(task.task_type, "rewrite")
        yield from self._editor_assist_iter(
            action,
            text,
            max_tokens=self.max_tokens_assist,
            use_workspace=use_workspace,
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
