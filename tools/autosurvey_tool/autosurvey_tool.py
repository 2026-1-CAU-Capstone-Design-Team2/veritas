from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.tool import BaseTool, ToolResult


class AutoSurveyTool(BaseTool):
    """LLM-facing wrapper around the AutoSurvey workflow.

    The workflow remains owned by workflows.autosurvey_workflow.AutoSurveyWorkflow.
    This tool is only an adapter that lets chat agents invoke that workflow as one
    high-level capability. It does not expose AutoSurvey's internal tools directly.
    """

    CHAT_MAX_DOCS_CAP = 5

    def __init__(
        self,
        schema: dict[str, Any],
        *,
        workflow,
        rag_service=None,
        run_store_service=None,
        max_docs_cap: int = CHAT_MAX_DOCS_CAP,
    ) -> None:
        super().__init__(schema=schema)
        self.workflow = workflow
        self.rag_service = rag_service
        self.run_store_service = run_store_service or getattr(workflow, "run_store_service", None)
        self.max_docs_cap = max(1, min(int(max_docs_cap), self.CHAT_MAX_DOCS_CAP))

    @property
    def name(self) -> str:
        return "autosurvey"

    def run(
        self,
        request: str | None = None,
        max_docs: int | None = None,
        force_plan: bool = True,
        overwrite_summaries: bool = False,
        **_: Any,
    ) -> ToolResult:
        request_text = str(request or "").strip()
        if not request_text:
            return ToolResult(success=False, error="`request` is required for autosurvey.")

        effective_max_docs = self._cap_max_docs(max_docs)
        original_max_docs = getattr(self.workflow, "max_docs", None)
        original_scout_docs = getattr(self.workflow, "scout_docs", None)
        existing_kept_count = self._kept_record_count()
        effective_total_max_docs = existing_kept_count + effective_max_docs

        try:
            self.workflow.max_docs = effective_total_max_docs
            self.workflow.scout_docs = max(
                1,
                min(int(getattr(self.workflow, "scout_docs", 3)), effective_max_docs),
            )

            result = self.workflow.run_all(
                user_request=request_text,
                force_plan=bool(force_plan),
                overwrite_summaries=bool(overwrite_summaries),
            )

            indexed_chunks = None
            if self.rag_service is not None and self.run_store_service is not None:
                clean_md_dir = getattr(self.run_store_service, "clean_md_dir", None)
                index_path = getattr(self.run_store_service, "index_path", None)
                if clean_md_dir is not None:
                    indexed_chunks = self.rag_service.index_autosurvey_output(
                        clean_md_dir=Path(clean_md_dir),
                        index_path=Path(index_path) if index_path is not None else None,
                        clear_first=True,
                    )

            final_result = result.get("final_result", {}) if isinstance(result, dict) else {}
            final_path = final_result.get("final_path") if isinstance(final_result, dict) else None
            excerpt = self._read_excerpt(final_path)

            data = {
                "request": request_text,
                "max_docs": effective_max_docs,
                "existing_kept_docs_before_run": existing_kept_count,
                "effective_total_max_docs": effective_total_max_docs,
                "final_path": str(final_path) if final_path else None,
                "indexed_chunks": indexed_chunks,
                "final_report_excerpt": excerpt,
                "workflow_result": self._compact_workflow_result(result),
            }
            content = self._build_content(data)
            return ToolResult(success=True, content=content, data=data)
        except Exception as e:
            return ToolResult(success=False, error=f"AutoSurvey workflow failed: {e}")
        finally:
            if original_max_docs is not None:
                self.workflow.max_docs = original_max_docs
            if original_scout_docs is not None:
                self.workflow.scout_docs = original_scout_docs

    def _cap_max_docs(self, value: int | None) -> int:
        if value is None:
            requested = self.max_docs_cap
        else:
            try:
                requested = int(value)
            except Exception:
                requested = self.max_docs_cap
        return max(1, min(requested, self.max_docs_cap))

    def _kept_record_count(self) -> int:
        try:
            records = self.workflow.run_store_service.list_non_duplicate_records()
            return len(records)
        except Exception:
            return 0

    def _read_excerpt(self, final_path: Any, *, max_chars: int = 6000) -> str:
        if not final_path:
            return ""
        try:
            path = Path(str(final_path))
            if not path.exists() or not path.is_file():
                return ""
            return path.read_text(encoding="utf-8")[:max_chars].strip()
        except Exception:
            return ""

    def _compact_workflow_result(self, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {}
        iterations = result.get("iterations", [])
        return {
            "grounding": result.get("grounding"),
            "initial_plan": result.get("initial_plan"),
            "active_plan": result.get("active_plan"),
            "iteration_count": len(iterations) if isinstance(iterations, list) else None,
        }

    def _build_content(self, data: dict[str, Any]) -> str:
        final_path = data.get("final_path") or "(not available)"
        excerpt = data.get("final_report_excerpt") or "(No final report excerpt available.)"
        indexed = data.get("indexed_chunks")
        indexed_text = "unknown" if indexed is None else str(indexed)
        return (
            "AutoSurvey completed.\n"
            f"Final report: {final_path}\n"
            f"Indexed chunks: {indexed_text}\n\n"
            "Final report excerpt:\n"
            f"{excerpt}"
        )


__all__ = ["AutoSurveyTool"]
