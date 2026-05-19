from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

import httpx


API_BASE_URL = os.getenv("VERITAS_API_BASE_URL", "http://127.0.0.1:8001")

# Default per-request ceiling. AutoSurvey and the chat stream pass their own
# longer timeouts; everything else inherits this.
_DEFAULT_TIMEOUT = 600.0

STATE: dict[str, object] = {
    "current_workspace_id": "default",
    "workspaces": [
        {
            "workspaceId": "default",
            "name": "default",
            "detail": "기본 워크스페이스",
            "status": "active",
        }
    ],
    "ui_state": {
        "workspaceId": "default",
        "workspaceName": "default",
    },
    "settings": {
        "model": {
            "modelName": "0.8B",
        },
        "localAccess": {
            "folderPaths": [],
        },
        "documentTools": {
            "custom": [],
        },
        # AutoSurvey pacing (설정 > 고급 설정 > 조사 진행 방식). The real values
        # come from the backend via /fe/bootstrap — load_bootstrap_state()
        # replaces STATE["settings"] wholesale, so this default only seeds the
        # pre-bootstrap window.
        "research": {
            "sampleCount": 3,
            "planCount": 5,
        },
    },
}


class ApiError(RuntimeError):
    pass


class ApiClient:
    """HTTP client for the local VERITAS API.

    Backed by a single shared :class:`httpx.Client`, so TCP connections are
    pooled and kept alive across calls instead of paying a fresh connection per
    request. ``httpx.Client`` is safe to share across threads, which matters
    here: the frontend issues requests from several worker threads (chat
    stream, progress poller, detached loaders).
    """

    def __init__(self, base_url: str = API_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        # follow_redirects mirrors urllib's default behaviour (FastAPI 307s on
        # trailing-slash mismatches); httpx does not follow redirects otherwise.
        self._client = httpx.Client(
            timeout=_DEFAULT_TIMEOUT, follow_redirects=True
        )

    def get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("GET", self._url(path, query))

    def post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._request_json(
            "POST", self._url(path), payload=payload, timeout=timeout
        )

    def delete(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("DELETE", self._url(path, query))

    def put(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("PUT", self._url(path), payload=payload)

    def stream_post_sse(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """POST a JSON body and yield decoded SSE (event, data) tuples.

        Server-Sent Events format: 'event: <name>\\ndata: <json>\\n\\n'.
        Non-JSON data lines are yielded with the raw string under data['_raw'].
        """
        data = json.dumps(payload or {}, ensure_ascii=False)
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        try:
            with self._client.stream(
                "POST",
                self._url(path),
                content=data,
                headers=headers,
                timeout=600.0,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise ApiError(
                        self._error_message(response.text)
                        or f"HTTP {response.status_code}"
                    )
                event_name = "message"
                data_buffer: list[str] = []
                for line in response.iter_lines():
                    text = line.rstrip("\r\n")
                    if text == "":
                        if data_buffer:
                            raw_data = "\n".join(data_buffer)
                            try:
                                decoded: dict[str, Any] = json.loads(raw_data)
                                if not isinstance(decoded, dict):
                                    decoded = {"value": decoded}
                            except json.JSONDecodeError:
                                decoded = {"_raw": raw_data}
                            yield event_name, decoded
                        event_name = "message"
                        data_buffer = []
                        continue
                    if text.startswith(":"):
                        continue
                    if text.startswith("event:"):
                        event_name = text[len("event:") :].strip() or "message"
                    elif text.startswith("data:"):
                        data_buffer.append(text[len("data:") :].lstrip())
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(str(e)) from e

    def upload_files(self, path: str, files: list[Path]) -> dict[str, Any]:
        # httpx builds the multipart/form-data body (boundary, headers) itself.
        file_parts = [
            (
                "files",
                (file_path.name, file_path.read_bytes(), "application/octet-stream"),
            )
            for file_path in files
        ]
        try:
            response = self._client.post(self._url(path), files=file_parts)
        except Exception as e:
            raise ApiError(str(e)) from e
        return self._handle_response(response)

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        from urllib.parse import quote, urlencode

        normalized = path if path.startswith("/") else f"/{path}"
        # Percent-encode the path so non-ASCII segments are transmitted safely.
        # Workspace ids come from term-grounding and are typically Korean (e.g.
        # /api/v1/workspaces/<한글 id>). `safe="/"` keeps the path separators
        # intact, and FastAPI/Starlette percent-decodes path params on the way
        # in, so the route still sees the original id. Plain-ASCII paths are
        # unaffected (quote is a no-op), and httpx leaves existing %XX escapes
        # alone rather than double-encoding them.
        normalized = quote(normalized, safe="/")
        url = f"{self.base_url}{normalized}"
        if not query:
            return url
        cleaned = {k: v for k, v in query.items() if v is not None}
        return f"{url}?{urlencode(cleaned)}" if cleaned else url

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        content: str | None = None
        headers: dict[str, str] = {}
        if method in ("POST", "PUT"):
            # Match the previous behaviour: POST/PUT always send a JSON body,
            # even an empty {}. ensure_ascii=False keeps Korean readable on the
            # wire (the server decodes either form correctly).
            content = json.dumps(payload or {}, ensure_ascii=False)
            headers["Content-Type"] = "application/json"
        try:
            response = self._client.request(
                method,
                url,
                content=content,
                headers=headers,
                timeout=timeout or _DEFAULT_TIMEOUT,
            )
        except Exception as e:
            raise ApiError(str(e)) from e
        return self._handle_response(response)

    def _handle_response(self, response: httpx.Response) -> dict[str, Any]:
        raw = response.text
        if response.status_code >= 400:
            raise ApiError(
                self._error_message(raw) or f"HTTP {response.status_code}"
            )
        if not raw.strip():
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ApiError(f"Invalid JSON response: {e}") from e
        if isinstance(payload, dict):
            return payload
        return {"data": payload}

    def _error_message(self, raw: str) -> str:
        try:
            payload = json.loads(raw)
        except Exception:
            return raw.strip()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or "")
        return raw.strip()


api_client = ApiClient()


def default_workspace() -> dict[str, str]:
    return {
        "workspaceId": "default",
        "name": "default",
        "detail": "기본 워크스페이스",
        "status": "active",
    }


def load_bootstrap_state() -> dict[str, Any]:
    bootstrap = api_client.get("/api/v1/fe/bootstrap")
    workspaces = bootstrap.get("workspaces")
    if not isinstance(workspaces, list) or not workspaces:
        workspaces = [default_workspace()]

    current_workspace_id = str(bootstrap.get("currentWorkspaceId") or "default")
    if not any(
        isinstance(item, dict) and item.get("workspaceId") == current_workspace_id
        for item in workspaces
    ):
        current_workspace_id = str(workspaces[0].get("workspaceId") or "default")

    settings = bootstrap.get("settings")
    if isinstance(settings, dict):
        STATE["settings"] = settings
    STATE["workspaces"] = workspaces
    STATE["current_workspace_id"] = current_workspace_id
    STATE["ui_state"] = {
        **dict(STATE.get("ui_state") or {}),
        "workspaceId": current_workspace_id,
        "workspaceName": _workspace_name(workspaces, current_workspace_id),
    }
    return bootstrap


def current_workspace_id() -> str:
    """Return the cached current workspace id — never blocks.

    This is read on the UI thread on every page navigation and message send, so
    it must not do I/O. The cache is seeded at startup (``frontend.ui.main``
    calls :func:`load_bootstrap_state` before the window opens) and kept fresh
    afterwards by :func:`switch_workspace` and the explicit
    :func:`load_bootstrap_state` calls that follow every workspace-mutating
    operation (research completion, workspace creation/deletion). Callers that
    must see a guaranteed-fresh value should call :func:`load_bootstrap_state`
    on a worker thread first.
    """
    return str(STATE.get("current_workspace_id") or "default")


def switch_workspace(workspace_id: str) -> str:
    response = api_client.post("/api/v1/workspaces/switch", {"workspaceId": workspace_id})
    selected_id = str(response.get("workspaceId") or workspace_id)
    selected_name = str(response.get("name") or selected_id)
    STATE["current_workspace_id"] = selected_id
    ui_state = dict(STATE.get("ui_state") or {})
    ui_state.update({"workspaceId": selected_id, "workspaceName": selected_name})
    STATE["ui_state"] = ui_state
    return selected_name


def _workspace_name(workspaces: list[Any], workspace_id: str) -> str:
    for item in workspaces:
        if isinstance(item, dict) and item.get("workspaceId") == workspace_id:
            return str(item.get("name") or workspace_id)
    return workspace_id
