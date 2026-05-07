from __future__ import annotations

STATE: dict[str, object] = {
    "current_workspace_id": "mock-ai-safety",
    "workspaces": [
        {
            "workspaceId": "mock-ai-safety",
            "name": "AI Safety Brief Workspace",
            "detail": "Dummy workspace for standalone frontend preview.",
        },
        {
            "workspaceId": "mock-regulatory",
            "name": "Regulatory Memo Workspace",
            "detail": "Mock policy review workspace with sample document state.",
        },
        {
            "workspaceId": "mock-climate",
            "name": "Climate Evidence Workspace",
            "detail": "Sample evidence review workspace for UI navigation checks.",
        },
    ],
    "ui_state": {
        "workspaceId": "mock-ai-safety",
        "workspaceName": "AI Safety Brief Workspace",
    },
    "settings": {
        "model": {
            "modelId": "veritas-balanced",
            "modelName": "VERITAS Balanced",
            "temperature": 0.2,
            "maxOutputTokens": 1600,
        },
        "defaultWorkspace": {
            "workspaceId": "mock-ai-safety",
            "workspaceName": "AI Safety Brief Workspace",
            "openOnLaunch": True,
        },
    },
}
