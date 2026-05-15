from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import HTTPException

from agent import ChatAgent
from llm.llama_server_llm import LLMClient
from tools.autosurvey_tool import AutoSurveyTool
from tools.loader import build_registry, load_schema
from workflows import AutoSurveyWorkflow


SCREEN_EVENT_BUFFER_MAX = 100
RESEARCH_PROGRESS_BUFFER_MAX = 500


class AgentRuntime:
    def __init__(self) -> None:
        self.output_root = Path(os.getenv("VERITAS_OUTPUT_DIR", "runs")).expanduser().resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._cleanup_pending_dirs()
        # Remove a stale `runs/api/` from a previous session before we decide
        # which workspace to attach to. This is what fixes the "api 폴더가
        # 계속 생기는" issue — we never re-materialize it unless there is
        # genuinely no real workspace to land on.
        self._cleanup_empty_api_dir()
        # Drop SQLite rows for workspaces whose runs/<id>/ folder was deleted
        # while the app was offline. The dashboard reads workspaces directly
        # from the DB, so this is what keeps "최근 작업" honest across reboots.
        try:
            from db.workspace_sync import reconcile_workspaces_with_disk

            reconcile_workspaces_with_disk(self.output_root)
        except Exception as e:
            print(f"[workspace][reconcile][warn] {e}")

        self.llm = LLMClient(
            host=os.getenv("VERITAS_LLM_HOST", "127.0.0.1"),
            port=int(os.getenv("VERITAS_LLM_PORT", "8080")),
            embed_host=os.getenv("VERITAS_EMBED_HOST") or None,
            embed_port=int(os.getenv("VERITAS_EMBED_PORT", "8081")),
            trace_latency=os.getenv("VERITAS_TRACE_LATENCY", "1") != "0",
        )
        self._workspace_lock = threading.RLock()
        self._screen_events: deque[dict[str, Any]] = deque(maxlen=SCREEN_EVENT_BUFFER_MAX)
        self._screen_event_seq = 0
        self._screen_event_lock = threading.Lock()
        self._screen_monitoring_started_at: str | None = None
        self._research_progress: deque[dict[str, Any]] = deque(maxlen=RESEARCH_PROGRESS_BUFFER_MAX)
        self._research_progress_seq = 0
        self._research_progress_lock = threading.Lock()
        self._research_active_job: dict[str, Any] | None = None

        # Boot-time workspace selection: prefer the most recently-used real
        # workspace so we never create `runs/api/` when one already exists.
        initial = self._discover_initial_workspace()
        if initial is not None:
            self.workspace_id = initial.name
            self.output_dir = initial
        else:
            self.workspace_id = "default"
            self.output_dir = self.output_root / "api"
        self._configure_workspace_runtime(self.output_dir)

    def _configure_workspace_runtime(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = output_dir
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
        self.chat_agent.chat_history = self._load_workspace_chat_history()
        if self.rag_service is not None:
            self.rag_service.chat_history = list(self.chat_agent.chat_history)

    def set_workspace(self, workspace_id: str) -> None:
        workspace_id = str(workspace_id or "").strip()
        if not workspace_id:
            return
        # The frontend uses "default" as a placeholder for "no workspace
        # selected yet". If real workspaces exist, resolve it to the most
        # recently used one so we don't materialize a phantom `runs/api/`.
        if workspace_id == "default":
            recent = self._discover_initial_workspace()
            if recent is not None:
                workspace_id = recent.name
        if workspace_id == self.workspace_id:
            return
        with self._workspace_lock:
            if workspace_id == self.workspace_id:
                return
            was_monitoring = bool(
                self._screen_monitoring_started_at
                and self.chat_agent._screen_monitor_thread
                and self.chat_agent._screen_monitor_thread.is_alive()
            )
            if was_monitoring:
                try:
                    self.chat_agent.stop_screen_monitoring()
                except Exception as e:
                    print(f"[screen_monitoring][warn] stop on workspace switch failed: {e}")
            leaving_default = self.workspace_id == "default"
            self.workspace_id = workspace_id
            output_dir = (
                self.output_root / workspace_id
                if workspace_id != "default"
                else self.output_root / "api"
            )
            self._configure_workspace_runtime(output_dir)
            # If we just moved off the default `api/` workspace and it has
            # no meaningful content, remove it so it doesn't linger.
            if leaving_default and workspace_id != "default":
                self._cleanup_empty_api_dir()
            if was_monitoring:
                self.start_screen_monitoring()

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

    def answer_chat_iter(self, message: str, mode: str) -> Iterator[str]:
        if mode == "rag":
            self._ensure_rag_index(require_documents=False)
            return self.chat_agent.ask_rag_iter(message)
        return self.chat_agent.ask_auto_iter(message)

    def answer_chat_selection_iter(self, message: str, mode: str) -> Iterator[str]:
        normalized_mode = str(mode or "research").strip().lower()
        if normalized_mode in {"research", "autosurvey"}:
            return self.chat_agent.ask_explicit_tool_iter("autosurvey", message)
        if normalized_mode == "rag":
            self._ensure_rag_index(require_documents=False)
            return self.chat_agent.ask_explicit_tool_iter("rag", message)
        return self.chat_agent.ask_auto_iter(message)

    def persist_chat_turn(self, message: str, assistant_text: str) -> None:
        """Persist a (user, assistant) turn into the workspace chat_history.json
        file so any chat panel (write/document-assist) can render the same log.
        """
        try:
            path = self.output_dir / "chat_history.json"
            payload: list[dict[str, Any]] = []
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    items = raw.get("items", raw) if isinstance(raw, dict) else raw
                    if isinstance(items, list):
                        payload = [item for item in items if isinstance(item, dict)]
                except Exception:
                    payload = []
            payload.append({"role": "user", "text": message})
            payload.append({"role": "assistant", "text": assistant_text})
            from ..api_common import utc_now_iso

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"items": payload, "updatedAt": utc_now_iso()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[chat][persist][warn] failed to save chat history: {e}")

    def answer_chat(self, message: str, mode: str) -> str:
        if mode == "rag":
            self._ensure_rag_index(require_documents=False)
            return self.chat_agent.ask_rag(message, stream=False)
        return self.chat_agent.ask_auto(message, stream=False)

    def answer_chat_selection(self, message: str, mode: str) -> str:
        """Answer a frontend chat turn where the mode selector is authoritative.

        The chat UI's "자료조사" and "RAG" choices are equivalent to entering
        `/autosurvey ...` and `/rag ...` in the CLI. Other internal API calls
        still use answer_chat(), where "research" means general tool-capable chat.
        """
        normalized_mode = str(mode or "research").strip().lower()
        if normalized_mode in {"research", "autosurvey"}:
            return self.chat_agent.ask_explicit_tool("autosurvey", message, stream=False)
        if normalized_mode == "rag":
            self._ensure_rag_index(require_documents=False)
            return self.chat_agent.ask_explicit_tool("rag", message, stream=False)
        return self.chat_agent.ask_auto(message, stream=False)

    def _load_workspace_chat_history(self) -> list[tuple[str, str]]:
        path = self.output_dir / "chat_history.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []

        items = payload.get("items", payload) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return []

        turns: list[tuple[str, str]] = []
        pending_user: str | None = None
        for item in items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").lower()
            text = str(item.get("text") or item.get("content") or "")
            if role == "user":
                pending_user = text
            elif role == "assistant" and pending_user is not None:
                turns.append((pending_user, text))
                pending_user = None
        return turns

    def run_autosurvey(
        self,
        *,
        instruction: str,
        reference_urls: list[str] | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        request = self._append_reference_sites(instruction, reference_urls or [])
        self._reset_research_progress(job_id=job_id, instruction=instruction)
        self._emit_research_progress("term_grounding", "주제어 추출 중...")
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
            progress_callback=self._emit_research_progress,
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
            self._emit_research_progress("indexing", "검색 색인 생성 중...")
            indexed_chunks = rag_service.index_autosurvey_output(
                summary_dir=Path(summary_dir),
                index_path=Path(index_path) if index_path is not None else None,
                clear_first=True,
            )
        self._emit_research_progress("completed", "조사 완료", final=True)

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

    def _discover_initial_workspace(self) -> Path | None:
        """Return the most-recently-modified real workspace dir, or None.

        A "real" workspace has at least one piece of research evidence:
        a final report, a summary index, or any `doc_*.md` summary file.
        Used to avoid creating `runs/api/` when there is already a workspace
        to land on at boot, or to resolve frontend requests for the
        "default" workspace to something concrete.
        """
        if not self.output_root.exists():
            return None
        candidates: list[Path] = []
        try:
            for path in self.output_root.iterdir():
                if not path.is_dir():
                    continue
                name = path.name
                if name in {"api", "__pycache__"} or name.startswith("_"):
                    continue
                summary_dir = path / "summary"
                has_final = (path / "final.md").exists()
                has_index = (summary_dir / "index.json").exists()
                has_summaries = summary_dir.exists() and any(summary_dir.glob("doc_*.md"))
                if has_final or has_index or has_summaries:
                    candidates.append(path)
        except Exception as e:
            print(f"[workspace][discover][warn] {e}")
            return None
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0]

    def _cleanup_empty_api_dir(self) -> None:
        """Remove `runs/api/` if it has no meaningful research data.

        Called at boot (to clear a stale `api/` from a prior session) and
        whenever we transition off the default workspace, so the directory
        never sticks around as a phantom side-effect of initialization.
        """
        api_dir = self.output_root / "api"
        if not api_dir.exists() or not api_dir.is_dir():
            return
        summary_dir = api_dir / "summary"
        has_final = (api_dir / "final.md").exists()
        has_index = (summary_dir / "index.json").exists()
        has_summaries = summary_dir.exists() and any(summary_dir.glob("doc_*.md"))
        if has_final or has_index or has_summaries:
            return
        # Only chromadb/corpus skeletons remain — safe to remove.
        try:
            shutil.rmtree(api_dir)
        except Exception as e:
            print(f"[workspace][cleanup][warn] could not remove {api_dir}: {e}")

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

    def _reset_research_progress(
        self,
        *,
        job_id: str | None,
        instruction: str,
    ) -> None:
        from datetime import datetime, timezone

        with self._research_progress_lock:
            self._research_progress.clear()
            self._research_progress_seq = 0
            self._research_active_job = {
                "jobId": job_id,
                "workspaceId": self.workspace_id,
                "instruction": instruction,
                "startedAt": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "status": "running",
            }

    def _emit_research_progress(
        self,
        stage: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        final: bool = False,
    ) -> None:
        from datetime import datetime, timezone

        message_text = " ".join(str(message or "").split()).strip()[:280]
        with self._research_progress_lock:
            self._research_progress_seq += 1
            seq = self._research_progress_seq
            event = {
                "seq": seq,
                "stage": str(stage or "").strip() or "info",
                "message": message_text,
                "detail": detail or {},
                "timestamp": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            self._research_progress.append(event)
            if self._research_active_job is not None and final:
                self._research_active_job["status"] = "completed"

    def get_research_progress(self, since: int, limit: int) -> dict[str, Any]:
        if limit <= 0:
            limit = 50
        limit = min(limit, RESEARCH_PROGRESS_BUFFER_MAX)
        with self._research_progress_lock:
            latest_seq = self._research_progress_seq
            events = [
                event
                for event in self._research_progress
                if int(event.get("seq", 0)) > since
            ]
            job_snapshot = dict(self._research_active_job or {})
        events.sort(key=lambda item: int(item.get("seq", 0)))
        events = events[:limit]
        next_cursor = events[-1]["seq"] if events else since
        return {
            "items": events,
            "nextCursor": next_cursor,
            "latestSeq": latest_seq,
            "activeJob": job_snapshot or None,
        }

    def start_screen_monitoring(self) -> dict[str, Any]:
        """Start proactive screen monitoring on the active ChatAgent.

        Captured screen interventions are converted into assistant answers and
        appended to an in-memory event buffer that the frontend can poll.
        """
        with self._workspace_lock:
            if not self.chat_agent.has_screen_context():
                raise HTTPException(
                    status_code=409,
                    detail="screen_context tool is not registered. Enable VERITAS_ENABLE_SCREEN_CONTEXT before starting the API.",
                )
            started = self.chat_agent.start_screen_monitoring(
                on_answer=self._on_screen_assist_answer,
                stream=False,
            )
            if not started:
                raise HTTPException(
                    status_code=500,
                    detail="Failed to start screen monitoring. Check screen_context tool status and capture logs.",
                )
            if self._screen_monitoring_started_at is None:
                from datetime import datetime, timezone

                self._screen_monitoring_started_at = (
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                )
            return self.screen_monitoring_status()

    def stop_screen_monitoring(self) -> dict[str, Any]:
        with self._workspace_lock:
            if self.chat_agent.has_screen_context():
                self.chat_agent.stop_screen_monitoring()
            self._screen_monitoring_started_at = None
            return self.screen_monitoring_status()

    def screen_monitoring_status(self) -> dict[str, Any]:
        registered = bool(
            self.registry is not None and self.registry.has("screen_context")
        )
        polling = False
        last_poll_error: str | None = None
        latest_event_id: str | None = None
        latest_captured_at: str | None = None
        latest_diagnostics: dict[str, Any] = {}
        pending_intervention_count = 0
        capture_log_path: str | None = None
        if registered:
            try:
                result = self.registry.call("screen_context", action="status")
            except Exception as e:
                last_poll_error = f"status call failed: {e}"
                result = None
            if result is not None and getattr(result, "success", False):
                data = result.data if isinstance(result.data, dict) else {}
                polling = bool(data.get("polling"))
                last_poll_error = data.get("last_poll_error")
                latest_event_id = data.get("latest_event_id")
                latest_captured_at = data.get("latest_captured_at")
                diagnostics = data.get("latest_diagnostics") or {}
                if isinstance(diagnostics, dict):
                    latest_diagnostics = diagnostics
                pending_intervention_count = int(
                    data.get("pending_intervention_count") or 0
                )
                capture_log_path = data.get("capture_log_path")
            elif result is not None:
                last_poll_error = getattr(result, "error", None) or last_poll_error

        with self._screen_event_lock:
            latest_seq = self._screen_event_seq
            event_buffer_size = len(self._screen_events)

        return {
            "registered": registered,
            "polling": polling,
            "monitoringStartedAt": self._screen_monitoring_started_at,
            "workspaceId": self.workspace_id,
            "lastPollError": last_poll_error,
            "latestCaptureEventId": latest_event_id,
            "latestCapturedAt": latest_captured_at,
            "latestDiagnostics": latest_diagnostics,
            "pendingInterventionCount": pending_intervention_count,
            "captureLogPath": capture_log_path,
            "eventBufferSize": event_buffer_size,
            "latestEventSeq": latest_seq,
        }

    def get_screen_events_since(
        self,
        *,
        since: int,
        limit: int,
    ) -> dict[str, Any]:
        if limit <= 0:
            limit = 20
        limit = min(limit, SCREEN_EVENT_BUFFER_MAX)
        with self._screen_event_lock:
            latest_seq = self._screen_event_seq
            events = [
                event for event in self._screen_events if int(event.get("seq", 0)) > since
            ]
        events.sort(key=lambda item: int(item.get("seq", 0)))
        events = events[:limit]
        next_cursor = events[-1]["seq"] if events else since
        return {
            "items": events,
            "nextCursor": next_cursor,
            "latestSeq": latest_seq,
            "workspaceId": self.workspace_id,
        }

    def _on_screen_assist_answer(
        self,
        answer: str,
        intervention: dict[str, Any],
    ) -> None:
        from datetime import datetime, timezone

        text = str(answer or "").strip()
        if not text:
            return
        with self._screen_event_lock:
            self._screen_event_seq += 1
            seq = self._screen_event_seq
            workspace_id = self.workspace_id
        writing_context = intervention.get("writing_context") if isinstance(intervention, dict) else {}
        if not isinstance(writing_context, dict):
            writing_context = {}
        app_context = intervention.get("app_context") if isinstance(intervention, dict) else {}
        if not isinstance(app_context, dict):
            app_context = {}
        focused = " ".join(str(writing_context.get("focused_sentence") or "").split()).strip()
        recent = " ".join(str(writing_context.get("recent_sentences") or "").split()).strip()
        trigger_text = focused or recent
        event = {
            "seq": seq,
            "eventId": str(intervention.get("event_id") or "") or f"proactive_{seq}",
            "workspaceId": workspace_id,
            "answer": text,
            "category": "proactive",
            "tone": "working",
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "capturedAt": intervention.get("captured_at"),
            "triggerText": trigger_text,
            "appContext": {
                "title": app_context.get("title") or app_context.get("window_title"),
                "processName": app_context.get("process_name"),
                "activeAppType": app_context.get("active_app_type")
                or writing_context.get("active_app_type"),
            },
            "writingContext": {
                "focusedSentence": focused,
                "recentSentences": recent,
                "paragraphSource": writing_context.get("paragraph_source"),
                "fullTextChars": writing_context.get("full_text_chars"),
                "confidence": writing_context.get("confidence"),
            },
        }
        with self._screen_event_lock:
            self._screen_events.append(event)

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
