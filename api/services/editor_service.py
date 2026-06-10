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
ChatAgent) and uses *additive* RAG — it grounds in the workspace index when
available but never hard-gates on similarity. Quick actions, by contrast, split
into two groups: the forced-RAG ones (rewrite / continue) are hard-gated and
refuse (``EditorGroundingUnavailable``) when no grounding is available, while
the rest (summarize / polish / grammar) run as plain LLM calls.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

from fastapi import HTTPException

from agent import EditorGroundingUnavailable
from agent.chat_agent import strip_prefix_echo

from ..api_common import new_id, utc_now_iso
from ..api_models import ProactiveGenerateRequest, ProactiveObserveRequest
from . import export_service, proactive_service
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
    document_cursor: int | None = None,
    section_heading: str = "",
) -> Iterator[bytes]:
    """Stream a ghost-writing continuation as SSE.

    Per the proactive-bandit plan (veritas_bandit_policy_implementation_guide.md
    §16), this endpoint is now a **thin wrapper** over the unified proactive
    pipeline:

        observe(surface="native_editor") → if should_intervene → generate_stream

    The bandit gates whether to suggest at all (engage policy). When it
    picks ``no_op``, this stream emits a ``done`` with empty text so existing
    frontend callers don't break — they just see "no suggestion this time",
    same as if the ChatAgent decided this wasn't a continuation moment.

    ``decisionId`` is included in every SSE event so the frontend can call
    ``/api/v1/proactive/feedback`` with the same id when the user accepts
    (TAB) or rejects (ESC) the suggestion. New frontends should prefer the
    direct ``/api/v1/proactive/*`` routes — this wrapper exists for backward
    compatibility with the existing native editor.
    """
    prefix = (prefix or "")[-2000:]
    suffix = (suffix or "")[:500]
    suggestion_id = new_id("sg")

    if not prefix.strip() and not suffix.strip():
        yield _sse("start", {"suggestionId": suggestion_id, "workspaceId": workspace_id})
        yield _sse("done", {"suggestionId": suggestion_id, "text": ""})
        return

    # Build a synthetic observation from the cursor context. ``current_*``
    # fields are derived best-effort from the prefix so the bandit's primitive
    # features have something to chew on. The full ``text`` we send is the
    # prefix+suffix concatenation since the legacy API never carried the
    # whole document — and that's fine, because the policy's feature
    # extraction only ever looks at lengths, not the body.
    text = f"{prefix}{suffix}"
    current_paragraph = _last_paragraph(prefix) or prefix[-400:]
    current_sentence = _last_sentence(prefix)
    payload = ProactiveObserveRequest(
        surface="native_editor",
        workspaceId=workspace_id,
        documentKey=workspace_id,  # caller didn't provide a doc-level key
        text=text,
        cursor=len(prefix),
        # ``cursor`` above is the caret offset within the truncated prefix window
        # (what features.py wants). ``documentCursor`` is the caret's TRUE offset
        # in the whole document, which the reject ladder keys "same spot" on — it
        # must not be window-clamped, or one spot's cooldown freezes the whole
        # doc. Fall back to len(prefix) only when the caller didn't send it.
        documentCursor=(document_cursor if document_cursor is not None else len(prefix)),
        prefix=prefix,
        suffix=suffix,
        currentSentence=current_sentence,
        currentParagraph=current_paragraph,
        previousParagraph="",
        confidence=float(use_workspace),
        # The editor knows the whole document, so it passes the heading of the
        # section the cursor sits under — the ghost generator injects it as
        # ``[현재 섹션 제목]`` to keep the continuation on the section's topic.
        metadata={"section_heading": (section_heading or "").strip()},
    )

    try:
        decision_dict = proactive_service.observe(payload)
    except Exception as e:  # noqa: BLE001 — surfaced as SSE error
        yield _sse("start", {"suggestionId": suggestion_id, "workspaceId": workspace_id})
        yield _sse("error", {"error": f"{type(e).__name__}: {e}"})
        return

    decision_id = str(decision_dict.get("decisionId") or "")
    should_intervene = bool(decision_dict.get("shouldIntervene"))
    task = decision_dict.get("task") or {}
    suggestion_type = task.get("taskType") if isinstance(task, dict) else None

    yield _sse(
        "start",
        {
            "suggestionId": suggestion_id,
            "workspaceId": workspace_id,
            "decisionId": decision_id,
            "shouldIntervene": should_intervene,
            # Keep ``suggestionType`` for backward-compat with the existing
            # native frontend; the rule-based system calls this ``taskType``
            # internally but the wire field name stays the same.
            "suggestionType": suggestion_type,
            "taskType": suggestion_type,
            "renderMode": task.get("renderMode") if isinstance(task, dict) else None,
        },
    )

    if not should_intervene:
        yield _sse(
            "done",
            {
                "suggestionId": suggestion_id,
                "decisionId": decision_id,
                "text": "",
                "shouldIntervene": False,
            },
        )
        return

    collected: list[str] = []
    try:
        gen_payload = ProactiveGenerateRequest(decisionId=decision_id)
        for raw in proactive_service.generate_stream(gen_payload):
            event_name, event_payload = _parse_sse_bytes(raw)
            if event_name == "delta":
                chunk = str(event_payload.get("text") or "")
                if chunk:
                    collected.append(chunk)
                    yield _sse("delta", {"text": chunk})
            elif event_name == "error":
                yield _sse("error", {"error": event_payload.get("error", "unknown")})
                return
            elif event_name in ("start", "target", "done"):
                # `start`/`target` are useful diagnostics for the new frontend
                # but the legacy ghostwriting UI only consumes delta/done.
                # We forward `target` so an inline-diff renderer can pick it up.
                if event_name == "target":
                    yield _sse("target", event_payload)
                continue
    except Exception as e:  # noqa: BLE001
        yield _sse("error", {"error": f"{type(e).__name__}: {e}"})
        return

    # Strip a leading echo of the prefix's trailing word(s)/marker on the FULL
    # collected text — robust to multi-word echoes that the per-chunk streaming
    # strip (capped at the decision window) can't catch. Idempotent.
    text_out = strip_prefix_echo(prefix, "".join(collected))
    # Preserve a single leading space (the model is told to prefix one when the
    # continuation starts a new word) so the suggestion never glues onto the
    # prefix; only trailing/newline padding is trimmed.
    text_out = text_out.strip("\n").rstrip()
    if text_out[:1].isspace():
        text_out = " " + text_out.lstrip()
    yield _sse(
        "done",
        {
            "suggestionId": suggestion_id,
            "decisionId": decision_id,
            "text": text_out,
            "shouldIntervene": True,
        },
    )


# ---------------------------------------------------- assist (quick actions)

def assist_stream(
    workspace_id: str,
    action: str,
    text: str,
    max_tokens: int = 800,
    use_workspace: bool = True,
) -> Iterator[bytes]:
    """Stream a quick-action transform as SSE.

    Grounding no longer depends on the editor's RAG toggle (``use_workspace``):
    the forced-RAG actions (rewrite / continue) are hard-gated on workspace
    grounding in the agent and refuse to run when none is available, while every
    other action runs as a plain LLM call. We only pass whether this editor's
    workspace is the active index, so a stale editor never grounds against the
    wrong one.
    """
    action = (action or "").strip().lower()
    assist_id = new_id("as")
    yield _sse("start", {"assistId": assist_id, "action": action})

    body = (text or "").strip()
    if not body and action != "continue":
        yield _sse("done", {"assistId": assist_id, "text": ""})
        return

    collected: list[str] = []
    try:
        grounded = _workspace_is_active(workspace_id)
        for chunk in get_runtime().editor_assist_iter(
            action, body, max_tokens=max_tokens, use_workspace=grounded
        ):
            if not chunk:
                continue
            collected.append(chunk)
            yield _sse("delta", {"text": chunk})
    except EditorGroundingUnavailable as e:
        # Expected hard-gate refusal for a forced-RAG action with no grounding —
        # surface the plain Korean reason (no "Type:" prefix).
        yield _sse("error", {"error": str(e)})
        return
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


# Cheap end-of-prefix splitters — same heuristic as the proactive context
# selector but inlined here so the legacy wrapper doesn't reach across
# packages for two regexes.
_PARA_SPLIT = re.compile(r"\n{2,}")
_SENT_SPLIT = re.compile(r"(?<=[\.\?\!。？！])\s+")


def _last_paragraph(text: str) -> str:
    if not text:
        return ""
    parts = _PARA_SPLIT.split(text)
    for chunk in reversed(parts):
        if chunk.strip():
            return chunk.strip()
    return ""


def _last_sentence(text: str) -> str:
    if not text:
        return ""
    paragraph = _last_paragraph(text)
    sents = [s.strip() for s in _SENT_SPLIT.split(paragraph) if s.strip()]
    return sents[-1] if sents else paragraph


def _parse_sse_bytes(raw: bytes) -> tuple[str, dict[str, Any]]:
    """Split one ``event:/data:`` SSE frame back into ``(name, payload)``.

    The proactive service emits one frame per yield; this lets the legacy
    wrapper forward only the events the existing native-editor frontend
    expects. Falls back to ``("message", {})`` for malformed frames.
    """
    try:
        text = raw.decode("utf-8", errors="replace")
        name = "message"
        data_payload: dict[str, Any] = {}
        for line in text.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip() or "message"
            elif line.startswith("data:"):
                data_str = line[len("data:"):].strip()
                if data_str:
                    try:
                        parsed = json.loads(data_str)
                        if isinstance(parsed, dict):
                            data_payload = parsed
                    except json.JSONDecodeError:
                        data_payload = {"raw": data_str}
        return name, data_payload
    except Exception:
        return "message", {}


def _iso_from_mtime(mtime: float) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(mtime, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
