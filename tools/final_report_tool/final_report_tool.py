from __future__ import annotations

import json
from typing import Any

from core.prompts import FINAL_PROMPT
from tools.tool import BaseTool, ToolResult


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

            prompt = json.dumps(
                {
                    "user_request": user_request,
                    "plan": plan,
                    "kept_doc_count": len([r for r in records if r.duplicate_of is None]),
                    "duplicate_count": len([r for r in records if r.duplicate_of is not None]),
                    "batch_summaries": batch_summaries,
                },
                ensure_ascii=False,
                indent=2,
            )

            final_markdown = self._llm.ask(FINAL_PROMPT, prompt, reasoning=True)
            self._run_store_service.save_final_report(final_markdown)

            return ToolResult(
                success=True,
                content=f"Final report written to {self._run_store_service.final_path}",
                data={"final_path": str(self._run_store_service.final_path)},
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to write final report: {e}")