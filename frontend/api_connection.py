"""Frontend ↔ API connection bootstrap (client side).

Establishes and verifies the HTTP connection the PySide UI uses to reach the
VERITAS API. By default the UI requires an *external* API server (started with
``python -m api --api``); if it is unreachable this raises
:class:`ApiUnavailableError` so a misconfiguration (e.g. a port mismatch)
surfaces immediately instead of being silently masked.

Set ``VERITAS_EMBED_API=1`` to opt into single-process convenience: the UI asks
the **api layer** (:func:`api.main.run_api`) to host the server in-process. The
frontend never references the ASGI app object itself — "how to run the API"
stays in the api layer, so this module is purely a *client-side* connection
concern despite living next to the UI.
"""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse
import urllib.request


class ApiUnavailableError(RuntimeError):
    """Raised when the external API server cannot be reached and the in-process
    fallback is not enabled. The frontend surfaces this to the user instead of
    silently masking a misconfiguration."""


def is_api_available(base_url: str, *, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/v1/fe/bootstrap", timeout=timeout):
            return True
    except Exception:
        return False


def _embed_enabled() -> bool:
    return os.getenv("VERITAS_EMBED_API", "0").strip().lower() in ("1", "true", "yes", "on")


def ensure_api_connection(base_url: str, *, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Ensure the frontend has a reachable API server, then return.

    Default (strict) behavior: the frontend talks to the **external** API server
    the user runs (``python -m api --api``). If that server is not reachable at
    ``base_url``, raise :class:`ApiUnavailableError` with actionable guidance so
    the misconfiguration is surfaced immediately.

    This used to *silently* start an in-process uvicorn whenever the external
    API was missing — with HTTP logs suppressed (``log_level="warning"``). A port
    mismatch (e.g. the API on :8000 while the frontend looked at :8001) therefore
    went unnoticed: the UI quietly talked to its own hidden in-process backend
    while the user's separate API server sat idle, receiving zero requests. That
    fallback is now **opt-in** and **loud**.

    Set ``VERITAS_EMBED_API=1`` to keep the single-process convenience (the UI
    auto-starts the backend in its own process). When enabled, the in-process
    server is started with a clear log line and **visible HTTP access logs**
    (``log_level="info"``) so it can never be invisible again.
    """
    if is_api_available(base_url):
        return

    if not _embed_enabled():
        raise ApiUnavailableError(
            f"API 서버에 연결할 수 없습니다: {base_url}\n\n"
            f"먼저 API 서버를 실행하세요:\n"
            f"    python -m api --api --port {port}\n\n"
            f"다른 주소를 사용하려면 VERITAS_API_BASE_URL 환경변수를 설정하세요.\n"
            f"UI 프로세스 안에서 백엔드를 함께 띄우려면 VERITAS_EMBED_API=1 로 실행하세요."
        )

    parsed = urlparse(base_url)
    if parsed.hostname:
        host = parsed.hostname
    if parsed.port:
        port = parsed.port

    print(
        f"[api][embed] 외부 API({base_url})를 찾지 못해 in-process 서버를 "
        f"{host}:{port} 에 기동합니다 (VERITAS_EMBED_API=1)."
    )

    def run_server() -> None:
        # Delegate to the api layer's own runner instead of launching
        # ``api.api:app`` here — the frontend should not know how the API app is
        # hosted (layer boundary). ``run_api`` uses uvicorn's default ``info``
        # log level, so embedded HTTP access logs stay visible. Imported lazily
        # so this api-layer dependency only exists on the opt-in embed path.
        from api.main import run_api

        run_api(host, port, False)

    thread = threading.Thread(target=run_server, name="veritas-api", daemon=True)
    thread.start()

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if is_api_available(base_url):
            print(f"[api][embed] in-process API 서버 준비 완료: {base_url}")
            return
        time.sleep(0.15)

    raise ApiUnavailableError(
        f"in-process API 서버가 제한 시간 내에 기동되지 않았습니다: {base_url}"
    )
