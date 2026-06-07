"""TEMP diagnostic — full tracebacks to logs/thread_errors.log.

Captures request-thread exceptions that get turned into chat-error text and
background-worker exceptions that would otherwise be swallowed by print-only
handlers, so an intermittent runtime fault (e.g. "cannot release un-acquired
lock") leaves a localizable traceback. Remove once the fault is fixed.
"""

from __future__ import annotations

import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

_write_lock = threading.Lock()
_installed = False


def _log_path() -> Path:
    try:
        from db.db import get_app_data_dir

        base = get_app_data_dir() / "logs"
    except Exception:
        base = Path.home() / ".veritas" / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base / "thread_errors.log"


def log_thread_error(where: str, exc: BaseException | None = None) -> None:
    """Append a timestamped full traceback. Never raises."""
    try:
        if exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        else:
            tb = traceback.format_exc()
        stamp = datetime.now(timezone.utc).isoformat()
        thread = threading.current_thread().name
        block = f"\n===== {stamp} | where={where} | thread={thread} =====\n{tb}\n"
        with _write_lock:
            with _log_path().open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(block)
    except Exception:
        pass


def install_excepthooks() -> None:
    """Route uncaught thread/main-thread exceptions to thread_errors.log."""
    global _installed
    if _installed:
        return
    _installed = True

    def _thread_hook(args) -> None:  # type: ignore[no-untyped-def]
        log_thread_error(f"threading.excepthook:{args.thread.name}", args.exc_value)

    def _sys_hook(exc_type, exc_value, exc_tb) -> None:  # type: ignore[no-untyped-def]
        log_thread_error("sys.excepthook", exc_value)

    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass
    try:
        sys.excepthook = _sys_hook
    except Exception:
        pass


__all__ = ["log_thread_error", "install_excepthooks"]
