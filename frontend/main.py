from __future__ import annotations

import argparse
import sys
from pathlib import Path


def run_ui() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from frontend.ui.main import main as ui_main

    ui_main()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the standalone VERITAS frontend UI preview.")
    return parser.parse_args()


def main() -> None:
    run_ui()


def cli_main() -> None:
    parse_args()
    main()


if __name__ == "__main__":
    cli_main()
