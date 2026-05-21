"""Live LLM model switching — the single orchestrator the UI talks to.

Responsibility split (kept deliberately thin to avoid a tangled call graph):

* :class:`~llm.llama_supervisor.LlamaServer` owns the *process* (restart).
* :class:`AgentRuntime.switch_llm_model` owns the *mechanism* (download →
  restart → re-detect → persist).
* This module owns *transport*: it runs the switch and streams progress into a
  :class:`ProgressBuffer` the frontend polls — exactly the research / verify
  pattern — so the route stays a one-liner and the UI calls a single endpoint.

The switch runs synchronously on FastAPI's thread pool (the route is a plain
``def``), so a multi-GB download does not block the event loop; the frontend
issues it on a worker thread and polls :func:`get_switch_progress`.
"""

from __future__ import annotations

from typing import Any

from llm.model_catalog import bytes_label, get_model

from ..repositories import state_repository as repo
from .agent_runtime import get_runtime
from .progress_buffer import ProgressBuffer


_progress = ProgressBuffer(maxlen=200)


def get_switch_progress(*, since: int, limit: int) -> dict[str, Any]:
    return _progress.get_since(since=since, limit=limit)


def switch_model(model_id: str) -> dict[str, Any]:
    """Switch the chat LLM to ``model_id`` (download if needed) and report progress."""
    spec = get_model(model_id, kind="llm")
    _progress.reset(modelId=spec.id, modelName=spec.name)

    def report(stage: str, message: str, detail: dict[str, Any] | None = None) -> None:
        detail = dict(detail or {})
        done, total = detail.get("done"), detail.get("total")
        if stage == "download" and isinstance(done, int) and isinstance(total, int) and total > 0:
            detail["pct"] = int(done * 100 / total)
            message = f"모델 다운로드 중 {bytes_label(done)} / {bytes_label(total)}"
        _progress.emit(stage, message, detail=detail)

    try:
        applied = get_runtime().switch_llm_model(spec.id, report=report)
    except Exception as exc:  # surfaced to the UI via progress + HTTP error
        _progress.emit("failed", f"모델 전환 실패: {exc}", final=True)
        raise

    # The runtime persisted the selection through llm.model_settings (the store
    # the launcher reads). Re-sync the api-layer settings cache so GET /settings
    # reflects the new model immediately.
    repo.reload_settings()

    _progress.emit(
        "completed",
        f"모델 전환 완료: {applied.name}",
        detail={"modelId": applied.id, "modelName": applied.name},
        final=True,
    )
    return {"model": {"modelId": applied.id, "modelName": applied.name}, "updated": True}
