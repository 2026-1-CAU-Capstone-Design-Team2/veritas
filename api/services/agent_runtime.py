from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from fastapi import HTTPException

from agent import ChatAgent
from llm.llama_server_llm import LLMClient
from tools.autosurvey_tool import AutoSurveyTool
from tools.loader import build_registry, load_schema
from workflows import AutoSurveyConfig, AutoSurveyWorkflow

from .progress_buffer import BUFFER_DEFAULT_MAX, ProgressBuffer
from .screen_monitor import SCREEN_EVENT_BUFFER_MAX, ScreenMonitor


RESEARCH_PROGRESS_BUFFER_MAX = BUFFER_DEFAULT_MAX


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
        # ScreenMonitor owns the intervention event ring buffer + lifecycle
        # state (started_at flag, chat_agent / registry references). The
        # actual screen poller thread still lives inside ChatAgent —
        # ScreenMonitor is just the runtime-side coordinator.
        self._screen_monitor = ScreenMonitor(workspace_lock=self._workspace_lock)
        # Two parallel progress streams. Same buffer class, two instances —
        # so a research run and a verify run can be in flight at the same
        # time without their events colliding. The verify task pollers
        # already enforce ``Query(..., le=500)`` for the cursor read so the
        # ``BUFFER_DEFAULT_MAX`` ceiling matches.
        self._research_progress = ProgressBuffer(maxlen=RESEARCH_PROGRESS_BUFFER_MAX)
        self._verify_progress = ProgressBuffer(maxlen=RESEARCH_PROGRESS_BUFFER_MAX)

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
        # Release the previous workspace's ChromaDB handles before swapping in a
        # new registry. On Windows an open SQLite handle would otherwise keep the
        # old workspace directory locked and undeletable.
        previous_rag_service = getattr(self, "rag_service", None)
        if previous_rag_service is not None:
            try:
                previous_rag_service.close()
            except Exception as e:
                print(f"[workspace][warn] failed to release previous RAG store: {e}")

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
            config=AutoSurveyConfig.from_env(),
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
        # Rebind the screen-monitor's view of the workspace — same controller
        # instance, fresh chat_agent / registry every workspace switch. Avoids
        # the stale-reference race the pre-extraction code didn't even
        # acknowledge because everything was on AgentRuntime directly.
        self._screen_monitor.bind(
            chat_agent=self.chat_agent,
            registry=self.registry,
        )

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
            # Stop the screen poller (if any) before swapping in a new
            # chat_agent — otherwise the old thread keeps running against a
            # released registry. The controller remembers whether it was
            # running so we can re-start cleanly after the switch.
            was_monitoring = self._screen_monitor.stop_for_workspace_switch()
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
        max_docs: int | None = None,
        scout_docs: int | None = None,
        collect_batch_size: int | None = None,
    ) -> dict[str, Any]:
        from datetime import datetime, timezone

        started_at = time.perf_counter()
        started_wall = datetime.now(timezone.utc)
        request = self._append_reference_sites(instruction, reference_urls or [])
        self._research_progress.reset(
            jobId=job_id,
            workspaceId=self.workspace_id,
            instruction=instruction,
        )
        self._research_progress.emit("term_grounding", "주제어 추출 중...")
        workspace_name, grounding = self._grounding_workspace_from_request(request)
        workspace_dir = self._reserve_workspace_dir(workspace_name)
        # Make the new workspace visible to the rest of the system *immediately*.
        # The frontend's Research page, sidebar, and chat panels can switch to
        # this workspace mid-run instead of waiting for the workflow to complete.
        try:
            self._publish_new_workspace(workspace_dir, request)
        except Exception as e:
            print(f"[workspace][publish][warn] {e}")
        # AutoSurvey pacing — per-request values from the research page
        # (maxDocs) and 설정 > 고급 설정 > 조사 진행 방식 (scoutDocs /
        # collectBatchSize) take precedence; otherwise the VERITAS_* env
        # defaults apply. ``AutoSurveyConfig.from_env`` owns the full
        # resolution chain so this call site only has to express the
        # caller-provided overrides. ``collect_batch_size`` doubles as the
        # ``document_summarize`` batch size so one collect cycle maps to
        # one batch summary.
        autosurvey_config = AutoSurveyConfig.from_env(
            max_docs=max_docs,
            collect_batch_size=collect_batch_size,
            scout_docs=scout_docs,
        )
        registry, run_store_service, rag_service = build_registry(
            llm=self.llm,
            run_root=workspace_dir,
            batch_size=autosurvey_config.collect_batch_size,
            max_context=int(os.getenv("VERITAS_MAX_CONTEXT", "16384")),
            enable_screen_context=False,
        )
        workflow = AutoSurveyWorkflow(
            registry=registry,
            run_store_service=run_store_service,
            config=autosurvey_config,
            progress_callback=self._research_progress.emit,
        )
        result = workflow.run_all(
            user_request=request,
            force_plan=True,
            overwrite_summaries=False,
            grounding=grounding,
        )
        indexed_chunks = None
        clean_md_dir = getattr(run_store_service, "clean_md_dir", None)
        index_path = getattr(run_store_service, "index_path", None)
        if clean_md_dir is not None:
            self._research_progress.emit("indexing", "검색 색인 생성 중...")
            indexed_chunks = rag_service.index_autosurvey_output(
                clean_md_dir=Path(clean_md_dir),
                index_path=Path(index_path) if index_path is not None else None,
                clear_first=True,
            )

        failed_documents = (
            result.get("failed_documents", []) if isinstance(result, dict) else []
        )
        if not isinstance(failed_documents, list):
            failed_documents = []

        if failed_documents:
            self._research_progress.emit(
                "completed",
                f"조사 완료 · 요약 실패 {len(failed_documents)}건",
                final=True,
            )
        else:
            self._research_progress.emit("completed", "조사 완료", final=True)

        final_path = run_store_service.final_path
        records = self._read_index_records(index_path)
        # `_document_summaries` already drops duplicates, so its length is the
        # count of actually-collected documents — the value the UI shows as
        # "수집된 문서 수". Duplicates in index.json must never inflate it.
        document_summaries = self._document_summaries(records)
        final_report = self._read_excerpt(final_path, max_chars=1_000_000)
        if not isinstance(final_report, str):
            final_report = ""
        elapsed_seconds = round(time.perf_counter() - started_at, 3)
        # Persist run timing into the workspace (summary/timing.json) so the
        # elapsed time survives completion and an API restart — the in-memory
        # job dict is the only other place it lives, and that is lost on restart.
        try:
            run_store_service.save_timing(
                {
                    "startedAt": started_wall.isoformat().replace("+00:00", "Z"),
                    "completedAt": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "elapsedSeconds": elapsed_seconds,
                }
            )
        except Exception as e:
            print(f"[timing][warn] failed to persist run timing: {e}")
        return {
            "request": request,
            "workspace_id": workspace_dir.name,
            "workspace_name": workspace_dir.name,
            "max_docs": getattr(workflow, "max_docs", None),
            "final_path": str(final_path) if final_path else None,
            "indexed_chunks": indexed_chunks,
            "elapsed_seconds": elapsed_seconds,
            "documents": document_summaries,
            "document_count": len(document_summaries),
            "non_duplicate_document_count": len(document_summaries),
            "failed_documents": failed_documents,
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

    def _publish_new_workspace(self, workspace_dir: Path, user_request: str) -> None:
        """Surface a freshly-reserved workspace before the workflow runs.

        Without this, the new workspace exists on disk but is hidden from
        `_scan_run_workspaces` (no `final.md` / `summary/index.json` / docs
        yet), so the sidebar and chat panels still show the previous
        workspace until AutoSurvey completes minutes later.

        Steps:
        1. Write `summary/request.md` so the workspace passes the
           "has any research evidence" filter in `_scan_run_workspaces`.
        2. Update the in-memory workspace catalog and the persisted
           `app_state.current_workspace_id` so `/api/v1/fe/bootstrap`
           returns the new workspace as current.
        3. Emit a `workspace_created` progress event with the new id,
           display name, and absolute path so the frontend can update
           info tiles, sidebar, and chat panels live.
        """
        workspace_id = workspace_dir.name
        # 1. Make the workspace dir visible to _scan_run_workspaces.
        try:
            summary_dir = workspace_dir / "summary"
            summary_dir.mkdir(parents=True, exist_ok=True)
            (summary_dir / "request.md").write_text(
                str(user_request or "").strip(),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[workspace][publish][warn] request.md: {e}")

        # 2. Promote in-memory + persisted state.
        try:
            from ..api_common import utc_now_iso
            from ..repositories import state_repository as repo
            from . import workspaces_service

            repo.upsert_workspace(
                {
                    "workspaceId": workspace_id,
                    "name": workspace_id,
                    "detail": "조사 진행 중",
                    "status": "running",
                    "lastWorkedAt": utc_now_iso(),
                    "path": str(workspace_dir.resolve()),
                }
            )
            repo.set_current_workspace(workspace_id)
            workspaces_service.remember_current_workspace(workspace_id)
        except Exception as e:
            print(f"[workspace][publish][warn] catalog: {e}")

        # 3. Tell the frontend.
        self._research_progress.emit(
            "workspace_created",
            f"새 워크스페이스 생성: {workspace_id}",
            detail={
                "workspaceId": workspace_id,
                "name": workspace_id,
                "path": str(workspace_dir.resolve()),
            },
        )

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

        clean_md_dir = self.run_store_service.clean_md_dir
        has_clean_md = clean_md_dir.exists() and any(clean_md_dir.glob("*.md"))
        indexed = 0
        if has_clean_md:
            indexed = self.rag_service.index_autosurvey_output(
                clean_md_dir=clean_md_dir,
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
            # Duplicates are not collected documents — they carry no clean_md
            # and no summary file, and hold a ``dup_*`` id. They stay in
            # index.json only to short-circuit re-fetching the same URL, so
            # they are excluded from every user-facing document list and count.
            if record.get("duplicate_of"):
                continue
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

    def get_research_progress(self, since: int, limit: int) -> dict[str, Any]:
        """Public read API for the research progress stream.

        Thin facade over :class:`ProgressBuffer.get_since`; kept on the
        runtime so route handlers (``api/api_routes/research.py``) have one
        well-known callable instead of digging into ``_research_progress``.
        """
        return self._research_progress.get_since(since=since, limit=limit)

    # ------------------------------------------------------------------ verify
    # Verify reuses the same :class:`ProgressBuffer` class as research — two
    # instances so the frontend's two pages can poll their own streams
    # without events colliding.

    def get_verify_progress(self, *, since: int, limit: int) -> dict[str, Any]:
        """Public read API for the verify progress stream — facade like
        :meth:`get_research_progress`."""
        return self._verify_progress.get_since(since=since, limit=limit)

    def run_verification(
        self,
        *,
        workspace_id: str | None,
        tasks: list[str] | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Run the verification pipelines on a workspace, emitting progress events.

        Resolves the "default" placeholder to the most recent real workspace
        (matching :meth:`set_workspace` semantics) so the user never sees a
        verification job hit the empty boot workspace. Returns a compact
        summary dict — the *full* artifacts live in
        ``runs/<workspace>/verification/*.json`` and are read back on demand
        through :class:`VerificationPersistence`.
        """
        from services.verification import (
            ArtifactLoader,
            DenseIndex,
            VerificationConfig,
            VerificationPersistence,
            VerificationService,
        )
        from services.verification.service import ALL_TASKS

        resolved_workspace = (workspace_id or "").strip() or self.workspace_id
        if resolved_workspace == "default":
            initial = self._discover_initial_workspace()
            if initial is not None:
                resolved_workspace = initial.name
        if not resolved_workspace or resolved_workspace == "default":
            raise HTTPException(
                status_code=422,
                detail="검증할 워크스페이스가 없습니다. 먼저 조사를 진행해 주세요.",
            )

        workspace_dir = self.output_root / resolved_workspace
        if not workspace_dir.exists() or not (workspace_dir / "summary").exists():
            raise HTTPException(
                status_code=404,
                detail=f"workspace '{resolved_workspace}' has no research output to verify",
            )

        chromadb_dir = workspace_dir / "chromadb"
        if not chromadb_dir.exists():
            raise HTTPException(
                status_code=409,
                detail="이 워크스페이스에는 인덱스(chromadb)가 없어 검증할 수 없습니다.",
            )

        selected_tasks = [task for task in (tasks or ALL_TASKS) if task in ALL_TASKS]
        if not selected_tasks:
            selected_tasks = list(ALL_TASKS)

        self._verify_progress.reset(
            jobId=job_id,
            workspaceId=resolved_workspace,
        )
        self._verify_progress.emit(
            "queued",
            f"검증 시작 · 워크스페이스 {resolved_workspace}",
            detail={"tasks": list(selected_tasks)},
        )

        config = VerificationConfig()
        service = VerificationService(
            workspace=resolved_workspace,
            artifact_loader=ArtifactLoader(self.output_root),
            dense=DenseIndex(self.llm),
            config=config,
            persistence=VerificationPersistence(self.output_root),
            # Task 1's flow planner needs a chat-completion LLM; the same
            # llama-server backs the embed channel via DenseIndex above.
            llm=self.llm,
        )

        try:
            artifacts = service.run(
                tasks=selected_tasks,
                progress_callback=self._verify_progress.emit,
            )
        except HTTPException:
            raise
        except Exception as exc:
            self._verify_progress.emit("failed", f"검증 실패: {exc}", final=True)
            raise HTTPException(
                status_code=502,
                detail=f"verification pipeline failed: {exc}",
            ) from exc

        return {
            "workspaceId": resolved_workspace,
            "completedTasks": list(selected_tasks),
            "configHash": artifacts.config_hash,
            "sectionCount": (
                len(artifacts.sections.sections) if artifacts.sections else 0
            ),
            "conceptClusterCount": (
                len(artifacts.consensus.concept_clusters) if artifacts.consensus else 0
            ),
            "conflictCount": (
                len(artifacts.consensus.conflicts) if artifacts.consensus else 0
            ),
            "reliabilityDistribution": (
                dict(artifacts.reliability.distribution)
                if artifacts.reliability
                else {}
            ),
            "documentCount": len(service.docs),
        }

    # -------------------------------------------------------------- screen
    # Thin facades over :class:`ScreenMonitor`. The route handlers in
    # ``api/services/screen_monitoring_service.py`` keep calling these on
    # the runtime so the public surface is unchanged; the controller owns
    # the state.

    def start_screen_monitoring(self) -> dict[str, Any]:
        """Start the screen poller; record assistant answers into the buffer."""
        return self._screen_monitor.start(
            on_answer=lambda answer, intervention: self._screen_monitor.record_assist_answer(
                answer, intervention, workspace_id=self.workspace_id
            ),
        )

    def stop_screen_monitoring(self) -> dict[str, Any]:
        return self._screen_monitor.stop()

    def screen_monitoring_status(self) -> dict[str, Any]:
        return self._screen_monitor.status(workspace_id=self.workspace_id)

    def get_screen_events_since(self, *, since: int, limit: int) -> dict[str, Any]:
        return self._screen_monitor.get_events_since(
            since=since,
            limit=limit,
            workspace_id=self.workspace_id,
        )

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
