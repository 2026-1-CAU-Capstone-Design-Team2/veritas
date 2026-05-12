from __future__ import annotations

STATE: dict[str, object] = {
    "current_workspace_id": "ws_001",
    "workspaces": [
        {
            "workspaceId": "ws_001",
            "name": "기후 정책 검증 워크스페이스",
            "detail": "웹 조사 12건 · 검증 완료 8건",
        },
        {
            "workspaceId": "ws_002",
            "name": "AI 안전성 브리프 워크스페이스",
            "detail": "웹 조사 9건 · 검증 완료 7건 · 경영진 보고용",
        },
        {
            "workspaceId": "ws_003",
            "name": "규제 대응 메모 워크스페이스",
            "detail": "웹 조사 15건 · 검증 완료 11건 · 안내문/공지문 적합",
        },
    ],
    "ui_state": {
        "workspaceId": "ws_001",
        "workspaceName": "기후 정책 검증 워크스페이스",
    },
    "settings": {
        "model": {
            "modelId": "veritas-balanced",
            "modelName": "VERITAS Balanced",
            "temperature": 0.2,
            "maxOutputTokens": 1600,
        },
        "defaultWorkspace": {
            "workspaceId": "ws_001",
            "workspaceName": "기후 정책 검증 워크스페이스",
            "openOnLaunch": True,
        },
    },
}
