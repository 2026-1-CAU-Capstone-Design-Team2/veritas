"""Editor (standalone document writer) prompts.

The editor window's three AI surfaces keep all their prompt text here so it
lives in :mod:`core.prompts` (alongside chat / verify / autosurvey) and is never
sent from the client — ``api.services.editor_service`` only fills the ``{...}``
slots and adds the conditional [참고 자료] / suffix blocks.

* :data:`SUGGEST_SYSTEM_PROMPT` / :data:`SUGGEST_USER_TEMPLATE` /
  :data:`SUGGEST_SUFFIX_BLOCK_TEMPLATE` — inline ghost-writing.
* :data:`ASSIST_SYSTEM_PROMPTS` / :data:`ASSIST_GROUNDED_USER_TEMPLATE` —
  quick-action transforms (rewrite / summarize / polish / grammar / continue).
* :data:`CHAT_SYSTEM_TEMPLATE` / :data:`CHAT_SOURCES_BLOCK_TEMPLATE` —
  document-grounded chat.
* :data:`REFERENCE_BLOCK_TEMPLATE` — shared [참고 자료] block prepended when
  workspace RAG grounding is available.
"""


# --- shared ----------------------------------------------------------------

# Prepended to a user prompt when workspace RAG grounding is available.
REFERENCE_BLOCK_TEMPLATE = "[참고 자료]\n{context}\n\n"


# --- inline ghost-writing --------------------------------------------------

SUGGEST_SYSTEM_PROMPT = (
    "당신은 한국어 문서 작성 보조기입니다. 사용자가 작성 중인 글의 커서 위치에서 "
    "자연스럽게 이어질 다음 텍스트만 출력하세요. 설명, 따옴표, 코드펜스(```), 머리말 없이 "
    "이어질 본문만 1~2문장 이내로 간결하게 작성합니다. [참고 자료]는 지금 작성 중인 "
    "내용과 관련될 때만 사실 근거로 활용하되 그대로 복사하지 말고, 이미 작성된 문장을 "
    "반복하지 마세요. [참고 자료]가 현재 쓰는 주제와 무관하거나 자연스럽게 이어 쓸 내용이 "
    "없으면, 억지로 만들지 말고 다른 말 없이 정확히 [NO_SUGGESTION] 만 출력하세요."
)

# Appended after the prefix when the cursor has text following it.
SUGGEST_SUFFIX_BLOCK_TEMPLATE = "\n\n[커서 뒤 내용]\n{suffix}"

# {reference} is "" or a filled REFERENCE_BLOCK_TEMPLATE; {suffix_block} is ""
# or a filled SUGGEST_SUFFIX_BLOCK_TEMPLATE.
SUGGEST_USER_TEMPLATE = (
    "{reference}[작성 중인 내용]\n{prefix}{suffix_block}"
    "\n\n[커서 위치에 이어서 작성할 텍스트만 출력]"
)


# --- quick-action transforms ----------------------------------------------

ASSIST_SYSTEM_PROMPTS = {
    "rewrite": (
        "다음 텍스트를 의미를 유지한 채 더 명확하고 자연스럽게 다시 써 주세요. "
        "[참고 자료]가 있으면 사실 근거로 활용하세요. 설명이나 따옴표 없이 결과 텍스트만 출력하세요."
    ),
    "summarize": (
        "다음 텍스트의 핵심만 남겨 간결하게 요약해 주세요. 1~3문장. 설명 없이 요약문만 출력하세요."
    ),
    "polish": (
        "다음 텍스트의 문장을 의미를 유지한 채 매끄럽고 간결하게 다듬어 주세요. "
        "설명 없이 다듬은 결과만 출력하세요."
    ),
    "grammar": (
        "다음 텍스트의 맞춤법과 문법 오류를 교정해 주세요. "
        "내용을 추가하지 말고, 설명 없이 교정된 전체 텍스트만 출력하세요."
    ),
    "continue": (
        "다음 글에 자연스럽게 이어질 다음 문단을 작성해 주세요. [참고 자료]가 있으면 사실 "
        "근거로 활용하세요. 설명 없이 이어질 본문만 출력하고, 이미 쓴 문장을 반복하지 마세요."
    ),
}

# Wraps the target text with a grounding block for the quick actions that
# benefit from workspace context.
ASSIST_GROUNDED_USER_TEMPLATE = "[참고 자료]\n{context}\n\n[대상 텍스트]\n{text}"


# --- document chat ---------------------------------------------------------

CHAT_SYSTEM_TEMPLATE = (
    "당신은 사용자의 문서 작성을 돕는 한국어 어시스턴트입니다. 아래 [현재 문서]와 "
    "[연결된 자료]를 근거로 질문에 답하거나 수정/개선을 제안하세요. 필요하면 마크다운으로 "
    "답하되 간결하게 작성하세요.\n\n[현재 문서]\n{doc}"
)

# Appended to the chat system prompt when workspace RAG grounding is available.
CHAT_SOURCES_BLOCK_TEMPLATE = "\n\n[연결된 자료]\n{context}"


__all__ = [
    "ASSIST_GROUNDED_USER_TEMPLATE",
    "ASSIST_SYSTEM_PROMPTS",
    "CHAT_SOURCES_BLOCK_TEMPLATE",
    "CHAT_SYSTEM_TEMPLATE",
    "REFERENCE_BLOCK_TEMPLATE",
    "SUGGEST_SUFFIX_BLOCK_TEMPLATE",
    "SUGGEST_SYSTEM_PROMPT",
    "SUGGEST_USER_TEMPLATE",
]
