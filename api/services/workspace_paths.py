"""Workspace directory utilities — no runtime state, no class.

All five functions here operate purely on filesystem layout: discovering
the most-recently-used real workspace, reserving a directory for a new
one, and removing skeleton directories that never accumulated real
research data. They live as module-level functions because they hold no
state — the matching peer-pattern in ``api/services/*_service.py``.

The grounding-driven workspace-name extraction (:func:`extract_workspace_name_from_request`)
sits here too: it takes a request string and an LLM client, returns the
preferred name. AgentRuntime previously had this inline in
``_grounding_workspace_from_request`` but the only state it needed was the
LLM, so passing it as an argument keeps the function pure.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from tools.loader import load_schema


logger = logging.getLogger(__name__)


# Workspaces are "real" when they show at least one piece of research
# evidence — a final report, a summary index, or any per-doc summary.
# The boot-time discovery uses this to skip skeleton-only dirs (chromadb +
# corpus only) so we never re-attach to a phantom workspace.
def _has_research_evidence(workspace_dir: Path) -> bool:
    summary_dir = workspace_dir / "summary"
    return (
        (workspace_dir / "final.md").exists()
        or (summary_dir / "index.json").exists()
        or (summary_dir.exists() and any(summary_dir.glob("doc_*.md")))
    )


def discover_initial_workspace(output_root: Path) -> Path | None:
    """Return the most-recently-modified real workspace dir, or ``None``.

    "Real" = has at least one piece of research evidence (see
    :func:`_has_research_evidence`). Used at boot to avoid materializing
    ``runs/api/`` when there is already a workspace to land on, and to
    resolve frontend requests for the ``"default"`` workspace to
    something concrete.
    """
    if not output_root.exists():
        return None
    candidates: list[Path] = []
    try:
        for path in output_root.iterdir():
            if not path.is_dir():
                continue
            name = path.name
            if name in {"api", "__pycache__"} or name.startswith("_"):
                continue
            if _has_research_evidence(path):
                candidates.append(path)
    except Exception as exc:  # noqa: BLE001 — boot-time, must not abort startup
        logger.warning("workspace_paths: discover failed: %s", exc)
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def cleanup_pending_dirs(output_root: Path) -> None:
    """Remove half-created ``_pending_*`` workspace dirs left over from a
    crashed run, scoped strictly inside ``output_root``.

    ``Path.resolve`` + ``in root.parents`` guards against ``..``-injection
    through a misconfigured env var so an absolute-path mistake can't
    delete files outside the runs tree.
    """
    try:
        root = output_root.resolve()
        for path in root.glob("_pending_*"):
            if not path.is_dir():
                continue
            resolved = path.resolve()
            if root not in resolved.parents:
                continue
            try:
                shutil.rmtree(resolved)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "workspace_paths: could not remove %s: %s", resolved, exc
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("workspace_paths: pending cleanup skipped: %s", exc)


def cleanup_empty_api_dir(output_root: Path) -> None:
    """Remove ``runs/api/`` if it has no meaningful research data.

    Called at boot (to clear a stale ``api/`` from a prior session) and
    whenever the runtime transitions off the default workspace, so the
    directory never sticks around as a phantom side-effect of
    initialization. The "meaningful" test matches
    :func:`_has_research_evidence`.
    """
    api_dir = output_root / "api"
    if not api_dir.exists() or not api_dir.is_dir():
        return
    if _has_research_evidence(api_dir):
        return
    try:
        shutil.rmtree(api_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("workspace_paths: could not remove %s: %s", api_dir, exc)


def safe_workspace_name(name: str) -> str:
    """Sanitize a free-form name to a filesystem-safe workspace id.

    Keeps Unicode word characters (including Hangul), digits, ``.`` and
    ``-``; turns everything else into ``_``. Strips terminal punctuation
    and falls back to ``"research"`` when the result would otherwise be
    empty. Length-capped at 80 chars so the workspace dir doesn't make
    Windows MAX_PATH issues worse than they already are.
    """
    text = re.sub(r"[^\w가-힣.-]+", "_", str(name or "").strip(), flags=re.UNICODE)
    text = text.strip("._-")
    return text[:80] or "research"


def reserve_workspace_dir(output_root: Path, workspace_name: str) -> Path:
    """Create a fresh workspace directory under ``output_root``.

    If the sanitized name is already taken, append ``-2``, ``-3``, … until
    a free slot is found. Using ``mkdir(exist_ok=False)`` on the final
    target guarantees we never silently merge into an existing workspace.
    """
    safe_name = safe_workspace_name(workspace_name)
    target = output_root / safe_name
    if target.exists():
        suffix = 2
        while (output_root / f"{safe_name}-{suffix}").exists():
            suffix += 1
        target = output_root / f"{safe_name}-{suffix}"
    target.mkdir(parents=True, exist_ok=False)
    return target


def extract_workspace_name_from_request(
    request: str,
    *,
    llm: Any,
) -> tuple[str, dict[str, Any] | None]:
    """Run term-grounding on the request and use its first term as the
    workspace name.

    Returns ``(name, grounding_payload)``. Falls back to ``("research", None)``
    when grounding fails or returns nothing usable so a survey can still
    proceed under a generic workspace name.

    Imports the term-grounding tool lazily to avoid pulling its (heavy)
    schema-load on every ``api/services`` module that imports this file.
    """
    try:
        from tools.term_grounding_tool import TermGroundingTool

        schema_path = (
            Path(__file__).resolve().parents[2]
            / "tools"
            / "term_grounding_tool"
            / "tool_schema.json"
        )
        result = TermGroundingTool(
            schema=load_schema(schema_path),
            llm=llm,
        ).run(user_request=request, max_terms=8)
        payload = (
            result.data
            if result.success and isinstance(result.data, dict)
            else {}
        )
        terms = payload.get("grounded_terms", [])
        if isinstance(terms, list):
            for term in terms:
                text = str(term or "").strip()
                if text:
                    return text, payload
    except Exception:  # noqa: BLE001 — grounding is best-effort
        pass
    return "research", None


__all__ = [
    "discover_initial_workspace",
    "cleanup_pending_dirs",
    "cleanup_empty_api_dir",
    "safe_workspace_name",
    "reserve_workspace_dir",
    "extract_workspace_name_from_request",
]
