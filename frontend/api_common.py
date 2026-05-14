from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


API_BASE_URL = os.getenv("VERITAS_API_BASE_URL", "http://127.0.0.1:8000")

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
    },
}


class ApiError(RuntimeError):
    pass


class ApiClient:
    def __init__(self, base_url: str = API_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")

    def get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self._url(path, query)
        request = urllib.request.Request(url, method="GET")
        return self._send_json(request)

    def post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url(path),
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        return self._send_json(request, timeout=timeout)

    def delete(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(self._url(path, query), method="DELETE")
        return self._send_json(request)

    def put(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url(path),
            data=data,
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        return self._send_json(request)

    def stream_post_sse(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """POST a JSON body and yield decoded SSE (event, data) tuples.

        Server-Sent Events format: 'event: <name>\\ndata: <json>\\n\\n'.
        Non-JSON data lines are yielded with the raw string under data['_raw'].
        """
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url(path),
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        try:
            response = urllib.request.urlopen(request, timeout=600)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="ignore")
            raise ApiError(self._error_message(raw) or f"HTTP {e.code}") from e
        except Exception as e:
            raise ApiError(str(e)) from e

        with response:
            event_name = "message"
            data_buffer: list[str] = []
            while True:
                line = response.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
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

    def upload_files(self, path: str, files: list[Path]) -> dict[str, Any]:
        boundary = f"----veritas-{uuid4().hex}"
        body = bytearray()
        for file_path in files:
            name = file_path.name
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                (
                    'Content-Disposition: form-data; name="files"; '
                    f'filename="{name}"\r\n'
                    "Content-Type: application/octet-stream\r\n\r\n"
                ).encode("utf-8")
            )
            body.extend(file_path.read_bytes())
            body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        request = urllib.request.Request(
            self._url(path),
            data=bytes(body),
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        return self._send_json(request)

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        from urllib.parse import quote, urlencode

        normalized = path if path.startswith("/") else f"/{path}"
        # Percent-encode the path so non-ASCII segments survive http.client's
        # ASCII-only request line. Workspace ids come from term-grounding and
        # are typically Korean (e.g. /api/v1/workspaces/<한글 id>); passing the
        # raw string through raised "'ascii' codec can't encode characters".
        # `safe="/"` keeps the path separators intact, and FastAPI/Starlette
        # percent-decodes path params on the way in, so the route still sees
        # the original id. Plain-ASCII paths are unaffected (quote is a no-op).
        normalized = quote(normalized, safe="/")
        url = f"{self.base_url}{normalized}"
        if not query:
            return url
        cleaned = {k: v for k, v in query.items() if v is not None}
        return f"{url}?{urlencode(cleaned)}" if cleaned else url

    def _send_json(
        self,
        request: urllib.request.Request,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=timeout or 600) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="ignore")
            raise ApiError(self._error_message(raw) or f"HTTP {e.code}") from e
        except Exception as e:
            raise ApiError(str(e)) from e

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
    try:
        load_bootstrap_state()
    except Exception:
        pass
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
