"""Final report synthesis tool.

The final report is synthesized by ONE LLM call from the original user request,
a short plan summary, run stats, and the batch summaries. Two structural
safeguards keep internal pipeline state out of the visible report:

* The LLM input is rendered as **human-readable sectioned text**
  (:func:`render_final_report_input`), never a raw ``json.dumps`` blob. A model
  cannot echo a JSON payload it was never given. ``search_queries`` and the full
  ``plan`` object are deliberately omitted; only an allowlist of plan fields is
  passed.
* After generation a deterministic **leakage guard**
  (:func:`repair_user_request_section_if_leaked`) replaces the ``## User
  Request`` section with the original request if a JSON payload still leaked
  into it — no extra LLM call. This guarantees the contract regardless of model
  (the ``runs/Multi_Armed_Bandit-2`` case dumped the whole input JSON there).

Both helpers are pure functions so they unit-test without an LLM.
"""

from __future__ import annotations

import re
from typing import Any

from core.latex_cleanup import clean_latex_in_markdown
from core.prompts import FINAL_PROMPT
from core.report_markdown_normalizer import normalize_final_report_markdown
from tools.tool import BaseTool, ToolResult


# Allowlist caps for the plan summary so a runaway plan can't pad the prompt.
_MAX_MUST_COVER = 6
_MAX_KEYWORDS = 12

# Payload keys that must never appear in a user-facing ``## User Request``
# section — their presence means the raw input JSON leaked through.
_LEAK_KEYS = ('"batch_summaries"', '"search_queries"', '"user_request"', '"plan"')
_USER_REQUEST_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+User Request\s*$", re.IGNORECASE)
_ANY_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")


def render_final_report_input(
    user_request: str,
    plan: Any,
    kept_doc_count: int,
    duplicate_count: int,
    batch_summaries: list[str],
) -> str:
    """Render the final-report LLM input as labelled, human-readable sections.

    Emits the original request, an allowlisted plan summary (topic / goal /
    must-cover / keywords — NOT search queries or the raw plan), short run stats,
    and the batch summaries as Markdown. No JSON, so there is no payload blob for
    the model to copy into the report.
    """
    plan = plan if isinstance(plan, dict) else {}
    topic = str(plan.get("topic") or "").strip()
    goal = str(plan.get("goal") or "").strip()
    must_cover = [str(x).strip() for x in (plan.get("must_cover") or []) if str(x).strip()]
    keywords = [str(x).strip() for x in (plan.get("keywords") or []) if str(x).strip()]

    lines: list[str] = ["Original User Request:", str(user_request or "").strip(), ""]

    if topic or goal or must_cover or keywords:
        lines.append("Research Plan Summary:")
        if topic:
            lines.append(f"- Topic: {topic}")
        if goal:
            lines.append(f"- Goal: {goal}")
        if must_cover:
            lines.append("- Must cover:")
            lines.extend(f"    - {item}" for item in must_cover[:_MAX_MUST_COVER])
        if keywords:
            lines.append("- Keywords: " + ", ".join(keywords[:_MAX_KEYWORDS]))
        lines.append("")

    lines.append("Run Stats:")
    lines.append(f"- Kept documents: {kept_doc_count}")
    lines.append(f"- Duplicate documents: {duplicate_count}")
    lines.append("")

    lines.append("Batch Summaries:")
    for index, summary in enumerate(batch_summaries or [], start=1):
        lines.append(f"--- Batch {index} ---")
        lines.append(str(summary).strip())

    return "\n".join(lines).strip() + "\n"


def _section_is_leaked(body: str) -> bool:
    stripped = body.lstrip()
    if stripped.startswith("{") or stripped.startswith("```json"):
        return True
    return any(key in body for key in _LEAK_KEYS)


def repair_user_request_section_if_leaked(
    final_markdown: str, user_request: str
) -> tuple[str, bool]:
    """If the ``## User Request`` section leaked the input JSON, restore it.

    Replaces only that one section's body with the original request as a
    blockquote; every other section (Executive Summary, math, the Source Notes
    table, …) is left byte-identical. Pure; returns ``(markdown, was_repaired)``.
    """
    lines = final_markdown.split("\n")
    start = next(
        (i for i, line in enumerate(lines) if _USER_REQUEST_HEADING_RE.match(line)),
        None,
    )
    if start is None:
        return final_markdown, False
    end = next(
        (i for i in range(start + 1, len(lines)) if _ANY_HEADING_RE.match(lines[i])),
        len(lines),
    )
    body = "\n".join(lines[start + 1 : end])
    if not _section_is_leaked(body):
        return final_markdown, False

    request = str(user_request or "").strip()
    quoted = "\n".join(("> " + ln) if ln.strip() else ">" for ln in request.split("\n"))
    new_section = ["", quoted, ""]
    repaired = lines[: start + 1] + new_section + lines[end:]
    return "\n".join(repaired), True


class FinalReportTool(BaseTool):
    def __init__(self, schema: dict[str, Any], llm, run_store_service) -> None:
        super().__init__(schema=schema)
        self._llm = llm
        self._run_store_service = run_store_service

    @property
    def name(self) -> str:
        return "final_report"

    def run(self, user_request: str | None = None) -> ToolResult:
        try:
            if not user_request:
                user_request = self._run_store_service.load_request()

            plan = self._run_store_service.load_plan()
            records = self._run_store_service.load_records()
            batch_summaries = self._run_store_service.load_all_batch_summaries()

            prompt = render_final_report_input(
                user_request=user_request,
                plan=plan,
                kept_doc_count=len([r for r in records if r.duplicate_of is None]),
                duplicate_count=len([r for r in records if r.duplicate_of is not None]),
                batch_summaries=batch_summaries,
            )

            # The final report is the survey's visible synthesis artifact —
            # keep API reasoning models at their default (medium) effort.
            final_markdown = self._llm.ask(
                FINAL_PROMPT, prompt, reasoning=True, reasoning_effort="medium"
            )
            # Local llama-server models double-escape backslashes inside math
            # blocks (``\\\\mathcal{L}`` instead of ``\\mathcal{L}``), which
            # the markdown renderer then parses as a forced newline followed
            # by literal text ``mathcal{L}`` — every equation breaks. Run a
            # rule-based cleanup over ``$$…$$`` / ``$…$`` / ``\\[…\\]`` /
            # ``\\(…\\)`` blocks before persisting so users see clean math.
            final_markdown = clean_latex_in_markdown(final_markdown)
            # Deterministic leakage guard: if the model echoed the internal
            # input JSON into ``## User Request``, replace that section with the
            # original request. No extra LLM call; only that section changes.
            final_markdown, leaked = repair_user_request_section_if_leaked(
                final_markdown, user_request
            )
            if leaked:
                print("[final_report] repaired leaked JSON in ## User Request section")
            # Repair the ``## Source Notes`` table only — bullet-prefixed rows,
            # a missing header/separator, or bare ``doc_1`` ids otherwise break
            # the table rendering. Pure + idempotent; the rest is untouched.
            final_markdown = normalize_final_report_markdown(final_markdown)
            self._run_store_service.save_final_report(final_markdown)

            return ToolResult(
                success=True,
                content=f"Final report written to {self._run_store_service.final_path}",
                data={"final_path": str(self._run_store_service.final_path)},
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to write final report: {e}")


__all__ = [
    "FinalReportTool",
    "render_final_report_input",
    "repair_user_request_section_if_leaked",
]
