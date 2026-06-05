"""CLI: run the flat (one-shot) baseline over DRB tasks → DRB raw JSONL.

Mirrors :mod:`benchmarks.drb.veritas_runner` (same defaults, output path, work
root, env-driven generator, sidecar metadata) but drives
:func:`benchmarks.drb.flat_agent.run_flat_research` instead of AutoSurvey.

Fairness wiring:

* **Generator** — ``build_autosurvey_llm(LLMClient(...))``: the exact model the
  AutoSurvey arm generates with (local llama-server, or OpenAI when the env
  selects it).
* **Search** — the same ``WebSearchTool`` AutoSurvey's collect loop uses.
* **Fetch** — the same ``fetch_with_crawl4ai`` primitive, with the same
  ``--fetch-max-chars`` cap.

It imports no AutoSurvey orchestration (no workflow/AutoSurveyTool/QueryPlan/
cleanup/summarize/final-report tools). Run manually; not unit-tested.

    python -m benchmarks.drb.flat_runner --model-name flat_local_web_m15 \
        --max-docs 15 --search-query-count 5 --fetch-max-chars 100000 --limit 2 --resume
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import services.fetch_webpage_tool_funcs as fetch_funcs
from core.prompts.drb_benchmark import FLAT_QUERY_PROMPT, FLAT_REPORT_PROMPT
from core.stdio_utf8 import force_utf8_stdio
from llm.autosurvey_llm_factory import build_autosurvey_llm
from llm.llama_server_llm import LLMClient
from tools.loader import load_schema
from tools.web_search_tool import WebSearchTool

from benchmarks.drb import drb_io, drb_vendor
from benchmarks.drb.flat_agent import run_flat_research


def _csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [piece.strip() for piece in value.split(",") if piece.strip()]
    return items or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the flat web+LLM baseline over DRB tasks.")
    parser.add_argument("--drb-root", default=drb_vendor.DEFAULT_DRB_ROOT)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--query-file", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--work-root", default=None, help="Defaults to runs/drb/<model-name> (debug packets only).")
    parser.add_argument("--max-docs", type=int, default=15)
    parser.add_argument("--search-query-count", type=int, default=5)
    parser.add_argument("--results-per-query", type=int, default=10)
    parser.add_argument("--fetch-max-chars", type=int, default=100_000)
    parser.add_argument("--fetch-timeout", type=int, default=20)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--task-ids", default=None)
    parser.add_argument("--languages", default=None)
    parser.add_argument("--topics", default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--debug-packets", action="store_true", help="Write per-task source packets under the work root (never the official raw).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--parallel", type=int, default=None)
    return parser.parse_args(argv)


def _make_query_fn(llm):
    def query_fn(task_prompt: str, language: str, max_queries: int) -> list[str]:
        payload = json.dumps(
            {"task": task_prompt, "language": language, "max_queries": max_queries},
            ensure_ascii=False,
        )
        data = llm.ask_json(FLAT_QUERY_PROMPT, payload, reasoning=False)
        queries = data.get("queries") if isinstance(data, dict) else None
        return [str(q).strip() for q in (queries or []) if str(q).strip()]

    return query_fn


def _make_report_fn(llm):
    def report_fn(task_prompt: str, language: str, sources_block: str) -> str:
        payload = (
            f"Task language: {language or 'unknown'}\n\n"
            f"Research task:\n{task_prompt}\n\n"
            f"Sources:\n{sources_block}\n"
        )
        return llm.ask(FLAT_REPORT_PROMPT, payload, reasoning=True)

    return report_fn


def _make_search_fn(web_search_tool):
    def search_fn(query: str, num_results: int) -> list[dict]:
        result = web_search_tool.run(query=query, num_results=num_results)
        if not getattr(result, "success", False) or not isinstance(result.data, dict):
            return []
        return result.data.get("results", []) or []

    return search_fn


def _make_fetch_fn(timeout_sec: int):
    def fetch_fn(url: str, max_chars: int) -> dict:
        return fetch_funcs.fetch_with_crawl4ai(url, timeout_sec=timeout_sec, max_chars=max_chars)

    return fetch_fn


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

    llm = LLMClient(host=args.host, port=args.port, max_parallel=args.parallel)
    generator = build_autosurvey_llm(llm)
    provider = "local" if generator is llm else "openai"
    generator_model = str(getattr(generator, "model", "") or getattr(llm, "model", "") or "unknown")

    web_search_tool = WebSearchTool(schema=load_schema(
        Path(__file__).resolve().parents[2] / "tools" / "web_search_tool" / "tool_schema.json"
    ))

    query_fn = _make_query_fn(generator)
    report_fn = _make_report_fn(generator)
    search_fn = _make_search_fn(web_search_tool)
    fetch_fn = _make_fetch_fn(args.fetch_timeout)

    print(f"[drb][flat] model={args.model_name} provider={provider} generator={generator_model}")
    print(f"[drb][flat] tasks={len(tasks)} resume_skips={len(done)} raw={raw_path}")

    written = 0
    for task in tasks:
        if args.resume and str(task.id) in done:
            print(f"[drb][flat][skip] task {task.id} already done")
            continue

        started = time.monotonic()
        meta: dict = {
            "task_id": task.id,
            "language": task.language,
            "topic": task.topic,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "generator_provider": provider,
            "generator_model": generator_model,
            "budgets": {
                "max_docs": args.max_docs,
                "search_query_count": args.search_query_count,
                "results_per_query": args.results_per_query,
                "fetch_max_chars": args.fetch_max_chars,
            },
        }
        try:
            print(f"[drb][flat][run] task {task.id}")
            result = run_flat_research(
                task.prompt,
                language=task.language,
                query_fn=query_fn,
                search_fn=search_fn,
                fetch_fn=fetch_fn,
                report_fn=report_fn,
                max_docs=args.max_docs,
                search_query_count=args.search_query_count,
                results_per_query=args.results_per_query,
                fetch_max_chars=args.fetch_max_chars,
            )
            drb_io.append_official_row(raw_path, task.id, task.prompt, result.article)
            written += 1

            if args.debug_packets:
                packet_path = work_root / f"task_{task.id}.sources.json"
                packet_path.write_text(
                    json.dumps(
                        [{"number": s.number, "url": s.url, "title": s.title} for s in result.sources],
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

            meta.update(
                {
                    "success": True,
                    "elapsed_sec": round(time.monotonic() - started, 2),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "article_chars": len(result.article),
                    "queries": result.queries,
                    "warnings": result.warnings,
                    **result.stats,
                }
            )
            print(f"[drb][flat][done] task {task.id} fetched={result.stats.get('fetched')} chars={len(result.article)}")
        except Exception as e:  # noqa: BLE001 — record failure, keep going
            meta.update(
                {
                    "success": False,
                    "elapsed_sec": round(time.monotonic() - started, 2),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error": f"{type(e).__name__}: {e}",
                }
            )
            print(f"[drb][flat][fail] task {task.id}: {e}")
            traceback.print_exc()
        finally:
            drb_io.append_meta_row(raw_path, meta)

    print(f"[drb][flat] wrote {written} new article(s) to {raw_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
