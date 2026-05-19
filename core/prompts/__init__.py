"""All Veritas LLM prompts, split by pipeline.

Originally a single 614-line ``core/prompts.py`` holding 23 prompt
constants. Split into four sub-modules so each prompt is easy to find
and edit without scrolling through the unrelated ones:

* :mod:`core.prompts.autosurvey` — survey pipeline (term grounding, planner,
  replanner, doc / batch / final summaries)
* :mod:`core.prompts.cleanup` — raw_md → clean_md document cleanup
* :mod:`core.prompts.verify` — verify pipeline (flow planner, reliability
  judge)
* :mod:`core.prompts.chat` — chat agent (system identity, RAG, query
  rewrite, tool-selection chat, screen-intervention)

Every existing call site of the form ``from core.prompts import X`` keeps
working because this module re-exports the full set at the package
boundary. Future prompts should land in whichever sub-module's concern
they match — adding a new file or shuffling between files is just a
matter of moving the constant and updating this re-export list.
"""

from .autosurvey import (
    BATCH_SUMMARY_PROMPT,
    DOC_CHUNK_NOTES_PROMPT,
    DOC_SUMMARY_PROMPT,
    DOC_SUMMARY_REDUCE_PROMPT,
    FINAL_PROMPT,
    INITIAL_PLANNER_PROMPT,
    PLANNER_PROMPT,
    REPLANNER_PROMPT,
    TERM_GROUNDING_PROMPT,
)
from .chat import (
    QUERY_REWRITE_PROMPT,
    QUERY_REWRITE_SYSTEM_PROMPT,
    RAG_EMPTY_CONTEXT_PROMPT_TEMPLATE,
    RAG_SYSTEM_PROMPT,
    RAG_USER_PROMPT_TEMPLATE,
    SCREEN_INTERVENTION_SYSTEM_PROMPT,
    SCREEN_INTERVENTION_USER_PROMPT_TEMPLATE,
    SYSTEM_PROMPT,
    TOOL_CHAT_FINAL_PROMPT_TEMPLATE,
    TOOL_CHAT_SYSTEM_PROMPT,
    TOOL_CHAT_USER_PROMPT_TEMPLATE,
)
from .cleanup import DOCUMENT_CLEANUP_PROMPT
from .verify import RELIABILITY_JUDGE_PROMPT, VERIFY_FLOW_PLANNER_PROMPT


__all__ = [
    # autosurvey
    "BATCH_SUMMARY_PROMPT",
    "DOC_CHUNK_NOTES_PROMPT",
    "DOC_SUMMARY_PROMPT",
    "DOC_SUMMARY_REDUCE_PROMPT",
    "FINAL_PROMPT",
    "INITIAL_PLANNER_PROMPT",
    "PLANNER_PROMPT",
    "REPLANNER_PROMPT",
    "TERM_GROUNDING_PROMPT",
    # chat
    "QUERY_REWRITE_PROMPT",
    "QUERY_REWRITE_SYSTEM_PROMPT",
    "RAG_EMPTY_CONTEXT_PROMPT_TEMPLATE",
    "RAG_SYSTEM_PROMPT",
    "RAG_USER_PROMPT_TEMPLATE",
    "SCREEN_INTERVENTION_SYSTEM_PROMPT",
    "SCREEN_INTERVENTION_USER_PROMPT_TEMPLATE",
    "SYSTEM_PROMPT",
    "TOOL_CHAT_FINAL_PROMPT_TEMPLATE",
    "TOOL_CHAT_SYSTEM_PROMPT",
    "TOOL_CHAT_USER_PROMPT_TEMPLATE",
    # cleanup
    "DOCUMENT_CLEANUP_PROMPT",
    # verify
    "RELIABILITY_JUDGE_PROMPT",
    "VERIFY_FLOW_PLANNER_PROMPT",
]
