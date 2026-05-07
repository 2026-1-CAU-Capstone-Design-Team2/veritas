from __future__ import annotations

import argparse
from pathlib import Path

from llm.llama_server_llm import LLMClient
from services.rag_service import RAGService
from storage.vector_store import VectorStore
from tools.loader import build_registry
from workflows import AutoSurveyWorkflow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Veritas AutoSurvey + RAG runner")
    parser.add_argument("instruction", nargs="?", help="Natural language instruction or question")
    parser.add_argument("--output-dir", required=True, help="Root directory for outputs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--embed-host", default=None)
    parser.add_argument("--embed-port", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--scout-docs", type=int, default=3)
    parser.add_argument("--max-docs", type=int, default=15)
    parser.add_argument("--max-context", type=int, default=16384)
    parser.add_argument("--rag-results", type=int, default=5)
    parser.add_argument(
        "--phase",
        choices=["all", "plan", "collect", "summarize", "final", "rag"],
        default="all",
        help="Which phase to run (rag = enter RAG chat only)",
    )
    parser.add_argument("--force-plan", action="store_true")
    parser.add_argument("--overwrite-summaries", action="store_true")
    parser.add_argument("--stream-summary", action="store_true")
    parser.add_argument("--stream-reasoning", action="store_true")
    parser.add_argument("--no-trace-latency", action="store_true")
    parser.add_argument("--markdown-root", default=None, help="Root directory for markdown indexing")
    parser.add_argument("--no-rag", action="store_true", help="Skip RAG chat after survey completes")
    parser.add_argument("--reindex", action="store_true", help="Force re-indexing of documents into vector store")
    return parser.parse_args()


def build_rag_service(llm: LLMClient, output_dir: Path) -> RAGService:
    vector_store = VectorStore(
        persist_dir=output_dir / "chromadb",
        collection_name="research_docs",
    )
    return RAGService(
        llm=llm,
        vector_store=vector_store,
    )


def run_rag_chat(
    args: argparse.Namespace,
    rag_service: RAGService,
    output_dir: Path,
    run_store_service,
) -> None:
    existing_chunks = rag_service.vector_store.get_document_count()

    if args.reindex or existing_chunks == 0:
        markdown_root = (
            Path(args.markdown_root).expanduser().resolve()
            if args.markdown_root
            else output_dir
        )

        summary_dir = run_store_service.summary_dir
        has_summary_docs = summary_dir.exists() and any(summary_dir.glob("doc_*.md"))

        if has_summary_docs and markdown_root == output_dir:
            print("[info] Indexing AutoSurvey summaries for RAG...")
            indexed = rag_service.index_autosurvey_output(
                summary_dir=summary_dir,
                index_path=run_store_service.index_path,
                clear_first=True,
            )
        else:
            print(f"[info] Indexing markdown files under {markdown_root}...")
            indexed = rag_service.index_all_markdown(
                base_dir=markdown_root,
                clear_first=True,
            )

        if indexed == 0:
            print("[error] No documents were indexed. Check --output-dir/--markdown-root and generated summaries.")
            return
    else:
        print(f"[info] Using existing index ({existing_chunks} chunks)")

    rag_service.chat_loop()


def main() -> None:
    args = parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    llm = LLMClient(
        host=args.host,
        port=args.port,
        embed_host=args.embed_host,
        embed_port=args.embed_port,
        stream_summary=args.stream_summary,
        stream_reasoning=args.stream_reasoning,
        trace_latency=not args.no_trace_latency,
    )

    registry, run_store_service = build_registry(
        llm=llm,
        run_root=output_dir,
        batch_size=args.batch_size,
        max_context=args.max_context,
    )

    workflow = AutoSurveyWorkflow(
        registry=registry,
        run_store_service=run_store_service,
        max_docs=args.max_docs,
        collect_batch_size=args.batch_size,
        scout_docs=args.scout_docs,
    )

    rag_service = build_rag_service(llm, output_dir)

    print(f"[info] output directory = {output_dir}")

    if args.phase == "plan":
        if not args.instruction:
            raise SystemExit("instruction is required for --phase plan")
        plan = workflow.run_plan(args.instruction, force_plan=args.force_plan)
        print(f"[done] plan saved: {run_store_service.plan_path}")
        print(plan)
        return

    if args.phase == "collect":
        user_request = args.instruction or run_store_service.load_request()
        plan = workflow.run_plan(user_request, force_plan=args.force_plan)
        result = workflow.run_collect(plan)
        print(f"[done] collected {result['record_count']} record(s)")
        return

    if args.phase == "summarize":
        result = workflow.run_summarize(overwrite=args.overwrite_summaries)
        print("[done] summaries updated")
        print(result)
        return

    if args.phase == "final":
        user_request = args.instruction or run_store_service.load_request()
        result = workflow.run_final(user_request=user_request)
        print(f"[done] final report saved: {result['final_path']}")
        return

    if args.phase == "rag":
        run_rag_chat(args, rag_service, output_dir, run_store_service)
        return

    # Auto-detect mode: no instruction + existing markdown data → RAG only
    has_any_markdown = any(output_dir.rglob("*.md"))
    if not args.instruction and args.phase == "all":
        if has_any_markdown:
            print("[info] No instruction provided, but documents exist. Entering RAG mode.")
            run_rag_chat(args, rag_service, output_dir, run_store_service)
            return
        else:
            raise SystemExit("instruction is required (or use --phase rag with existing data)")

    # Full survey pipeline
    result = workflow.run_all(
        user_request=args.instruction,
        force_plan=args.force_plan,
        overwrite_summaries=args.overwrite_summaries,
    )
    print(f"[done] final report saved: {run_store_service.final_path}")
    print(result["final_result"])

    # Enter RAG chat after survey (unless --no-rag)
    if not args.no_rag:
        print("\n" + "=" * 60)
        print("Survey complete! Entering RAG chat mode...")
        print("=" * 60)
        run_rag_chat(args, rag_service, output_dir, run_store_service)


if __name__ == "__main__":
    main()