from __future__ import annotations

import argparse
import os


def run_ui() -> None:
    from frontend.ui.main import main as ui_main

    ui_main()


def run_api(host: str, port: int, reload: bool) -> None:
    import uvicorn

    uvicorn.run("api.api:app", host=host, port=port, reload=reload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run VERITAS UI or API server.")
    parser.add_argument("--api", action="store_true", help="Run the FastAPI server instead of the UI.")
    parser.add_argument("--host", default="127.0.0.1", help="API server host.")
    parser.add_argument("--port", type=int, default=8000, help="API server port.")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload.")
    parser.add_argument(
        "--mem-debug",
        action="store_true",
        help="Trace the memory pipeline (prepare/commit/retrieval/flush) to stdout. "
        "Equivalent to setting VERITAS_MEMORY_DEBUG=1.",
    )
    parser.add_argument(
        "--mem-debug-file",
        nargs="?",
        const="logs/memory_trace.log",
        default=None,
        metavar="PATH",
        help="Also write the memory trace to a dedicated file (implies --mem-debug). "
        "Pass a path, or omit the value to use logs/memory_trace.log. "
        "Equivalent to setting VERITAS_MEMORY_DEBUG_FILE=<path>.",
    )
    return parser.parse_args()


def main() -> None:
    run_ui()


def cli_main() -> None:
    args = parse_args()
    # Set the env vars BEFORE the runtime is imported/constructed so every memory
    # component (including the reload subprocess) sees them. The env vars are the
    # single source of truth; the flags are just a convenience that sets them.
    # --mem-debug-file implies --mem-debug.
    mem_debug_file = getattr(args, "mem_debug_file", None)
    if getattr(args, "mem_debug", False) or mem_debug_file:
        os.environ["VERITAS_MEMORY_DEBUG"] = "1"
    if mem_debug_file:
        os.environ["VERITAS_MEMORY_DEBUG_FILE"] = mem_debug_file
    if args.api:
        run_api(args.host, args.port, args.reload)
    else:
        main()


if __name__ == "__main__":
    cli_main()
