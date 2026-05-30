"""Memory가 system msg에 끼우는 block + 내부 호출용 prompt."""


# working_context section format.
WORKING_CONTEXT_BLOCK_TEMPLATE = """\
### Working Context ###
{working_context}
"""


# evicted FIFO를 압축한 summary section format.
FIFO_SUMMARY_BLOCK_TEMPLATE = """\
### Recent Conversation Summary ###
{fifo_summary}
"""


# recall search results injected into system context.
RECALL_CONTEXT_BLOCK_TEMPLATE = """\
### Retrieved Recall Context ###
{recall_context}
"""


# archival search results injected into system context.
ARCHIVAL_CONTEXT_BLOCK_TEMPLATE = """\
### Retrieved Archival Context ###
{archival_context}
"""


# FIFO 누적이 warning_ratio 초과 시 추가되는 경고.
MEMORY_PRESSURE_SYSTEM_MESSAGE = """\
### Memory Pressure Alert ###
The context window is close to capacity. Important information should be compacted
into working context or summarized before older FIFO messages are evicted.
"""


# Summarizer가 raw_llm으로 호출하는 recursive summary prompt.
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


# working_context 재작성용 prompt.
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
