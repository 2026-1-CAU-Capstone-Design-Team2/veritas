from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from agent import ChatAgent
from llm.llama_server_llm import LLMClient
from tools.autosurvey_tool import AutoSurveyTool
from tools.loader import build_registry, load_schema
from workflows import AutoSurveyWorkflow


class AgentRuntime:
    def __init__(self) -> None:
        self.output_root = Path(os.getenv("VERITAS_OUTPUT_DIR", "runs")).expanduser().resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._cleanup_pending_dirs()
        self.output_dir = self.output_root / "api"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.llm = LLMClient(
            host=os.getenv("VERITAS_LLM_HOST", "127.0.0.1"),
            port=int(os.getenv("VERITAS_LLM_PORT", "8080")),
            embed_host=os.getenv("VERITAS_EMBED_HOST") or None,
            embed_port=int(os.getenv("VERITAS_EMBED_PORT", "8081")),
            trace_latency=os.getenv("VERITAS_TRACE_LATENCY", "1") != "0",
        )
        self.registry, self.run_store_service, self.rag_service = build_registry(
            llm=self.llm,
            run_root=self.output_dir,
            batch_size=int(os.getenv("VERITAS_BATCH_SIZE", "5")),
            max_context=int(os.getenv("VERITAS_MAX_CONTEXT", "16384")),
            enable_screen_context=os.getenv("VERITAS_ENABLE_SCREEN_CONTEXT", "1") != "0",
            screen_interval_sec=float(os.getenv("VERITAS_SCREEN_INTERVAL", "5.0")),
            screen_debug_log=os.getenv("VERITAS_SCREEN_DEBUG", "0") == "1",
        )
        self.workflow = AutoSurveyWorkflow(
            registry=self.registry,
            run_store_service=self.run_store_service,
            max_docs=int(os.getenv("VERITAS_MAX_DOCS", "15")),
            collect_batch_size=int(os.getenv("VERITAS_BATCH_SIZE", "5")),
            scout_docs=int(os.getenv("VERITAS_SCOUT_DOCS", "3")),
        )
        self._register_autosurvey_tool()
        self.chat_agent = ChatAgent(
            llm=self.llm,
            rag_service=self.rag_service,
            tool_registry=self.registry,
            screen_debug=os.getenv("VERITAS_SCREEN_DEBUG", "0") == "1",
        )

    def _register_autosurvey_tool(self) -> None:
        if self.registry.has("autosurvey"):
            return
        schema_path = Path(__file__).resolve().parents[2] / "tools" / "autosurvey_tool" / "tool_schema.json"
        self.registry.register(
            AutoSurveyTool(
                schema=load_schema(schema_path),
                workflow=self.workflow,
                rag_service=self.rag_service,
                run_store_service=self.run_store_service,
                max_docs_cap=int(os.getenv("VERITAS_API_AUTOSURVEY_MAX_DOCS", "5")),
            )
        )

    def answer_chat(self, message: str, mode: str) -> str:
        if mode == "rag":
            self._ensure_rag_index(require_documents=False)
            return self.chat_agent.ask_rag(message, stream=False)
        return self.chat_agent.ask_auto(message, stream=False)

    def run_autosurvey(
        self,
        *,
        instruction: str,
        reference_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        request = self._append_reference_sites(instruction, reference_urls or [])
        workspace_name, grounding = self._grounding_workspace_from_request(request)
        workspace_dir = self._reserve_workspace_dir(workspace_name)
        registry, run_store_service, rag_service = build_registry(
            llm=self.llm,
            run_root=workspace_dir,
            batch_size=int(os.getenv("VERITAS_BATCH_SIZE", "5")),
            max_context=int(os.getenv("VERITAS_MAX_CONTEXT", "16384")),
            enable_screen_context=False,
        )
        workflow = AutoSurveyWorkflow(
            registry=registry,
            run_store_service=run_store_service,
            max_docs=int(os.getenv("VERITAS_MAX_DOCS", "15")),
            collect_batch_size=int(os.getenv("VERITAS_BATCH_SIZE", "5")),
            scout_docs=int(os.getenv("VERITAS_SCOUT_DOCS", "3")),
        )
        result = workflow.run_all(
            user_request=request,
            force_plan=True,
            overwrite_summaries=False,
            grounding=grounding,
        )
        indexed_chunks = None
        summary_dir = getattr(run_store_service, "summary_dir", None)
        index_path = getattr(run_store_service, "index_path", None)
        if summary_dir is not None:
            indexed_chunks = rag_service.index_autosurvey_output(
                summary_dir=Path(summary_dir),
                index_path=Path(index_path) if index_path is not None else None,
                clear_first=True,
            )

        final_path = run_store_service.final_path
        records = self._read_index_records(index_path)
        final_report = self._read_excerpt(final_path, max_chars=1_000_000)
        if not isinstance(final_report, str):
            final_report = ""
        return {
            "request": request,
            "workspace_id": workspace_dir.name,
            "workspace_name": workspace_dir.name,
            "max_docs": getattr(workflow, "max_docs", None),
            "final_path": str(final_path) if final_path else None,
            "indexed_chunks": indexed_chunks,
            "elapsed_seconds": round(time.perf_counter() - started_at, 3),
            "documents": self._document_summaries(records),
            "document_count": len(records),
            "non_duplicate_document_count": len(
                [record for record in records if not record.get("duplicate_of")]
            ),
            "final_report": final_report,
            "final_report_excerpt": final_report[:6000].strip(),
            "workflow_result": self._compact_workflow_result(result),
        }

    def _grounding_workspace_from_request(self, request: str) -> tuple[str, dict[str, Any] | None]:
        try:
            from tools.term_grounding_tool import TermGroundingTool

            schema_path = Path(__file__).resolve().parents[2] / "tools" / "term_grounding_tool" / "tool_schema.json"
            result = TermGroundingTool(
                schema=load_schema(schema_path),
                llm=self.llm,
            ).run(user_request=request, max_terms=8)
            payload = result.data if result.success and isinstance(result.data, dict) else {}
            terms = payload.get("grounded_terms", [])
            if isinstance(terms, list):
                for term in terms:
                    text = str(term or "").strip()
                    if text:
                        return text, payload
        except Exception:
            pass
        return "research", None

    def _reserve_workspace_dir(self, workspace_name: str) -> Path:
        safe_name = self._safe_workspace_name(workspace_name)
        target = self.output_root / safe_name
        if target.exists():
            suffix = 2
            while (self.output_root / f"{safe_name}-{suffix}").exists():
                suffix += 1
            target = self.output_root / f"{safe_name}-{suffix}"
        target.mkdir(parents=True, exist_ok=False)
        return target

    def _safe_workspace_name(self, name: str) -> str:
        text = re.sub(r"[^\w가-힣.-]+", "_", str(name or "").strip(), flags=re.UNICODE)
        text = text.strip("._-")
        return text[:80] or "research"

    def _cleanup_pending_dirs(self) -> None:
        try:
            root = self.output_root.resolve()
            for path in root.glob("_pending_*"):
                if not path.is_dir():
                    continue
                resolved = path.resolve()
                if root not in resolved.parents:
                    continue
                try:
                    shutil.rmtree(resolved)
                except Exception as e:
                    print(f"[workspace][cleanup][warn] could not remove {resolved}: {e}")
        except Exception as e:
            print(f"[workspace][cleanup][warn] pending cleanup skipped: {e}")

    def _ensure_rag_index(self, *, require_documents: bool) -> None:
        if self.rag_service.get_document_count() > 0:
            return

        summary_dir = self.run_store_service.summary_dir
        has_summary_docs = summary_dir.exists() and any(summary_dir.glob("doc_*.md"))
        indexed = 0
        if has_summary_docs:
            indexed = self.rag_service.index_autosurvey_output(
                summary_dir=summary_dir,
                index_path=self.run_store_service.index_path,
                clear_first=True,
            )
        elif self.output_dir.exists() and any(self.output_dir.rglob("*.md")):
            indexed = self.rag_service.index_all_markdown(self.output_dir, clear_first=True)

        if require_documents and indexed <= 0:
            raise RuntimeError("No indexed documents are available for RAG.")

    def _append_reference_sites(self, instruction: str, reference_urls: list[str]) -> str:
        sites = []
        for url in reference_urls:
            cleaned = str(url or "").strip()
            if cleaned:
                sites.append(f"site:{cleaned}")
        if not sites:
            return instruction
        return f"{instruction.strip()}\n\nReference sites: {' '.join(sites)}"

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

    def _read_index_records(self, index_path: Any) -> list[dict[str, Any]]:
        if not index_path:
            return []
        try:
            path = Path(str(index_path))
            if not path.exists() or not path.is_file():
                return []
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = payload.get("records", [])
            return [record for record in records if isinstance(record, dict)]
        except Exception:
            return []

    def _document_summaries(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        documents: list[dict[str, Any]] = []
        for record in records:
            url = str(record.get("final_url") or record.get("url") or "").strip()
            title = str(record.get("title") or url or record.get("doc_id") or "Untitled").strip()
            documents.append(
                {
                    "docId": str(record.get("doc_id") or ""),
                    "title": title,
                    "url": url,
                    "domain": str(record.get("domain") or ""),
                    "searchQuery": str(record.get("search_query") or ""),
                    "duplicateOf": record.get("duplicate_of"),
                }
            )
        return documents

    def _compact_workflow_result(self, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {}
        iterations = result.get("iterations", [])
        return {
            "grounding": result.get("grounding"),
            "initial_plan": result.get("initial_plan"),
            "active_plan": result.get("active_plan"),
            "iteration_count": len(iterations) if isinstance(iterations, list) else None,
            "final_result": result.get("final_result"),
        }


_runtime: AgentRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> AgentRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            try:
                _runtime = AgentRuntime()
            except Exception as e:
                raise HTTPException(
                    status_code=503,
                    detail=f"Agent runtime is not available: {e}",
                ) from e
        return _runtime
