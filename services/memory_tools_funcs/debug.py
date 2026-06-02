"""Optional verbose tracing for the memory pipeline.

Enabled by the ``VERITAS_MEMORY_DEBUG`` env var (set directly or by the
``--mem-debug`` CLI flag). Off by default: when disabled each call is a single
cheap env lookup and returns immediately, so production paths are unaffected.

Output:
- Always to stdout (picked up by the launcher's ``api.log`` when spawned).
- Additionally to a dedicated file when ``VERITAS_MEMORY_DEBUG_FILE`` is set
  (the ``--mem-debug-file <path>`` CLI flag sets this). The file is opened once
  and appended to under a lock so the background flush thread and the main turn
  cannot interleave a half-written line.

Output format (one line per event)::

    [memory][<category>] <message>

Categories: ``prepare``, ``commit``, ``context``, ``retrieval``, ``flush``,
``working``.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


_file_lock = threading.Lock()
_file_handle: TextIO | None = None
_file_path_opened: str | None = None


def mem_debug_enabled() -> bool:
    """Return whether memory debug tracing is on for this process."""
    return os.getenv("VERITAS_MEMORY_DEBUG", "0") == "1"


def _debug_file_path() -> str:
    """Return the configured trace file path, or '' when none is set."""
    return str(os.getenv("VERITAS_MEMORY_DEBUG_FILE", "") or "").strip()


def _file_for(path: str) -> TextIO | None:
    """Return a shared append handle for ``path``, (re)opening on change.

    Returns None if the file cannot be opened, so tracing degrades to
    stdout-only instead of breaking the memory pipeline.
    """
    global _file_handle, _file_path_opened
    if _file_handle is not None and _file_path_opened == path:
        return _file_handle
    # Path changed (e.g. workspace switch reconfigured it) or first use.
    if _file_handle is not None:
        try:
            _file_handle.close()
        except Exception:
            pass
        _file_handle = None
        _file_path_opened = None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        _file_handle = open(path, "a", encoding="utf-8")
        _file_path_opened = path
    except Exception as exc:  # noqa: BLE001 - tracing must never break the pipeline
        print(f"[memory][debug][warn] could not open trace file {path!r}: {exc}", flush=True)
        _file_handle = None
        _file_path_opened = None
    return _file_handle


def mem_debug(category: str, message: str) -> None:
    """Emit one memory-trace line when tracing is enabled.

    Always prints to stdout; also appends to the dedicated trace file when
    ``VERITAS_MEMORY_DEBUG_FILE`` is set.
    """
    if not mem_debug_enabled():
        return
    line = f"[memory][{category}] {message}"
    print(line, flush=True)

    path = _debug_file_path()
    if not path:
        return
    with _file_lock:
        handle = _file_for(path)
        if handle is None:
            return
        try:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            handle.write(f"{ts} {line}\n")
            handle.flush()
        except Exception as exc:  # noqa: BLE001
            print(f"[memory][debug][warn] trace file write failed: {exc}", flush=True)


def close_debug_file() -> None:
    """Close the dedicated trace file handle, if open."""
    global _file_handle, _file_path_opened
    with _file_lock:
        if _file_handle is not None:
            try:
                _file_handle.close()
            except Exception:
                pass
            _file_handle = None
            _file_path_opened = None
