"""CLI: run Veritas AutoSurvey over DRB tasks → DRB raw JSONL.

Wires AutoSurvey directly the way ``main.py`` does (``LLMClient`` →
``build_autosurvey_llm`` → ``build_registry`` → ``AutoSurveyConfig.from_env`` →
``AutoSurveyWorkflow.run_all``), one fresh workspace per task under
``runs/drb/<model_name>/task_<id>/``, then exports each workspace's ``final.md``
to a DRB article via :mod:`benchmarks.drb.citation_adapter`.

It deliberately does **not** go through the chat-facing ``AutoSurveyTool`` and
injects no chat memory, RAG state, screen context, local private docs, or
proactive state — this is the clean, request-only research path so the benchmark
measures the AutoSurvey *algorithm*, not chat affordances.

Run it manually (see INSTRUCTION.md); it is not exercised by unit tests.

    python -m benchmarks.drb.veritas_runner --model-name veritas_autosurvey_local_m15 \
        --max-docs 15 --scout-docs 3 --batch-size 5 --limit 2 --resume
"""

from __future__ import annotations

import argparse
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from core.stdio_utf8 import force_utf8_stdio
from llm.autosurvey_llm_factory import build_autosurvey_llm
from llm.llama_server_llm import LLMClient
from tools.loader import build_registry
from workflows import AutoSurveyConfig, AutoSurveyWorkflow

from benchmarks.drb import drb_io, drb_vendor
from benchmarks.drb.citation_adapter import export_workspace_to_article


def _csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [piece.strip() for piece in value.split(",") if piece.strip()]
    return items or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Veritas AutoSurvey over DRB tasks.")
    parser.add_argument("--drb-root", default=drb_vendor.DEFAULT_DRB_ROOT)
    parser.add_argument("--model-name", required=True, help="DRB target model name (file stem).")
    parser.add_argument("--query-file", default=None, help="Defaults to <drb-root>/data/prompt_data/query.jsonl")
    parser.add_argument("--output", default=None, help="Defaults to <drb-root>/data/test_data/raw_data/<model-name>.jsonl")
    parser.add_argument("--work-root", default=None, help="Defaults to runs/drb/<model-name>")
    # Shared research budget (kept identical to the flat baseline for fairness).
    parser.add_argument("--max-docs", type=int, default=15)
    parser.add_argument("--scout-docs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--fetch-max-chars", type=int, default=100_000)
    parser.add_argument("--max-context", type=int, default=16384)
    # Task selection.
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--task-ids", default=None, help="Comma-separated task ids.")
    parser.add_argument("--languages", default=None, help="Comma-separated languages (zh,en).")
    parser.add_argument("--topics", default=None, help="Comma-separated topics.")
    parser.add_argument("--resume", action="store_true", help="Skip tasks already in the raw output.")
    # llama-server connection.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--embed-host", default=None)
    parser.add_argument("--embed-port", type=int, default=8081)
    parser.add_argument("--parallel", type=int, default=None)
    return parser.parse_args(argv)


def _collect_counts(run_store_service) -> dict[str, int]:
    """Best-effort kept/duplicate/rejected/fetch-error counts from the run store."""
    try:
        records = run_store_service.load_records()
        kept = sum(1 for r in records if r.duplicate_of is None)
        duplicates = sum(1 for r in records if r.duplicate_of is not None)
        summary_dir = run_store_service.summary_dir
        rejected = len(list(summary_dir.glob("rejected_*.md")))
        fetch_errors = len(list(summary_dir.glob("fetch_error_*.md")))
        return {
            "kept_docs": kept,
            "duplicates": duplicates,
            "rejected": rejected,
            "fetch_errors": fetch_errors,
        }
    except Exception:  # noqa: BLE001 — metadata only
        return {}


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    args = parse_args(argv)

    drb_root = drb_vendor.validate_layout(args.drb_root)
    query_file = Path(args.query_file) if args.query_file else drb_vendor.query_file_path(drb_root)
    raw_path = Path(args.output) if args.output else drb_vendor.raw_output_path(args.model_name, drb_root)
    work_root = Path(args.work_root) if args.work_root else Path("runs/drb") / args.model_name
    work_root.mkdir(parents=True, exist_ok=True)

    tasks = drb_io.load_tasks(
        query_file,
        limit=args.limit,
        task_ids=_csv(args.task_ids),
        languages=_csv(args.languages),
        topics=_csv(args.topics),
    )
    done = drb_io.completed_task_ids(raw_path) if args.resume else set()

    # The generator + config are shared across tasks; only the per-task workspace
    # (registry / run store / workflow) is rebuilt so each task is isolated.
    llm = LLMClient(host=args.host, port=args.port, embed_host=args.embed_host,
                    embed_port=args.embed_port, max_parallel=args.parallel)
    autosurvey_llm = build_autosurvey_llm(llm)
    # build_autosurvey_llm returns the local client unchanged for the local
    # provider, or an OpenAI client otherwise — identity is the reliable signal.
    provider = "local" if autosurvey_llm is llm else "openai"
    generator_model = str(getattr(autosurvey_llm, "model", "") or getattr(llm, "model", "") or "unknown")
    config = AutoSurveyConfig.from_env(
        max_docs=args.max_docs,
        collect_batch_size=args.batch_size,
        scout_docs=args.scout_docs,
        fetch_max_chars=args.fetch_max_chars,
    )

    print(f"[drb][veritas] model={args.model_name} provider={provider} generator={generator_model}")
    print(f"[drb][veritas] tasks={len(tasks)} resume_skips={len(done)} raw={raw_path}")

    written = 0
    for task in tasks:
        if args.resume and str(task.id) in done:
            print(f"[drb][veritas][skip] task {task.id} already done")
            continue

        workspace = (work_root / f"task_{task.id}").resolve()
        started = time.monotonic()
        start_iso = datetime.now(timezone.utc).isoformat()
        meta: dict = {
            "task_id": task.id,
            "language": task.language,
            "topic": task.topic,
            "workspace": str(workspace),
            "started_at": start_iso,
            "generator_provider": provider,
            "generator_model": generator_model,
            "budgets": {
                "max_docs": args.max_docs,
                "scout_docs": args.scout_docs,
                "batch_size": args.batch_size,
                "fetch_max_chars": args.fetch_max_chars,
            },
        }
        try:
            registry, run_store_service, _rag = build_registry(
                llm=llm,
                run_root=workspace,
                autosurvey_llm=autosurvey_llm,
                embedding_llm=llm,
                batch_size=config.collect_batch_size,
                max_context=args.max_context,
                enable_screen_context=False,
            )
            workflow = AutoSurveyWorkflow(
                registry=registry,
                run_store_service=run_store_service,
                config=config,
            )
            print(f"[drb][veritas][run] task {task.id} → {workspace}")
            workflow.run_all(user_request=task.prompt, force_plan=False, overwrite_summaries=False)

            article, warnings = export_workspace_to_article(workspace)
            drb_io.append_official_row(raw_path, task.id, task.prompt, article)
            written += 1

            meta.update(
                {
                    "success": True,
                    "elapsed_sec": round(time.monotonic() - started, 2),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "article_chars": len(article),
                    "warnings": warnings,
                    **_collect_counts(run_store_service),
                }
            )
            print(f"[drb][veritas][done] task {task.id} chars={len(article)} warnings={len(warnings)}")
        except Exception as e:  # noqa: BLE001 — record failure, keep going
            meta.update(
                {
                    "success": False,
                    "elapsed_sec": round(time.monotonic() - started, 2),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error": f"{type(e).__name__}: {e}",
                }
            )
            print(f"[drb][veritas][fail] task {task.id}: {e}")
            traceback.print_exc()
        finally:
            drb_io.append_meta_row(raw_path, meta)

    print(f"[drb][veritas] wrote {written} new article(s) to {raw_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
