"""system message 조립 헬퍼."""

from __future__ import annotations

from core.prompts.gateway import COMMON_GATEWAY_INSTRUCTIONS, JSON_STRICT_SUFFIX
from core.prompts.memory import (
    FIFO_SUMMARY_BLOCK_TEMPLATE,
    MEMORY_PRESSURE_SYSTEM_MESSAGE,
    WORKING_CONTEXT_BLOCK_TEMPLATE,
)


def assemble_system_message(
    *,
    task_instruction: str,
    working_context: str = "",
    fifo_summary: str = "",
    memory_pressure: bool = False,
    json_strict: bool = False,
) -> str:
    """공통 instruction + task + 옵션 섹션들을 하나의 system content로 합친다."""
    parts: list[str] = [COMMON_GATEWAY_INSTRUCTIONS.strip()]

    task = (task_instruction or "").strip()
    if task:
        parts.append(task)

    wc = (working_context or "").strip()
    if wc:
        parts.append(WORKING_CONTEXT_BLOCK_TEMPLATE.format(working_context=wc).strip())

    summary = (fifo_summary or "").strip()
    if summary:
        parts.append(FIFO_SUMMARY_BLOCK_TEMPLATE.format(fifo_summary=summary).strip())

    if memory_pressure:
        parts.append(MEMORY_PRESSURE_SYSTEM_MESSAGE.strip())

    if json_strict:
        parts.append(JSON_STRICT_SUFFIX.strip())

    return "\n\n".join(parts)
