"""Editor document store + SSE wiring for the editor's AI surfaces.

The standalone editor window talks to the backend only over HTTP/SSE (no Qt
coupling). This service owns:

1. Document CRUD on disk — drafts live at ``runs/<workspace>/drafts/<doc_id>.md``
   so they sit alongside the workspace's research artifacts and survive an API
   restart. Loading can also seed from the workspace's ``final.md`` without ever
   overwriting it.
2. SSE plumbing for ghost-writing / quick actions. The actual generation
   (and workspace RAG grounding) lives on the **ChatAgent** — same agent and
   workspace-bound ``rag_service`` the chat / document-assist pages use — reached
   through the runtime facades ``ghostwrite_iter`` / ``editor_assist_iter``.
   Prompt text lives in :mod:`core.prompts.editor`. This service only frames the
   stream (start / delta / done / error) and enforces the "editor workspace must
   be the active one to ground" guard so a stale editor never grounds against the
   wrong workspace's index. The editor's 문서 대화 now shares the main chat
   pipeline (``/api/v1/chat/messages/stream``) through the ChatBus.
3. Connected-sources count + export (pandoc, via :mod:`export_service`).

Ghost-writing fires on a continuation-moment heuristic (decided in the
ChatAgent) and uses *additive* RAG — like quick actions, it grounds in the
workspace index when available but never hard-gates on similarity.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

from fastapi import HTTPException

from ..api_common import new_id, utc_now_iso
from . import export_service
from .agent_runtime import get_runtime


# ---------------------------------------------------------------- filesystem

def _output_root() -> Path:
    return Path(os.getenv("VERITAS_OUTPUT_DIR", "runs")).expanduser().resolve()


def _workspace_dir(workspace_id: str) -> Path:
    workspace_id = str(workspace_id or "default").strip() or "default"
    root = _output_root()
    return root / workspace_id if workspace_id != "default" else root / "api"


def _drafts_dir(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / "drafts"


def _draft_path(workspace_id: str, doc_id: str) -> Path:
    return _drafts_dir(workspace_id) / f"{_safe_doc_id(doc_id)}.md"


def _safe_doc_id(doc_id: str) -> str:
    """Flat, filesystem-safe token so a doc id can never escape the drafts dir."""
    token = re.sub(r"[^A-Za-z0-9_-]+", "", str(doc_id or "").strip())
    if not token:
        raise HTTPException(status_code=422, detail="docId must not be empty")
    return token[:80]


def _title_from_content(content: str) -> str:
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
        text = heading.group(1).strip() if heading else stripped
        return text[:80] if text else "제목 없음"
    return "제목 없음"


def _workspace_is_active(workspace_id: str) -> bool:
    """True when the editor's workspace is the runtime's active one.

    Grounding reuses the ChatAgent's ``rag_service``, which is bound to the
    active workspace. If the editor was opened on a different workspace (e.g. the
    main app switched away), we must not ground against the wrong index.
    """
    try:
        runtime = get_runtime()
    except Exception:
        return False
    return str(workspace_id or "") == str(getattr(runtime, "workspace_id", ""))


# ----------------------------------------------------------------- documents

def new_document(workspace_id: str) -> dict[str, Any]:
    return {
        "docId": new_id("doc"),
        "workspaceId": workspace_id,
        "title": "제목 없음",
        "content": "",
        "source": "new",
    }


def load_document(
    workspace_id: str,
    source: str = "new",
    doc_id: str | None = None,
) -> dict[str, Any]:
    source = (source or "new").strip().lower()

    if source == "new":
        return new_document(workspace_id)

    if source == "final":
        final_path = _workspace_dir(workspace_id) / "final.md"
        if not final_path.exists():
            raise HTTPException(
                status_code=404,
                detail="이 워크스페이스에는 아직 final.md가 없습니다. 먼저 자료조사를 진행해 주세요.",
            )
        content = final_path.read_text(encoding="utf-8", errors="replace")
        return {
            "docId": new_id("doc"),
            "workspaceId": workspace_id,
            "title": _title_from_content(content),
            "content": content,
            "source": "final",
        }

    if source == "draft":
        if not doc_id:
            raise HTTPException(status_code=422, detail="source=draft 에는 docId가 필요합니다.")
        path = _draft_path(workspace_id, doc_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"draft '{doc_id}' 를 찾을 수 없습니다.")
        content = path.read_text(encoding="utf-8", errors="replace")
        return {
            "docId": _safe_doc_id(doc_id),
            "workspaceId": workspace_id,
            "title": _title_from_content(content),
            "content": content,
            "source": "draft",
        }

    raise HTTPException(status_code=422, detail=f"알 수 없는 source: {source}")


def list_documents(workspace_id: str) -> dict[str, Any]:
    drafts_dir = _drafts_dir(workspace_id)
    items: list[dict[str, Any]] = []
    if drafts_dir.exists():
        for path in drafts_dir.glob("*.md"):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                mtime = path.stat().st_mtime
            except Exception:
                continue
            items.append(
                {
                    "docId": path.stem,
                    "title": _title_from_content(content),
                    "updatedAt": _iso_from_mtime(mtime),
                    "_mtime": mtime,
                }
            )
    items.sort(key=lambda item: item.get("_mtime", 0.0), reverse=True)
    for item in items:
        item.pop("_mtime", None)
    return {"workspaceId": workspace_id, "items": items}


def save_document(
    workspace_id: str,
    doc_id: str,
    content: str,
    title: str | None = None,
) -> dict[str, Any]:
    path = _draft_path(workspace_id, doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = content or ""
    path.write_text(body, encoding="utf-8")
    return {
        "docId": _safe_doc_id(doc_id),
        "workspaceId": workspace_id,
        "title": title or _title_from_content(body),
        "savedAt": utc_now_iso(),
        "charCount": len(body),
        "path": str(path),
    }


def export_document(
    workspace_id: str,
    content: str,
    fmt: str,
    output_path: str,
    doc_id: str | None = None,
) -> dict[str, Any]:
    _ = (workspace_id, doc_id)
    return export_service.export(content, fmt, output_path)


# ------------------------------------------------------------ ghost-writing

def suggest_stream(
    workspace_id: str,
    prefix: str,
    suffix: str = "",
    max_tokens: int = 64,
    use_workspace: bool = True,
) -> Iterator[bytes]:
    """Stream a ghost-writing continuation as SSE. Generation lives on the
    ChatAgent, which decides whether the cursor is at a continuation moment;
    ``use_workspace`` enables *additive* RAG grounding when the index is active.
    """
    prefix = (prefix or "")[-2000:]
    suffix = (suffix or "")[:500]
    suggestion_id = new_id("sg")

    yield _sse("start", {"suggestionId": suggestion_id, "workspaceId": workspace_id})

    if not prefix.strip() and not suffix.strip():
        yield _sse("done", {"suggestionId": suggestion_id, "text": ""})
        return

    collected: list[str] = []
    try:
        runtime = get_runtime()
        # Additive RAG: ground only when this editor's workspace is the active
        # index (so we never ground against the wrong one); otherwise still offer
        # an ungrounded continuation. Whether to suggest at all is the agent's
        # continuation-moment decision, not a grounding gate.
        grounded = bool(use_workspace) and _workspace_is_active(workspace_id)
        for chunk in runtime.ghostwrite_iter(
            prefix, suffix, max_tokens=max_tokens, use_workspace=grounded
        ):
            if not chunk:
                continue
            collected.append(chunk)
            yield _sse("delta", {"text": chunk})
    except Exception as e:  # noqa: BLE001 — surfaced as SSE error
        yield _sse("error", {"error": f"{type(e).__name__}: {e}"})
        return

    yield _sse("done", {"suggestionId": suggestion_id, "text": "".join(collected).strip()})


# ---------------------------------------------------- assist (quick actions)

def assist_stream(
    workspace_id: str,
    action: str,
    text: str,
    max_tokens: int = 400,
    use_workspace: bool = True,
) -> Iterator[bytes]:
    """Stream a quick-action transform as SSE. Additive RAG (runs on the given
    text; grounds when the workspace index is available and active)."""
    action = (action or "").strip().lower()
    assist_id = new_id("as")
    yield _sse("start", {"assistId": assist_id, "action": action})

    body = (text or "").strip()
    if not body and action != "continue":
        yield _sse("done", {"assistId": assist_id, "text": ""})
        return

    collected: list[str] = []
    try:
        grounded = bool(use_workspace) and _workspace_is_active(workspace_id)
        for chunk in get_runtime().editor_assist_iter(
            action, body, max_tokens=max_tokens, use_workspace=grounded
        ):
            if not chunk:
                continue
            collected.append(chunk)
            yield _sse("delta", {"text": chunk})
    except Exception as e:  # noqa: BLE001 — surfaced as SSE error
        yield _sse("error", {"error": f"{type(e).__name__}: {e}"})
        return

    yield _sse("done", {"assistId": assist_id, "text": "".join(collected).strip()})


# --------------------------------------------------------- connected sources

def get_sources(workspace_id: str) -> dict[str, Any]:
    """Connected research sources for the 자료 panel — read from the workspace's
    ``summary/index.json`` (duplicates excluded)."""
    index_path = _workspace_dir(workspace_id) / "summary" / "index.json"
    items: list[dict[str, str]] = []
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        records = payload.get("records", []) if isinstance(payload, dict) else []
        for record in records:
            if not isinstance(record, dict) or record.get("duplicate_of"):
                continue
            url = str(record.get("final_url") or record.get("url") or "")
            title = str(record.get("title") or url or record.get("doc_id") or "Untitled")
            items.append({"title": title, "url": url})
    except Exception:
        pass
    return {"workspaceId": workspace_id, "count": len(items), "items": items}


# ---------------------------------------------------------------- internals

def _sse(event: str, payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")


def _iso_from_mtime(mtime: float) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(mtime, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
