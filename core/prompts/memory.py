"""Memory Runtime이 사용하는 prompt template.

기존 ``core/prompts/chat.py``, ``draft.py``, ``verify.py``, ``cleanup.py``를
직접 수정하지 않고, memory envelope과 recursive summary 등 memory-only
prompt만 여기에 둔다.

INTERACTIVE profile은 working + FIFO summary + recall + archival 전체를
envelope에 넣고, SCREEN_GROUNDED profile은 working_context만 노출하기
위해 별도 template를 제공한다 — screen 호출은 prompt 안에 이미 KB
context를 포함하므로 recall_context를 또 주입하면 grounding이 충돌한다.
"""


# INTERACTIVE 호출용 표준 envelope.
# user_prompt 앞에 prepend되어 LLM이 working context, 최근 대화 요약,
# 관련 recall, archival memory를 한눈에 볼 수 있게 한다.
MEMORY_ENVELOPE_TEMPLATE = """\
[MEMORY CONTEXT]

Working Context:
{working_context}

Recent Context Summary:
{fifo_summary}

Relevant Recall:
{recall_context}

Relevant Archival Memory:
{archival_context}

[/MEMORY CONTEXT]
"""


# SCREEN_GROUNDED 호출용 축약 envelope.
# screen intervention prompt는 이미 _screen_knowledge_context로 KB를 통합한
# 상태이므로 working_context만 추가로 노출한다. recall/archival을 또
# 끼우면 KB grounding과 충돌해 답변 품질이 떨어진다.
SCREEN_MEMORY_ENVELOPE_TEMPLATE = """\
[MEMORY CONTEXT]

Working Context:
{working_context}

[/MEMORY CONTEXT]
"""


# 누적 FIFO token이 warning_ratio를 넘었을 때 system prompt에 더해 흘려보내는
# 경고문. LLM에게 "지금 중요한 사실은 working context로 압축해두라"는 힌트.
# 1차 구현에서는 logging만 하고, 2차에서 prompt에 prepend하도록 확장한다.
MEMORY_PRESSURE_SYSTEM_MESSAGE = """\
System Alert: Memory pressure detected.
The context window is close to capacity. Important information should be compacted
into working context or summarized before older FIFO messages are evicted.
"""


# FIFO flush 시 evicted 메시지들을 recursive summary로 압축할 때 쓰는 prompt.
# MemorySummarizer는 raw_llm을 직접 호출 — MemoryAwareLLMClient를 우회해야
# Runtime.prepare()가 다시 호출되어 무한 재귀가 생기지 않는다.
MEMORY_SUMMARY_PROMPT = """\
Summarize the following evicted conversation/event messages into a compact recursive summary.

Rules:
- Preserve facts, decisions, user preferences, and unresolved tasks.
- Remove repetition and transient details.
- Do not invent new facts.
- Keep the summary concise.

Previous summary:
{previous_summary}

Evicted messages:
{evicted_messages}
"""


# Working context를 주기적으로 재작성할 때 쓰는 prompt.
# 1차 구현에서는 자동 트리거하지 않음 — WorkingContextRewriter를 추가하는
# 2차 마일스톤에서 사용한다.
WORKING_CONTEXT_REWRITE_PROMPT = """\
Rewrite the working context so it remains compact and useful.

Rules:
- Keep stable facts only.
- Remove stale or contradicted facts.
- Preserve user/project preferences when explicit.
- Do not include uncertain observations as facts.

Current working context:
{working_context}

New candidate facts:
{candidate_facts}
"""
