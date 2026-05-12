from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..api_models import (
    NavigateRequest,
    PredictionApplyRequest,
    PredictionHideRequest,
    PredictionShowRequest,
    ToastRequest,
    WorkspaceSyncRequest,
)
from ..services import frontend_service

router = APIRouter()


@router.get("/api/v1/fe/bootstrap")
async def ui_bootstrap(userId: str | None = Query(default=None)) -> dict[str, Any]:
    _ = userId
    return frontend_service.get_bootstrap()


@router.post("/api/v1/fe/actions/navigate")
async def ui_navigate(payload: NavigateRequest) -> dict[str, Any]:
    return frontend_service.navigate(payload.route)


@router.post("/api/v1/fe/actions/workspace-sync")
async def ui_workspace_sync(payload: WorkspaceSyncRequest) -> dict[str, Any]:
    return frontend_service.sync_workspace(payload.workspaceId, payload.workspaceName)


@router.post("/api/v1/fe/actions/toast", status_code=202)
async def ui_toast(payload: ToastRequest) -> dict[str, bool]:
    return frontend_service.queue_toast(payload.level, payload.message)


@router.post("/api/v1/fe/actions/prediction/show")
async def preview_popup_show(payload: PredictionShowRequest) -> dict[str, Any]:
    return frontend_service.show_prediction_popup(
        payload.predictionId,
        payload.text,
        payload.confidence,
        payload.anchor,
    )


@router.post("/api/v1/fe/actions/prediction/hide")
async def preview_popup_hide(payload: PredictionHideRequest) -> dict[str, Any]:
    return frontend_service.hide_prediction_popup(payload.predictionId, payload.reason)


@router.post("/api/v1/fe/actions/prediction/apply")
async def preview_popup_apply(payload: PredictionApplyRequest) -> dict[str, Any]:
    _ = payload.insertMode
    return frontend_service.apply_prediction_popup(payload.predictionId)


@router.get("/api/v1/fe/state/snapshot")
async def ui_state_snapshot(route: str | None = Query(default=None)) -> dict[str, Any]:
    return frontend_service.get_ui_snapshot(route)
