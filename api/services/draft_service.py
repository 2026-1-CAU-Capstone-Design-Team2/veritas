"""Built-in form draft (초안) generation.

Turns a workspace's collected knowledge base into a ready-to-use deliverable
document that follows a user-chosen built-in form (대분류 → 소분류 → 목차) and a
tone-and-manner. This is **separate from ``final.md``**: ``final.md`` reports the
research results *back to the user*; a draft is the actual working document
(주간 보고, 회의록, 사업 제안서, ...) the user hands off.

Pipeline (``generate_builtin_draft``):

1. Normalize + validate the wizard settings (ordered outline is required).
2. Gather the knowledge base for the target workspace **straight from disk**
   (``summary/batch_*.md`` + ``final.md``) — no dependency on which workspace
   the runtime is currently attached to, and no live RAG index required.
3. Resolve the tone to a sampling profile (:mod:`api.services.draft_forms`) and
   the matching writing-strategy prose (:mod:`core.prompts.draft`).
4. Generate with ``LLMClient.ask`` using that tone's sampling parameters.
5. Allocate the next draft number, persist the structured settings JSON
   (``drafts/draft_<n>_settings.json``) and the draft body
   (``drafts/draft_<n>.md`` — openable in the editor as docId ``draft_<n>``).

``regenerate_builtin_draft`` reloads a saved settings file and re-runs steps 2–5
in place (same number), so "동일 세팅에서 재생성" reuses the saved configuration.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from core.latex_cleanup import clean_latex_in_markdown
from core.prompts import (
    DRAFT_KNOWLEDGE_BLOCK_TEMPLATE,
    DRAFT_LENGTH_GUIDE,
    DRAFT_NO_KNOWLEDGE_NOTICE,
    DRAFT_SYSTEM_PROMPT,
    DRAFT_TEMPLATE_BLOCK_TEMPLATE,
    DRAFT_TONE_GUIDE,
    DRAFT_USER_PROMPT_TEMPLATE,
    DRAFT_USER_PROMPT_TEMPLATE_TEMPLATED,
)

from db import activity_repository as activity

from ..api_common import utc_now_iso
from . import draft_forms
from .agent_runtime import get_runtime


_SETTINGS_RE = re.compile(r"^draft_(\d+)_settings\.json$")

# Serializes draft-number allocation + reservation so two concurrent requests
# can never claim the same number. The slow LLM generation runs *outside* the
# lock; only the (allocate number → write reservation) step is guarded.
_draft_lock = threading.Lock()


# ------------------------------------------------------------------- public API

def list_forms() -> dict[str, Any]:
    """The built-in form catalog + tone/length options the wizard renders from."""
    return draft_forms.forms_payload()


def generate_builtin_draft(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = _normalize_settings(workspace_id, payload)
    # Ground + store against a data-bearing workspace. If the frontend passed a
    # stale or "default" id, fall back to the runtime's attached workspace so a
    # draft is never silently built from an empty knowledge base.
    workspace_id = _resolve_workspace(workspace_id)
    created = utc_now_iso()
    with _draft_lock:
        number = _next_draft_number(workspace_id)
        record = _build_settings_record(
            workspace_id, number, settings, created_at=created, updated_at=created
        )
        # Reserve the number on disk immediately so a concurrent request cannot
        # grab the same one while this draft is still generating.
        _write_settings(workspace_id, number, record)
    result = _render_and_persist(workspace_id, number, record)
    activity.log_activity(workspace_id, "draft_created", f"초안 draft_{number} 생성")
    return result


def regenerate_builtin_draft(workspace_id: str, draft_number: int) -> dict[str, Any]:
    number = int(draft_number)
    chosen_ws, settings_path = _locate_settings(workspace_id, number)
    if settings_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"draft_{number}_settings.json 을 찾을 수 없습니다.",
        )
    try:
        record = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"설정 파일을 읽을 수 없습니다: {e}") from e
    if not isinstance(record, dict) or not record.get("outline"):
        raise HTTPException(status_code=422, detail="설정 파일 형식이 올바르지 않습니다.")

    # Re-resolve the sampling profile from the stored tone in case the profile
    # definition changed since the draft was first saved. createdAt is kept.
    profile = draft_forms.resolve_tone(record.get("tone"))
    record["toneKey"] = profile["key"]
    record["sampling"] = {
        "samplingParams": profile["samplingParams"],
        "extraSamplingParams": profile["extraSamplingParams"],
        "reasoning": bool((record.get("sampling") or {}).get("reasoning", True)),
    }
    record.setdefault("createdAt", utc_now_iso())
    record["draftNumber"] = number
    record["workspaceId"] = chosen_ws
    return _render_and_persist(chosen_ws, number, record)


def list_drafts(workspace_id: str) -> dict[str, Any]:
    """List saved built-in drafts (their settings files), newest number first."""
    drafts_dir = _drafts_dir(workspace_id)
    items: list[dict[str, Any]] = []
    if drafts_dir.exists():
        for path in drafts_dir.glob("draft_*_settings.json"):
            match = _SETTINGS_RE.match(path.name)
            if not match:
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(record, dict):
                continue
            number = int(match.group(1))
            items.append(
                {
                    "draftNumber": number,
                    "draftId": f"draft_{number}",
                    "title": record.get("title") or record.get("docType") or f"초안 {number}",
                    "docType": record.get("docType"),
                    "tone": record.get("tone"),
                    "length": record.get("length"),
                    "updatedAt": record.get("updatedAt"),
                    "settingsFileName": path.name,
                }
            )
    items.sort(key=lambda item: item["draftNumber"], reverse=True)
    return {"workspaceId": workspace_id, "items": items}


# ------------------------------------------------------------- generation core

def _render_and_persist(workspace_id: str, number: int, record: dict[str, Any]) -> dict[str, Any]:
    runtime = get_runtime()
    budget = _knowledge_budget(runtime)
    selected_doc_ids = record.get("selectedDocIds")
    knowledge = _gather_knowledge(
        workspace_id, char_budget=budget, selected_doc_ids=selected_doc_ids
    )
    print(
        f"[draft] workspace={workspace_id} draft={number} "
        f"knowledge_chars={len(knowledge)} budget={budget} "
        f"selected_docs={'all' if selected_doc_ids is None else len(selected_doc_ids)}"
    )
    user_prompt = _compose_user_prompt(record, knowledge)
    sampling = record.get("sampling") or {}

    content = runtime.llm.ask(
        DRAFT_SYSTEM_PROMPT,
        user_prompt,
        reasoning=bool(sampling.get("reasoning", True)),
        sampling_params=sampling.get("samplingParams"),
        extra_sampling_params=sampling.get("extraSamplingParams"),
        stream_label="draft",
    )
    content = clean_latex_in_markdown(content).strip()

    title = _title_from_content(content) or record.get("docType") or f"초안 {number}"
    record["title"] = title
    record["hasKnowledgeBase"] = bool(knowledge)
    record["updatedAt"] = utc_now_iso()

    md_path = _draft_md_path(workspace_id, number)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(content, encoding="utf-8")
    _write_settings(workspace_id, number, record)

    settings_path = _settings_path(workspace_id, number)
    return {
        "draftId": f"draft_{number}",
        "draftNumber": number,
        "workspaceId": workspace_id,
        "title": title,
        "content": content,
        "tone": record.get("tone"),
        "hasKnowledgeBase": bool(knowledge),
        "settingsFileName": settings_path.name,
        "settingsPath": str(settings_path),
        "draftFileName": md_path.name,
        "draftPath": str(md_path),
    }


def _compose_user_prompt(record: dict[str, Any], knowledge: str) -> str:
    tone = record.get("tone") or draft_forms.DEFAULT_TONE
    length = record.get("length") or draft_forms.DEFAULT_LENGTH
    tone_guide = DRAFT_TONE_GUIDE.get(tone, DRAFT_TONE_GUIDE[draft_forms.DEFAULT_TONE])
    length_guide = DRAFT_LENGTH_GUIDE.get(length, DRAFT_LENGTH_GUIDE[draft_forms.DEFAULT_LENGTH])

    outline = record.get("outline") or []
    outline_text = "\n".join(f"{i}. {name}" for i, name in enumerate(outline, start=1))

    audience = str(record.get("audience") or "").strip()
    key_points = str(record.get("keyPoints") or "").strip()
    audience_block = f"\n[대상 독자] {audience}" if audience else ""
    keypoints_block = f"\n[핵심 내용 / 추가 지시]\n{key_points}" if key_points else ""

    knowledge = (knowledge or "").strip()
    knowledge_block = (
        DRAFT_KNOWLEDGE_BLOCK_TEMPLATE.format(knowledge=knowledge)
        if knowledge
        else DRAFT_NO_KNOWLEDGE_NOTICE
    )

    # Uploaded-form path: follow the extracted Markdown template (headings /
    # tables) rather than the outline alone.
    form_markdown = str(record.get("formMarkdown") or "").strip()
    if form_markdown:
        return DRAFT_USER_PROMPT_TEMPLATE_TEMPLATED.format(
            doc_type=record.get("docType") or "업로드 양식 기반",
            tone_guide=tone_guide,
            length_guide=length_guide,
            audience_block=audience_block,
            keypoints_block=keypoints_block,
            outline=outline_text,
            knowledge_block=knowledge_block,
            template_block=DRAFT_TEMPLATE_BLOCK_TEMPLATE.format(template=form_markdown),
        )

    return DRAFT_USER_PROMPT_TEMPLATE.format(
        doc_type=record.get("docType") or "직접 구성",
        tone_guide=tone_guide,
        length_guide=length_guide,
        audience_block=audience_block,
        keypoints_block=keypoints_block,
        outline=outline_text,
        knowledge_block=knowledge_block,
    )


def _gather_knowledge(
    workspace_id: str,
    *,
    char_budget: int,
    selected_doc_ids: list[str] | None = None,
) -> str:
    """Read the knowledge base for ``workspace_id`` from disk.

    Two grounding modes:

    * **Consolidated** (default / ``selected_doc_ids`` is ``None`` or covers
      every document): the batch summaries — which already merge per-document
      findings and carry ``[doc_*]`` citations — followed by ``final.md`` as
      additional consolidated context. This is the richest material and is what
      "all documents kept" uses.
    * **Filtered** (``selected_doc_ids`` is a strict subset chosen in the draft
      wizard's "자료 선택" step): ground *only* on those documents' per-doc
      summaries (``summary/doc_<id>.md``), so the draft can never lean on a
      document the user unchecked. An empty selection means every document was
      unchecked → no knowledge at all.

    Everything is read by path so this never depends on which workspace the
    runtime is currently attached to.
    """
    selected = _resolve_doc_filter(workspace_id, selected_doc_ids)
    if selected is not None:
        return _gather_selected_doc_summaries(workspace_id, selected, char_budget=char_budget)

    ws_dir = _workspace_dir(workspace_id)
    parts: list[str] = []
    used = 0

    # final.md first — the single most consolidated artifact — but capped so it
    # can never crowd out the batch summaries entirely (the user wants both).
    final_text = _read_text(ws_dir / "final.md")
    if final_text:
        block = f"=== final_brief ===\n{final_text}"
        final_cap = max(1000, int(char_budget * 0.6))
        block = block[:final_cap]
        parts.append(block)
        used += len(block)

    # Batch summaries (detailed, citation-rich) fill the remaining budget.
    summary_dir = ws_dir / "summary"
    if summary_dir.exists():
        for batch_file in sorted(summary_dir.glob("batch_*.md")):
            if used >= char_budget:
                break
            text = _read_text(batch_file)
            if not text:
                continue
            used = _append_within_budget(parts, f"=== {batch_file.stem} ===\n{text}", used, char_budget)

    return "\n\n".join(parts).strip()


def _append_within_budget(parts: list[str], block: str, used: int, budget: int) -> int:
    if used >= budget:
        return used
    remaining = budget - used
    if len(block) <= remaining:
        parts.append(block)
        return used + len(block)
    # Only include a truncated head when there is meaningful room left.
    if remaining > 500:
        parts.append(block[:remaining])
    return budget


def _resolve_doc_filter(
    workspace_id: str, selected_doc_ids: list[str] | None
) -> list[str] | None:
    """Decide whether to filter the knowledge base, and to which documents.

    Returns ``None`` to mean *no filter* — the caller should use the rich
    consolidated path (batch summaries + ``final.md``). Returns a list (in
    ``index.json`` order) to mean *ground only on these docs*; an empty list is
    a legitimate "user unchecked everything" selection.

    A selection that turns out to cover every known document collapses to
    ``None`` so the common "kept all" case keeps the richest grounding. A
    workspace with no ``index.json`` also collapses to ``None`` — there is
    nothing to filter against, so falling back to the consolidated path is safer
    than silently producing an empty knowledge base.
    """
    if selected_doc_ids is None:
        return None
    records = _read_index_records(workspace_id)
    all_ids = [doc_id for doc_id, _ in records]
    if not all_ids:
        return None
    selected_set = {str(doc_id).strip() for doc_id in selected_doc_ids if str(doc_id).strip()}
    kept = [doc_id for doc_id in all_ids if doc_id in selected_set]
    if len(kept) == len(all_ids):
        return None
    return kept


def _gather_selected_doc_summaries(
    workspace_id: str, doc_ids: list[str], *, char_budget: int
) -> str:
    """Concatenate the per-doc ``summary/doc_<id>.md`` files for ``doc_ids``.

    These are the same per-document summaries the verification layer parses, so
    the draft grounds on exactly the documents the user kept checked — nothing
    from an unchecked source leaks in. Titles are pulled from ``index.json`` so
    each block is labelled like the consolidated path's ``=== ... ===`` headers.
    """
    summary_dir = _workspace_dir(workspace_id) / "summary"
    titles = dict(_read_index_records(workspace_id))
    parts: list[str] = []
    used = 0
    for doc_id in doc_ids:
        if used >= char_budget:
            break
        text = _read_text(_doc_summary_path(summary_dir, doc_id))
        if not text:
            continue
        title = titles.get(doc_id, "")
        header = f"=== doc_{doc_id} · {title} ===" if title else f"=== doc_{doc_id} ==="
        used = _append_within_budget(parts, f"{header}\n{text}", used, char_budget)
    return "\n\n".join(parts).strip()


def _doc_summary_path(summary_dir: Path, doc_id: str) -> Path:
    """Per-doc summary path, mirroring the run-store's zero-padded naming.

    Digit ids map to ``doc_<3-digit>.md`` (e.g. ``doc_007.md``); any other id is
    used verbatim — the same rule the verification artifact loader applies.
    """
    doc_id = str(doc_id).strip()
    name = f"doc_{int(doc_id):03d}.md" if doc_id.isdigit() else f"doc_{doc_id}.md"
    return summary_dir / name


def _read_index_records(workspace_id: str) -> list[tuple[str, str]]:
    """``(doc_id, title)`` for each non-duplicate ``index.json`` record, in order."""
    index_path = _workspace_dir(workspace_id) / "summary" / "index.json"
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing/corrupt index -> no filterable docs
        return []
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return []
    out: list[tuple[str, str]] = []
    for record in records:
        if not isinstance(record, dict) or record.get("duplicate_of"):
            continue
        doc_id = str(record.get("doc_id") or "").strip()
        if not doc_id:
            continue
        title = str(record.get("title") or record.get("url") or f"문서 {doc_id}").strip()
        out.append((doc_id, title))
    return out


def _knowledge_budget(runtime: Any) -> int:
    """Char budget for the knowledge block, sized to the model's context window.

    Treats 1 char ≈ 1 token (conservative for mixed Korean/English) and reserves
    ~4k tokens for the prompt scaffolding plus the generated draft, so a small
    local model's window is never overflowed.
    """
    n_ctx = getattr(getattr(runtime, "llm", None), "n_ctx", 0) or 8192
    return max(2000, min(16000, int(n_ctx) - 4096))


# --------------------------------------------------------------- settings model

def _normalize_settings(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    source = (str(payload.get("source") or "custom").strip().lower()) or "custom"
    outline = [str(item).strip() for item in (payload.get("outline") or []) if str(item).strip()]
    if not outline:
        raise HTTPException(status_code=422, detail="목차에 항목을 1개 이상 추가하세요.")

    category = _selection(payload.get("category"))
    subtype = _selection(payload.get("subtype"))
    tone = draft_forms.resolve_tone(payload.get("tone"))["label"]
    length = draft_forms.resolve_length(payload.get("length"))

    # The form template is only meaningful for the uploaded-form path; ignore
    # any stray value for the built-in path so it can't alter that prompt.
    form_markdown = str(payload.get("formMarkdown") or "").strip() if source == "file" else ""

    # ``None`` (key absent / step skipped) stays None → no doc filter. A list —
    # even empty — is an explicit selection that filters grounding.
    raw_selection = payload.get("selectedDocIds")
    selected_doc_ids = (
        None
        if raw_selection is None
        else [str(doc_id).strip() for doc_id in raw_selection if str(doc_id).strip()]
    )

    return {
        "source": source,
        "category": category,
        "subtype": subtype,
        "outline": outline,
        "tone": tone,
        "length": length,
        "audience": str(payload.get("audience") or "").strip(),
        "keyPoints": str(payload.get("keyPoints") or "").strip(),
        "docType": _doc_type(source, category, subtype),
        "formMarkdown": form_markdown,
        "selectedDocIds": selected_doc_ids,
    }


def _build_settings_record(
    workspace_id: str,
    number: int,
    settings: dict[str, Any],
    *,
    created_at: str,
    updated_at: str,
) -> dict[str, Any]:
    profile = draft_forms.resolve_tone(settings["tone"])
    try:
        model_id = str(getattr(get_runtime().llm, "model", "") or "")
    except Exception:  # noqa: BLE001 - model id is non-critical metadata
        model_id = ""
    return {
        "version": 1,
        "draftNumber": number,
        "workspaceId": workspace_id,
        "source": settings["source"],
        "docType": settings["docType"],
        "category": settings["category"],
        "subtype": settings["subtype"],
        "outline": settings["outline"],
        "tone": settings["tone"],
        "toneKey": profile["key"],
        "length": settings["length"],
        "audience": settings["audience"],
        "keyPoints": settings["keyPoints"],
        "formMarkdown": settings.get("formMarkdown", ""),
        "selectedDocIds": settings.get("selectedDocIds"),
        "sampling": {
            "samplingParams": profile["samplingParams"],
            "extraSamplingParams": profile["extraSamplingParams"],
            "reasoning": True,
        },
        "model": model_id,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "draftFileName": f"draft_{number}.md",
    }


def _selection(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    key = str(value.get("key") or "").strip()
    label = str(value.get("label") or "").strip()
    if not key and not label:
        return None
    return {"key": key, "label": label}


def _doc_type(source: str, category: dict | None, subtype: dict | None) -> str:
    cat = (category or {}).get("label", "")
    sub = (subtype or {}).get("label", "")
    combined = " > ".join(part for part in (cat, sub) if part)
    if combined:
        return combined
    return "업로드 양식 기반" if source == "file" else "직접 구성"


# ------------------------------------------------------------------- filesystem

def _output_root() -> Path:
    return Path(os.getenv("VERITAS_OUTPUT_DIR", "runs")).expanduser().resolve()


def _workspace_dir(workspace_id: str) -> Path:
    workspace_id = str(workspace_id or "default").strip() or "default"
    root = _output_root()
    return root / workspace_id if workspace_id != "default" else root / "api"


def _drafts_dir(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / "drafts"


def _has_research(workspace_id: str) -> bool:
    return (_workspace_dir(workspace_id) / "summary").exists()


def _resolve_workspace(workspace_id: str) -> str:
    """Resolve to a workspace that actually has research output.

    Prefer the requested workspace when it exists and has a ``summary/`` dir.
    Otherwise — the frontend passed the "default" placeholder or a stale id —
    fall back to the runtime's currently-attached workspace (the most-recently
    used real one). This is the safety net that keeps draft grounding from
    silently dropping to the empty-knowledge path.
    """
    requested = str(workspace_id or "").strip()
    if requested and requested != "default" and _has_research(requested):
        return requested
    try:
        runtime_ws = str(getattr(get_runtime(), "workspace_id", "") or "").strip()
    except Exception:  # noqa: BLE001 - runtime may be unavailable
        runtime_ws = ""
    if runtime_ws and runtime_ws != "default" and _has_research(runtime_ws):
        return runtime_ws
    return requested or "default"


def _locate_settings(workspace_id: str, number: int) -> tuple[str, Path | None]:
    """Find a draft's settings file in the requested or resolved workspace."""
    requested = str(workspace_id or "").strip() or "default"
    candidates = [requested]
    resolved = _resolve_workspace(workspace_id)
    if resolved != requested:
        candidates.append(resolved)
    for ws in candidates:
        path = _settings_path(ws, number)
        if path.exists():
            return ws, path
    return requested, None


def _settings_path(workspace_id: str, number: int) -> Path:
    return _drafts_dir(workspace_id) / f"draft_{int(number)}_settings.json"


def _draft_md_path(workspace_id: str, number: int) -> Path:
    return _drafts_dir(workspace_id) / f"draft_{int(number)}.md"


def _next_draft_number(workspace_id: str) -> int:
    drafts_dir = _drafts_dir(workspace_id)
    highest = 0
    if drafts_dir.exists():
        for path in drafts_dir.glob("draft_*_settings.json"):
            match = _SETTINGS_RE.match(path.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def _write_settings(workspace_id: str, number: int, record: dict[str, Any]) -> None:
    path = _settings_path(workspace_id, number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_text(path: Path) -> str:
    try:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _title_from_content(content: str) -> str:
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
        text = heading.group(1).strip() if heading else stripped
        return text[:80] if text else ""
    return ""
