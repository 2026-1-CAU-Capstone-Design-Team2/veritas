from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class WorkspaceSwitchRequest(BaseModel):
    workspaceId: str


class DraftGenerateRequest(BaseModel):
    workspaceId: str
    prompt: str


class DraftRegenerateRequest(BaseModel):
    prompt: str


class ChatMessageRequest(BaseModel):
    workspaceId: str
    message: str


class FeedbackAnalyzeRequest(BaseModel):
    fileIds: list[str]


class TypingContextRequest(BaseModel):
    sessionId: str
    workspaceId: str
    cursor: int
    prefix: str
    suffix: str


class PredictionAckRequest(BaseModel):
    action: Literal["accept", "dismiss"]


class NavigateRequest(BaseModel):
    route: str


class WorkspaceSyncRequest(BaseModel):
    workspaceId: str
    workspaceName: str


class ToastRequest(BaseModel):
    level: Literal["info", "success", "warning", "error"]
    message: str


class PredictionShowRequest(BaseModel):
    predictionId: str
    text: str
    confidence: float
    anchor: str


class PredictionHideRequest(BaseModel):
    predictionId: str
    reason: str


class PredictionApplyRequest(BaseModel):
    predictionId: str
    insertMode: str
