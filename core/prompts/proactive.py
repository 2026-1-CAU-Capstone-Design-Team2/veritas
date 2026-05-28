"""Prompt templates for the proactive intervention pipeline.

Centralized here (rather than alongside ``services/proactive/generator.py``)
so all of Veritas's LLM prompt copy lives under ``core/prompts/`` — the
generator is just a router that fetches the right template by task type
and surface.

Two template families:

* :data:`LEAD_IN_EXTERNAL` — sent as the editor_assist prefix when the
  rendered surface is an external Windows app card (the bandit-era
  SuggestionCard). Each lead-in carries the bold "본문 / 설명:" format
  contract because ``SuggestionCard`` splits the model output on the
  literal ``설명:`` marker to populate its body / note labels.

* :data:`LEAD_IN_NATIVE` — for the future native ``native_inline_diff`` /
  ``native_inline_marker`` renderers. These forbid wrapping prose entirely:
  the renderer will replace the underlying paragraph wholesale, so any
  head/tail commentary would end up inside the user's document.

The native ``next_sentence`` path does NOT use any lead-in here — it goes
through ChatAgent.iter_ghostwrite, whose system prompt
(``SUGGEST_SYSTEM_PROMPT`` in :mod:`core.prompts.editor`) is already
optimized for pure continuation with no meta-commentary.
"""
from __future__ import annotations


# ----------------------------------------------------------- external

# External-card format contract. Keep in sync with the
# SuggestionCard's "설명:" split in document_assist_window.py.
FORMAT_CONTRACT_EXTERNAL: str = (
    "[응답 형식 — 반드시 준수]\n"
    "1) 첫 부분: 사용자가 그대로 복사-붙여넣기할 본문만. 메타 발언/이유 금지.\n"
    "2) 빈 줄 후 '설명:' 으로 시작하는 한두 줄로 이유/포인트를 별도 작성.\n"
    "[금지] 본문 안에 '추천합니다', '~하는 것이 좋아 보입니다', '권장' 같은 메타 발언.\n"
    "[금지] 본문 앞 머리말 ('아래와 같이...', '다음 문장이 적절합니다:' 등).\n"
)


LEAD_IN_EXTERNAL: dict[str, str] = {
    "next_sentence": (
        FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 문맥 다음에 이어질 한 문장을 본문으로 작성.\n\n"
        + "[직전 문맥]\n"
    ),
    "paragraph_rewrite": (
        FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 단락을 의미 보존하며 더 명확하고 자연스럽게 다시 써서 본문으로 출력. 새로운 사실 추가 금지.\n\n"
        + "[원문 단락]\n"
    ),
    "local_copyedit": (
        FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 문장/문단의 문법, 맞춤법, 반복 표현만 최소 수정한 *완전한 문장(들)*을 본문으로 출력.\n\n"
        + "[원문]\n"
    ),
    "logic_flow_review": (
        FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 단락에서 가장 시급한 흐름 문제 한 가지를 *고친 결과 단락*을 본문으로 출력. "
        + "원인은 설명 부분에만.\n\n"
        + "[원문]\n"
    ),
    "evidence_or_citation_prompt": (
        FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 주장에 필요한 근거 한 줄을 본문으로 출력. "
        + "근거를 단정할 수 없으면 '[근거 필요]' placeholder 한 줄.\n\n"
        + "[주장]\n"
    ),
    "recovery_or_integration_note": (
        FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 변경/삭제된 영역을 원문 흐름에 맞게 복구한 *완성된 문장 또는 단락*을 본문으로 출력.\n\n"
        + "[변경 영역]\n"
    ),
    "long_paragraph_split": (
        FORMAT_CONTRACT_EXTERNAL
        + "[과업] 아래 단락을 2~3개의 더 짧은 단락으로 나눈 *최종 결과*만 본문으로 출력. "
        + "분리 기준은 설명 부분에 짧게.\n\n"
        + "[원문]\n"
    ),
}


# ----------------------------------------------------------- native (future)

LEAD_IN_NATIVE: dict[str, str] = {
    # next_sentence on native is handled by ChatAgent.iter_ghostwrite —
    # see core.prompts.editor.SUGGEST_SYSTEM_PROMPT.
    "next_sentence": "",
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


def lead_in_for(*, task_type: str, surface_is_native: bool) -> str:
    """Look up the appropriate lead-in for ``(task_type, surface)``.

    Returns an empty string for unknown task types (the generator will then
    pass the body straight through to editor_assist's own template).
    """
    table = LEAD_IN_NATIVE if surface_is_native else LEAD_IN_EXTERNAL
    return table.get(task_type, "")


__all__ = [
    "FORMAT_CONTRACT_EXTERNAL",
    "LEAD_IN_EXTERNAL",
    "LEAD_IN_NATIVE",
    "lead_in_for",
]
