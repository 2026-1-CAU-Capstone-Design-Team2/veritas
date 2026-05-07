from __future__ import annotations

import argparse


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
    return parser.parse_args()


def main() -> None:
    run_ui()


def cli_main() -> None:
    args = parse_args()
    if args.api:
        run_api(args.host, args.port, args.reload)
    else:
        main()


if __name__ == "__main__":
    cli_main()
