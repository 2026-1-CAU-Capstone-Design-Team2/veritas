from __future__ import annotations

import atexit
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from fastapi import HTTPException

from agent import ChatAgent
from llm.autosurvey_llm_factory import build_autosurvey_llm
from llm.llama_server_llm import LLMClient
from llm.llama_supervisor import LlamaServer
from llm.memory_aware_llm import MemoryAwareLLMClient
from services.memory_tools_funcs import MemoryRuntime
from services.proactive.generator import DEFAULT_GHOST_MAX_TOKENS, ProactiveGenerator
from services.proactive.orchestrator import ProactiveOrchestrator
from services.proactive.screen_bridge import (
    observe_screen_intervention,
    proactive_screen_enabled,
)
from tools.autosurvey_tool import AutoSurveyTool
from tools.loader import build_registry, load_schema
from workflows import AutoSurveyConfig, AutoSurveyWorkflow

from . import workspace_paths
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

        llm_host = os.getenv("VERITAS_LLM_HOST", "127.0.0.1")
        llm_port = int(os.getenv("VERITAS_LLM_PORT", "8080"))
        embed_host = os.getenv("VERITAS_EMBED_HOST") or llm_host
        embed_port = int(os.getenv("VERITAS_EMBED_PORT", "8081"))

        # The API process owns the llama-server lifecycle so a settings-driven
        # model switch can restart it (see switch_llm_model). ensure_started
        # adopts an already-running server (dev / launcher) or spawns one; if it
        # can't (no binary / model not downloaded yet) we log and fall through —
        # the LLMClient below then connects to whatever is already serving, or
        # fails exactly as before.
        self._llm_server = LlamaServer("llm", llm_host, llm_port)
        self._embed_server = LlamaServer("embedding", embed_host, embed_port)
        self._ensure_llama_servers()
        # Best-effort: stop owned llama children when this process exits
        # gracefully (atexit does not run on a hard Windows TerminateProcess —
        # see shutdown_runtime / the API shutdown hook for the uvicorn path).
        atexit.register(self.shutdown)

        self.raw_llm = LLMClient(
            host=llm_host,
            port=llm_port,
            embed_host=os.getenv("VERITAS_EMBED_HOST") or None,
            embed_port=embed_port,
            trace_latency=os.getenv("VERITAS_TRACE_LATENCY", "1") != "0",
        )
        runtime_context_tokens = self._llm_context_per_slot_tokens()
        try:
            self.raw_llm.n_ctx = runtime_context_tokens
        except Exception:
            pass
        # MemoryRuntime은 workspace 결정 전이라 placeholder로 만들고,
        # _configure_workspace_runtime에서 configure_workspace로 갈아 끼운다.
        self.memory_runtime = MemoryRuntime(
            raw_llm=self.raw_llm,
            workspace_root=self.output_root / "api",
            max_context_tokens=runtime_context_tokens,
        )
        # self.llm은 wrapper. callers는 동일한 surface(.ask/.iter_ask/.call/...)를 본다.
        self.llm = MemoryAwareLLMClient(
            raw_llm=self.raw_llm,
            memory_runtime=self.memory_runtime,
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

    def _llm_context_per_slot_tokens(self) -> int:
        """Context budget for one request, not llama-server's total ``-c``."""
        try:
            from llm.llama_supervisor import effective_context_per_slot

            tokens = int(effective_context_per_slot("llm"))
        except Exception:
            tokens = int(getattr(self.raw_llm, "n_ctx", 8192) or 8192)
        return max(1, int(tokens))

    def _sync_llm_context_budget(self) -> int:
        tokens = self._llm_context_per_slot_tokens()
        try:
            self.raw_llm.n_ctx = tokens
        except Exception:
            pass
        memory_runtime = getattr(self, "memory_runtime", None)
        if memory_runtime is not None:
            try:
                memory_runtime.update_n_ctx(tokens)
            except Exception:
                pass
        return tokens

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
        # Stop the previous workspace's proactive bandit threads + flush state
        # before we rebuild the agent and rag references that its generator
        # closed over.
        previous_proactive = getattr(self, "_proactive_orchestrator", None)
        if previous_proactive is not None:
            try:
                previous_proactive.close()
            except Exception as e:
                print(f"[workspace][warn] failed to close previous proactive orchestrator: {e}")
            self._proactive_orchestrator = None

        output_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = output_dir
        # workspace 전환마다 memory storage 핸들을 새 디렉토리로 교체.
        if hasattr(self, "memory_runtime"):
            self.memory_runtime.configure_workspace(output_dir)
            # One-shot lift of the legacy chat_history.json log into the
            # workspace's memory.sqlite3, so the new read-side projection sees
            # pre-memory conversations. The helper is idempotent — it bails
            # when recall already has rows OR no legacy file exists — and on a
            # successful import the JSON file is renamed out of the way so the
            # migration never repeats.
            self._migrate_legacy_chat_history(output_dir)
        try:
            from ..repositories import state_repository

            custom_document_tools = state_repository.get_document_tools_settings()
        except Exception:
            custom_document_tools = []
        self.registry, self.run_store_service, self.rag_service = build_registry(
            llm=self.llm,
            run_root=self.output_dir,
            batch_size=int(os.getenv("VERITAS_BATCH_SIZE", "5")),
            max_context=int(os.getenv("VERITAS_MAX_CONTEXT", "16384")),
            enable_screen_context=os.getenv("VERITAS_ENABLE_SCREEN_CONTEXT", "1") != "0",
            screen_interval_sec=float(os.getenv("VERITAS_SCREEN_INTERVAL", "1.0")),
            screen_debug_log=os.getenv("VERITAS_SCREEN_DEBUG", "0") == "1",
            custom_document_tools=custom_document_tools,
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
            screen_workspace_id=self.workspace_id,
        )
        # Conversation history lives in the workspace memory.sqlite3 (FIFO +
        # recall) which the MemoryRuntime opened above; the agent keeps no
        # parallel in-memory turn log, and ``draft_chat_service.get_chat_history``
        # projects the recall tier into the UI shape on demand. The legacy
        # per-workspace chat_history.json file is migrated above and then
        # archived — it is no longer a live writer.
        # Rebind the screen-monitor's view of the workspace — same controller
        # instance, fresh chat_agent / registry every workspace switch. Avoids
        # the stale-reference race the pre-extraction code didn't even
        # acknowledge because everything was on AgentRuntime directly.
        self._screen_monitor.bind(
            chat_agent=self.chat_agent,
            registry=self.registry,
        )
        # Build the per-workspace proactive orchestrator lazily — see
        # ``get_proactive_orchestrator``. We do NOT eagerly construct it here
        # because the boot-time workspace can be "default" (a placeholder) and
        # we don't want to materialize a runs/api/proactive_policy/ folder if
        # the user never opens the editor.
        self._proactive_orchestrator: ProactiveOrchestrator | None = None

    def _ensure_llama_servers(self) -> None:
        """Adopt-or-spawn the LLM + embedding llama-servers (best effort).

        Resolves the selected GGUF paths from settings and asks each supervisor
        to adopt a running server or spawn one. Failures (missing binary /
        un-downloaded model) are logged, not raised — the LLMClient constructor
        still runs and connects to whatever is serving, preserving prior
        behavior when llama was started outside the app.

        Gated on ``VERITAS_MANAGE_LLAMA=1`` (set by the launcher): only then does
        the API spawn + own the servers (enabling live restart on model switch).
        Without it the default behavior is unchanged — the app connects to an
        externally-started llama-server and a live switch reports it as
        externally managed.
        """
        if os.getenv("VERITAS_MANAGE_LLAMA", "0") != "1":
            return
        try:
            from llm.model_catalog import (
                find_model_file,
                selected_embedding_from_settings,
                selected_model_from_settings,
            )
            from llm.model_settings import load_settings

            settings = load_settings()
            targets = (
                (self._llm_server, find_model_file(selected_model_from_settings(settings)), "llm"),
                (self._embed_server, find_model_file(selected_embedding_from_settings(settings)), "embedding"),
            )
        except Exception as exc:  # noqa: BLE001 - resolution is best-effort
            print(f"[runtime][llama][warn] could not resolve model paths: {exc}")
            return

        for server, path, label in targets:
            try:
                if path is not None:
                    server.ensure_started(path)
                elif not server.is_healthy(0.5):
                    print(
                        f"[runtime][llama][warn] {label} model not downloaded and "
                        f"no server running on :{server.port}"
                    )
            except Exception as exc:  # noqa: BLE001 - keep boot resilient
                print(f"[runtime][llama][warn] {label} ensure_started failed: {exc}")

    def switch_llm_model(self, model_id: str, *, report=None):
        """Live-switch the chat LLM model end-to-end.

        Single owner of the *mechanism* so the settings service / route stay
        thin: (1) download the GGUF if missing, (2) restart the owned
        llama-server with it, (3) re-detect model + n_ctx on the shared
        LLMClient *in place* (every tool keeps its reference), (4) persist the
        selection. ``report(stage, message, detail)`` is an optional progress
        sink (download bytes / restart / refresh) the caller forwards to a
        progress buffer.
        """
        from pathlib import Path as _Path

        from llm.model_catalog import find_model_file, get_model
        from llm.model_manager import download_model
        from llm.model_settings import save_selected_models

        emit = report or (lambda *_a, **_k: None)
        spec = get_model(model_id, kind="llm")

        path = find_model_file(spec)
        if path is None:
            emit("download", f"{spec.short_name} 다운로드 중...", {})

            def _on_bytes(done: int, total: int | None) -> None:
                emit("download", "모델 다운로드 중", {"done": int(done), "total": int(total or 0)})

            path = download_model(spec, progress=_on_bytes, hf_token=os.getenv("HF_TOKEN"))

        # Persist the selection as soon as the model is on disk — BEFORE the
        # (riskier) server restart. If the restart then fails, the next runtime
        # startup still loads THIS model rather than the previously-selected one
        # (which may be exactly why we are switching, e.g. it OOM'd on load).
        save_selected_models(llm_model_id=spec.id)

        emit("restart", "LLM 서버 재시작 중...", {})
        self._llm_server.restart(_Path(path))

        emit("refresh", "모델 정보 갱신 중...", {})
        self.llm.refresh_model_info()
        self._sync_llm_context_budget()
        return spec

    def restart_llm_server(self) -> None:
        """Restart the currently selected LLM server to apply llama flags."""
        from pathlib import Path as _Path

        from llm.model_catalog import find_model_file, selected_model_from_settings
        from llm.model_settings import load_settings

        settings = load_settings()
        spec = selected_model_from_settings(settings)
        path = find_model_file(spec)
        if path is None:
            raise RuntimeError(f"selected model is not downloaded: {spec.name}")
        self._llm_server.restart(_Path(path))
        self.llm.refresh_model_info()
        self._sync_llm_context_budget()

    def shutdown(self) -> None:
        """Stop any llama-servers this process owns (best effort, idempotent)."""
        proactive = getattr(self, "_proactive_orchestrator", None)
        if proactive is not None:
            try:
                proactive.close()
            except Exception:
                pass
        for server in (getattr(self, "_llm_server", None), getattr(self, "_embed_server", None)):
            if server is None:
                continue
            try:
                server.stop()
            except Exception:
                pass
        # Close the reused memory.sqlite3 connection so WAL/SHM sidecar files are
        # checkpointed and removed instead of relying on __del__ GC at exit.
        memory_runtime = getattr(self, "memory_runtime", None)
        if memory_runtime is not None:
            try:
                memory_runtime.close()
            except Exception:
                pass
        # Flush + close the optional memory trace file (--mem-debug-file).
        try:
            from services.memory_tools_funcs.debug import close_debug_file

            close_debug_file()
        except Exception:
            pass

    def set_llm_parallel(self, value: int) -> int:
        """Apply the parallel-decoding concurrency to the shared LLM client.

        Encapsulates the ``self.llm.max_parallel`` mutation so callers (e.g. the
        settings service) don't reach through the runtime into the LLM client.
        Clamped to 1..5 to match the settings contract; returns the applied
        value. Takes effect on the next batch since ``LLMClient.map_parallel``
        reads ``max_parallel`` at call time.
        """
        applied = max(1, min(5, int(value)))
        self.llm.max_parallel = applied
        return applied

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
                memory_runtime=self.memory_runtime,
                max_docs_cap=int(os.getenv("VERITAS_API_AUTOSURVEY_MAX_DOCS", "5")),
            )
        )

    def answer_chat_iter(self, message: str, mode: str) -> Iterator[str]:
        if mode == "rag":
            self._ensure_rag_index(require_documents=False)
            return self.chat_agent.ask_rag_iter(message)
        return self.chat_agent.ask_auto_iter(message)

    def answer_chat_selection_iter(
        self,
        message: str,
        mode: str,
        doc_context: str = "",
        *,
        source_scope_filter: str = "all",
        include_private_local: bool = True,
    ) -> Iterator[str]:
        normalized_mode = str(mode or "research").strip().lower()
        if normalized_mode in {"research", "autosurvey"}:
            return self.chat_agent.ask_explicit_tool_iter(
                "autosurvey", message, doc_context=doc_context
            )
        if normalized_mode == "rag":
            self._ensure_rag_index(require_documents=False)
            # Strict grounded RAG, NOT the permissive rag_search + tool-synthesis
            # path: when the active workspace's index has nothing relevant to the
            # question (e.g. it is about another workspace's topic), the model
            # says so instead of answering from general knowledge.
            return self.chat_agent.ask_rag_iter(
                message,
                doc_context=doc_context,
                source_scope_filter=source_scope_filter,
                include_private_local=include_private_local,
            )
        return self.chat_agent.ask_auto_iter(message, doc_context=doc_context)

    # -- proactive bandit -----------------------------------------------------
    # One orchestrator per workspace; reused across native_editor and
    # external_screen surfaces so the bandit learns from both.

    def get_proactive_orchestrator(self) -> ProactiveOrchestrator:
        """Return (and lazily build) the proactive orchestrator for the
        current workspace. Reused on every observe / feedback call.

        Post-pivot the orchestrator is purely deterministic — no rng to wire,
        no bandit state to seed. The generator is constructed from the
        AgentRuntime's existing ghostwrite / editor_assist facades so the
        LLM call path stays unchanged.
        """
        with self._workspace_lock:
            if self._proactive_orchestrator is not None:
                return self._proactive_orchestrator
            generator = ProactiveGenerator(
                ghostwrite_iter=self.ghostwrite_iter,
                editor_assist_iter=self.editor_assist_iter,
                workspace_is_active=self._workspace_is_active,
                max_tokens_ghost=int(
                    os.getenv(
                        "VERITAS_PROACTIVE_GHOST_MAX_TOKENS",
                        str(DEFAULT_GHOST_MAX_TOKENS),
                    )
                ),
            )
            self._proactive_orchestrator = ProactiveOrchestrator(
                output_root=self.output_root,
                workspace_id=self.workspace_id,
                generator=generator,
            )
            return self._proactive_orchestrator

    def _workspace_is_active(self, workspace_id: str) -> bool:
        """Whether ``workspace_id`` matches the active runtime workspace —
        the generator uses this as the RAG grounding gate, matching the
        existing pattern in ``editor_service._workspace_is_active``."""
        return str(workspace_id or "") == str(self.workspace_id or "")

    # -- editor (standalone writer) surfaces ----------------------------------
    # Thin facades over the ChatAgent so the editor window's three AI surfaces
    # run through the same agent (and its workspace-bound rag_service) as the
    # chat / document-assist pages, instead of calling the LLM directly.

    def ghostwrite_iter(
        self,
        prefix: str,
        suffix: str = "",
        *,
        max_tokens: int = 64,
        use_workspace: bool = True,
        section_heading: str = "",
    ) -> Iterator[str]:
        return self.chat_agent.iter_ghostwrite(
            prefix,
            suffix,
            max_tokens=max_tokens,
            use_workspace=use_workspace,
            section_heading=section_heading,
        )

    def editor_assist_iter(
        self,
        action: str,
        text: str,
        *,
        max_tokens: int = 400,
        use_workspace: bool = True,
        additive_grounding: bool = False,
    ) -> Iterator[str]:
        return self.chat_agent.iter_editor_assist(
            action,
            text,
            max_tokens=max_tokens,
            use_workspace=use_workspace,
            additive_grounding=additive_grounding,
        )

    def _migrate_legacy_chat_history(self, output_dir: Path) -> None:
        """One-shot lift of ``<workspace>/chat_history.json`` into memory.sqlite3.

        The legacy JSON log existed because each chat turn was written to two
        parallel stores — the workspace JSON file (for UI rendering) and the
        memory FIFO/Recall. Now the recall tier is the single source of truth
        and ``draft_chat_service`` reads through ``history_as_chat_items``, so
        pre-memory workspaces would lose their visible history without this
        bootstrap.

        Guarded against repeating itself: bails when the recall tier already
        has rows (so live conversations aren't double-imported on subsequent
        workspace switches), and on a successful import renames the JSON to
        ``chat_history.legacy.json`` so the next configure call sees nothing
        to migrate. Failures are logged but never raised — chat history is
        best-effort and the runtime must boot regardless.
        """
        legacy_path = output_dir / "chat_history.json"
        if not legacy_path.exists():
            return
        try:
            if self.memory_runtime.recall.tail(limit=1):
                # The workspace already has live recall rows — either the
                # migration already ran or new chat happened. Either way, do
                # not re-import the JSON over the top of fresh memory.
                return
            payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[chat][migrate][warn] could not read {legacy_path.name}: {e}")
            return

        items = payload.get("items", payload) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return

        try:
            imported = self.memory_runtime.import_legacy_chat_items(items)
        except Exception as e:
            print(f"[chat][migrate][warn] import failed: {e}")
            return

        if imported <= 0:
            return

        try:
            legacy_path.rename(output_dir / "chat_history.legacy.json")
        except Exception as e:
            # Renaming is the idempotency guard; if it fails the next configure
            # call would see the JSON again, but the recall-non-empty guard
            # above still prevents re-import.
            print(f"[chat][migrate][warn] could not archive legacy file: {e}")
        print(
            f"[chat][migrate] imported {imported} legacy turn(s) "
            f"from {legacy_path.name} into memory.sqlite3"
        )

    def answer_chat(self, message: str, mode: str) -> str:
        if mode == "rag":
            self._ensure_rag_index(require_documents=False)
            return self.chat_agent.ask_rag(message, stream=False)
        return self.chat_agent.ask_auto(message, stream=False)

    def answer_chat_selection(
        self,
        message: str,
        mode: str,
        *,
        source_scope_filter: str = "all",
        include_private_local: bool = True,
    ) -> str:
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
            # Strict grounded RAG (see answer_chat_selection_iter) — refuses
            # off-corpus questions rather than answering from general knowledge.
            return self.chat_agent.ask_rag(
                message,
                stream=False,
                source_scope_filter=source_scope_filter,
                include_private_local=include_private_local,
            )
        return self.chat_agent.ask_auto(message, stream=False)

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
        autosurvey_llm = build_autosurvey_llm(self.llm)
        self._research_progress.emit("term_grounding", "주제어 추출 중...")
        workspace_name, grounding = self._grounding_workspace_from_request(
            request,
            llm=autosurvey_llm,
        )
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
            autosurvey_llm=autosurvey_llm,
            embedding_llm=self.llm,
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

    def _grounding_workspace_from_request(
        self,
        request: str,
        *,
        llm=None,
    ) -> tuple[str, dict[str, Any] | None]:
        return workspace_paths.extract_workspace_name_from_request(
            request,
            llm=llm or self.llm,
        )

    def _reserve_workspace_dir(self, workspace_name: str) -> Path:
        return workspace_paths.reserve_workspace_dir(self.output_root, workspace_name)

    def _safe_workspace_name(self, name: str) -> str:
        return workspace_paths.safe_workspace_name(name)

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
        workspace_paths.cleanup_pending_dirs(self.output_root)

    def _discover_initial_workspace(self) -> Path | None:
        return workspace_paths.discover_initial_workspace(self.output_root)

    def _cleanup_empty_api_dir(self) -> None:
        workspace_paths.cleanup_empty_api_dir(self.output_root)

    def _ensure_rag_index(self, *, require_documents: bool) -> None:
        if self.rag_service.get_document_count(source_scope_filter="external") > 0:
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
            self.llm.check_embedding_endpoint()
        except Exception as exc:
            self._verify_progress.emit("failed", f"寃利??ㅽ뙣: {exc}", final=True)
            raise HTTPException(
                status_code=503,
                detail=str(exc),
            ) from exc

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
        """Start the screen poller; record assistant answers into the buffer.

        Wraps the on_answer callback to *also* drive a proactive observe on
        the first chunk of each intervention (``done=False`` mid-stream OR
        ``done=True`` final, whichever lands first). When proactive screen
        mode is enabled (``VERITAS_PROACTIVE_SCREEN=1``, default on) we
        rewrite the intervention's ``event_id`` to the ``pd_*`` decisionId so
        the frontend card renders 복사 / 거절 / 다시 and the legacy feedback
        endpoint forwards into the bandit canonical reward path.
        """
        seen_decisions: dict[str, str] = {}
        # 마지막으로 unresolved-card 게이트에 마킹한 카드 id — 같은 카드의
        # 스트리밍 청크마다 registry를 다시 부르지 않기 위한 1칸짜리 기억.
        last_marked_card: list[str] = [""]

        def on_answer(answer, intervention, done=True):  # type: ignore[no-untyped-def]
            retry_event_id = (
                str(intervention.get("retry_event_id") or "").strip()
                if isinstance(intervention, dict)
                else ""
            )
            if retry_event_id:
                # "다시" 재발화: 새 pd_ 카드를 만들지 않고 원래 카드 id를 재사용 →
                # 프론트가 같은 카드 내용을 갱신(upsert)한다. pd_ rewrite는 건너뛴다.
                intervention = dict(intervention)
                intervention["event_id"] = retry_event_id
            elif isinstance(intervention, dict) and proactive_screen_enabled():
                event_id = str(intervention.get("event_id") or "")
                decision_id = seen_decisions.get(event_id) if event_id else None
                if decision_id is None:
                    try:
                        orch = self.get_proactive_orchestrator()
                        decision_id = observe_screen_intervention(
                            orchestrator=orch,
                            intervention=intervention,
                            workspace_id=self.workspace_id,
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(f"[proactive][screen_bridge][warn] {exc}")
                        decision_id = None
                    if decision_id and event_id:
                        seen_decisions[event_id] = decision_id
                if decision_id:
                    # Swap eventId so feedback routes to /proactive/feedback.
                    # Keep the original under ``legacyEventId`` for traceback.
                    intervention = dict(intervention)
                    intervention["legacy_event_id"] = event_id
                    intervention["event_id"] = decision_id
            # 첫 non-empty 청크 = 카드가 실제 렌더되기 시작한 순간. 이 시점에
            # unresolved-card 게이트를 잠가, 사용자가 반응(또는 만료)하기 전에는
            # 캡처 루프가 새 개입을 스케줄하지 못하게 한다. 빈 답변(스킵된
            # 개입)은 카드가 안 생기므로 마킹하지 않는다. pd_* rewrite 이후라
            # 게이트에는 proactive id와 legacy id가 함께 등록된다.
            if str(answer or "").strip() and isinstance(intervention, dict):
                card_id = str(intervention.get("event_id") or "")
                # 매 청크마다 mark — answer(직전 제안 텍스트)를 게이트에 갱신해
                # retry의 avoid_text로 쓰게 한다. card_id가 처음일 때만 새 카드로
                # 등록되고(게이트 내부), 이후는 answer만 최신화된다.
                if card_id:
                    is_new = card_id != last_marked_card[0]
                    last_marked_card[0] = card_id
                    try:
                        self.registry.call(
                            "screen_context",
                            action="mark_card_shown",
                            intervention=intervention,
                            answer_text=str(answer or ""),
                        )
                    except Exception as exc:  # noqa: BLE001 — 게이트는 best-effort
                        if is_new:
                            print(f"[screen_context][card_gate][warn] mark failed: {exc}")
            self._screen_monitor.record_assist_answer(
                answer, intervention, workspace_id=self.workspace_id, done=done
            )

        return self._screen_monitor.start(on_answer=on_answer)

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

    def record_screen_feedback(
        self, *, event_id: str, intervention_type: str, action: str, reward: float
    ) -> dict[str, Any]:
        return self._screen_monitor.record_feedback(
            event_id=event_id,
            intervention_type=intervention_type,
            action=action,
            reward=reward,
        )

    def resolve_screen_card(self, *, event_id: str, action: str) -> dict[str, Any]:
        """Feedback이 도착한 카드를 unresolved-card 게이트에서 해제한다."""
        return self._screen_monitor.resolve_card(event_id=event_id, action=action)

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


def shutdown_runtime() -> None:
    """Stop the runtime's owned llama-servers if the runtime was built.

    Called from the API's uvicorn shutdown hook (graceful exits). Does not
    construct the runtime — a no-op if it was never built."""
    runtime = _runtime
    if runtime is not None:
        runtime.shutdown()
