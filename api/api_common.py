from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


STATE: dict[str, Any] = {
    "current_workspace_id": "default",
    "workspaces": [
        {
            "workspaceId": "default",
            "name": "default",
            "detail": "기본 워크스페이스",
            "status": "active",
        }
    ],
    "dashboard_summary": {
        "processedDocs": 0,
        "verifiedWorkspaces": 0,
        "feedbackCompletionRate": 0,
    },
    "recent_documents": [],
    "verify_results": [],
    "drafts": {},
    "chat_sessions": {},
    "documents": {},
    "feedback_sessions": {},
    "feedback_files": {},
    "feedback_results": {},
    "prediction_state": {},
    "research_jobs": {},
    "document_assist_sessions": {},
    "ui_state": {
        "route": "dashboard",
        "workspaceId": "default",
        "workspaceName": "default",
        "predictionPopup": {"visible": False},
    },
    "settings": {
        "model": {
            "modelId": "qwen35-0.8b-q8_0",
            "modelName": "Qwen3.5 0.8B 8-bit",
        },
        "embeddingModel": {
            "modelId": "granite-embedding-97m-r2-q8_0",
            "modelName": "Granite Embedding 97M Multilingual R2 8-bit",
        },
        "launcher": {
            "initialModelSelected": False,
        },
        "localAccess": {
            "folderPaths": [],
        },
        "documentTools": {
            "custom": [],
        },
        # AutoSurvey pacing (설정 > 고급 설정 > 조사 진행 방식). Persisted here so
        # it rides the /fe/bootstrap response into the frontend STATE and is
        # honored by every research run, not just the in-memory UI session.
        "research": {
            "sampleCount": 3,
            "planCount": 5,
        },
        # 병렬 디코딩 동시 요청 수 (설정 > 고급 설정 > 병렬 디코딩). Drives
        # LLMClient.max_parallel; 1 = serial (default). Rides the bootstrap
        # response into the frontend STATE so the stepper shows the live value.
        "llmParallel": 1,
    },
}
