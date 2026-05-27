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
        "--screen-debug",
        action="store_true",
        help="Turn on the screen-intervention decision trace (sets VERITAS_SCREEN_TRACE=1): "
        "emits [screen_debug] lines and attaches scenario/KB/prompt debug info to the "
        "frontend cards. Mirrors the launcher's --screen-debug for a standalone API run.",
    )
    return parser.parse_args()


def main() -> None:
    run_ui()


def cli_main() -> None:
    args = parse_args()
    # Set the trace env var before the app/agent starts so screen_trace_enabled()
    # (read live) picks it up. Harmless in UI mode — only the API path reads it.
    if args.screen_debug:
        os.environ["VERITAS_SCREEN_TRACE"] = "1"
        print("[api] screen-debug on (VERITAS_SCREEN_TRACE=1)", flush=True)
    if args.api:
        run_api(args.host, args.port, args.reload)
    else:
        main()


if __name__ == "__main__":
    cli_main()
