"""Force UTF-8 stdout/stderr so prints never crash on Korean Windows (cp949).

On Windows, when a process's stdout/stderr is a **pipe** rather than a console
(e.g. the API server spawned by ``launcher.py``, or any output captured by a
parent process), Python sets the stream encoding to the locale code page —
``cp949`` on a Korean install. A ``print`` of any character outside cp949 then
raises ``UnicodeEncodeError: 'cp949' codec can't encode character``. This bites
constantly because web-scraped document text is full of such characters: the
em-dash ``—`` (U+2014), en-dash ``–``, smart quotes ``“ ” ‘ ’``, ellipsis ``…``.

In the AutoSurvey document-cleanup pass it surfaced as
``LLM 정제 호출 실패: 'cp949' codec can't encode character '\\u2014' ...`` — a
``print`` inside the cleanup call (the retry-nudge log, whose Korean text
contains ``—``) blew up and was caught as an "LLM call failed" error.

:func:`force_utf8_stdio` reconfigures both streams to UTF-8 — which can encode
every Unicode code point — with ``errors="backslashreplace"`` as a final guard.
It is idempotent and a no-op when the stream is already UTF-8 or cannot be
reconfigured (detached / replaced by a non-text object). Call it once at each
process entry point, before any output. For launcher-spawned children,
``PYTHONUTF8=1`` in the child environment is the byte-zero equivalent and is set
in ``launcher.py`` so even import-time prints (before this runs) are safe.
"""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 in place (best effort, idempotent)."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        current = (getattr(stream, "encoding", "") or "").replace("-", "").lower()
        if current == "utf8":
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError, AttributeError):
            # Stream detached or not reconfigurable — leave it untouched.
            pass


__all__ = ["force_utf8_stdio"]
