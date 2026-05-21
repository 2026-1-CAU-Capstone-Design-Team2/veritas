"""Editor document store + inline ghost-writing suggestion stream.

The standalone editor window talks to the backend only over HTTP/SSE (no Qt
coupling). This service owns three concerns:

1. Document CRUD on disk — drafts live at ``runs/<workspace>/drafts/<doc_id>.md``
   so they sit alongside the workspace's research artifacts and survive an API
   restart. Loading can also seed from the workspace's ``final.md`` (the
   "이 보고서로 글쓰기" entry point) without ever overwriting it.
2. Inline ghost-writing — a short streaming continuation of whatever the user
   is typing. Unlike the chat/document-assist streams it deliberately bypasses
   the RAG / tool-routing ChatAgent and calls ``LLMClient.iter_ask`` directly
   with a tiny ``max_tokens`` budget, because a ghost suggestion must be fast
   and is only a local continuation of the cursor context (no retrieval).
3. Export — delegated to :mod:`export_service` (pandoc wrapper).

SSE event shape mirrors :func:`draft_chat_service.send_chat_message_stream`
(start / delta / done / error) so the frontend worker can be a near-clone of
``ChatStreamWorker``.
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
    """Resolve a workspace id to its run directory.

    Mirrors the mapping the rest of the API uses: a real workspace is
    ``runs/<id>/`` while the ``"default"`` placeholder (no workspace selected
    yet) maps to ``runs/api/``.
    """
    workspace_id = str(workspace_id or "default").strip() or "default"
    root = _output_root()
    return root / workspace_id if workspace_id != "default" else root / "api"


def _drafts_dir(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / "drafts"


def _draft_path(workspace_id: str, doc_id: str) -> Path:
    safe = _safe_doc_id(doc_id)
    return _drafts_dir(workspace_id) / f"{safe}.md"


def _safe_doc_id(doc_id: str) -> str:
    """Keep a doc id to a flat, filesystem-safe token so it can never escape
    the drafts directory via ``..`` or path separators."""
    token = re.sub(r"[^A-Za-z0-9_-]+", "", str(doc_id or "").strip())
    if not token:
        raise HTTPException(status_code=422, detail="docId must not be empty")
    return token[:80]


def _title_from_content(content: str) -> str:
    """Best-effort document title: the first markdown heading, else the first
    non-empty line, capped — used for the 열기 list and the window title."""
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
        text = heading.group(1).strip() if heading else stripped
        return text[:80] if text else "제목 없음"
    return "제목 없음"


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
    """Load editor content.

    ``source`` is one of:
      * ``"new"``   — a blank document with a fresh id.
      * ``"final"`` — seed from the workspace ``final.md``; a *new* draft id is
        minted so saving never clobbers the canonical report.
      * ``"draft"`` — an existing ``drafts/<doc_id>.md`` (404 if missing).
    """
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
    """List saved drafts for the 열기 menu, newest first."""
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
    """Persist a draft to ``runs/<workspace>/drafts/<doc_id>.md``.

    Title is intentionally not stored in a sidecar — it is derived from the
    content's first heading on read, so the file is the single source of truth.
    """
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
    _ = (workspace_id, doc_id)  # reserved for future per-workspace export rules
    return export_service.export(content, fmt, output_path)


# ------------------------------------------------------------ ghost-writing

_SUGGEST_SYSTEM_PROMPT = (
    "당신은 한국어 문서 작성 보조기입니다. 사용자가 작성 중인 글의 커서 위치에서 "
    "자연스럽게 이어질 다음 텍스트만 출력하세요. 설명, 따옴표, 코드펜스(```), 머리말 없이 "
    "이어질 본문만 1~2문장 이내로 간결하게 작성합니다. 이미 작성된 문장을 반복하지 말고, "
    "문맥의 말투와 문체를 그대로 유지하세요."
)


def suggest_stream(
    workspace_id: str,
    prefix: str,
    suffix: str = "",
    max_tokens: int = 64,
) -> Iterator[bytes]:
    """Stream a short ghost-writing continuation as SSE.

    Events:
        event: start  data: {"suggestionId": "...", "workspaceId": "..."}
        event: delta  data: {"text": "<chunk>"}
        event: done   data: {"suggestionId": "...", "text": "<full>"}
        event: error  data: {"error": "..."}

    Calls ``LLMClient.iter_ask`` directly (no RAG / tools) with a tiny token
    budget so the suggestion is fast. The cursor context is the only input:
    the frontend sends ~500 chars before the cursor as ``prefix`` and a little
    after as ``suffix``.
    """
    # Defensive caps so a runaway client payload can't blow the prompt up; the
    # frontend already trims to ~500 chars of prefix.
    prefix = (prefix or "")[-2000:]
    suffix = (suffix or "")[:500]
    suggestion_id = new_id("sg")

    yield _sse("start", {"suggestionId": suggestion_id, "workspaceId": workspace_id})

    if not prefix.strip() and not suffix.strip():
        # Nothing to continue from — emit an empty, successful suggestion.
        yield _sse("done", {"suggestionId": suggestion_id, "text": ""})
        return

    user_prompt = f"[작성 중인 내용]\n{prefix}"
    if suffix.strip():
        user_prompt += f"\n\n[커서 뒤 내용]\n{suffix}"
    user_prompt += "\n\n[커서 위치에 이어서 작성할 텍스트만 출력]"

    collected: list[str] = []
    try:
        runtime = get_runtime()
        for chunk in runtime.llm.iter_ask(
            _SUGGEST_SYSTEM_PROMPT,
            user_prompt,
            reasoning=False,
            sampling_params={
                "temperature": 0.3,
                "top_p": 0.9,
                "max_tokens": max(8, min(256, int(max_tokens))),
            },
            stream_label="editor-suggest",
        ):
            if not chunk:
                continue
            collected.append(chunk)
            yield _sse("delta", {"text": chunk})
    except Exception as e:  # noqa: BLE001 — surfaced to the client as an SSE error
        yield _sse("error", {"error": f"{type(e).__name__}: {e}"})
        return

    text = "".join(collected).strip()
    yield _sse("done", {"suggestionId": suggestion_id, "text": text})


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
