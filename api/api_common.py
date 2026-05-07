from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


STATE: dict[str, Any] = {
    "current_workspace_id": "ws_001",
    "workspaces": [
        {
            "workspaceId": "ws_001",
            "name": "기후 정책 검증 워크스페이스",
            "detail": "웹 조사 12건 · 검증 완료 8건",
            "status": "verified",
            "lastWorkedAt": "2026-04-08T12:30:00Z",
        },
        {
            "workspaceId": "ws_002",
            "name": "AI 안전성 브리프",
            "detail": "초안 3건 · 피드백 1건",
            "status": "verified",
            "lastWorkedAt": "2026-04-08T09:15:00Z",
        },
        {
            "workspaceId": "ws_003",
            "name": "내부 보고서 정리",
            "detail": "검증 진행 중",
            "status": "draft",
            "lastWorkedAt": "2026-04-07T16:45:00Z",
        },
    ],
    "dashboard_summary": {
        "processedDocs": 48,
        "verifiedWorkspaces": 6,
        "feedbackCompletionRate": 98,
    },
    "recent_documents": [
        {"type": "feedback", "name": "2026_Q2_리스크_브리프.docx", "createdAt": "2026-04-08T12:10:00Z"},
        {"type": "document", "name": "시장 동향 요약.pdf", "createdAt": "2026-04-08T11:35:00Z"},
        {"type": "draft", "name": "AI 안전성 백서 초안", "createdAt": "2026-04-08T10:40:00Z"},
        {"type": "feedback", "name": "기후 정책 검증 결과", "createdAt": "2026-04-07T18:20:00Z"},
    ],
    "verify_results": [
        {
            "docId": "doc_11",
            "workspaceId": "ws_001",
            "title": "AI 안전성 백서",
            "matchRate": 92,
            "level": "높음",
            "issues": ["출처 표기 형식 혼재"],
        },
        {
            "docId": "doc_12",
            "workspaceId": "ws_001",
            "title": "기후 정책 브리프",
            "matchRate": 74,
            "level": "중간",
            "issues": ["수치 근거 추가 필요", "용어 일관성 개선"],
        },
        {
            "docId": "doc_13",
            "workspaceId": "ws_002",
            "title": "내부 규정 점검표",
            "matchRate": 61,
            "level": "낮음",
            "issues": ["표본 출처 누락"],
        },
    ],
    "drafts": {},
    "chat_sessions": {
        "session_default": [
            {"role": "user", "text": "경영진 보고용으로 정리해줘"},
            {"role": "assistant", "text": "개요, 리스크, 실행 권고 순서로 정리하겠습니다."},
        ]
    },
    "documents": {
        "ws_001": {
            "summary": "핵심 정책과 검증 결과를 3문단으로 요약한 문서입니다.",
            "mergedText": "[스크랩 합본]\n- 정책 발표 자료\n- 보도자료\n- 내부 검증 노트",
        },
        "ws_002": {
            "summary": "AI 안전성 관련 근거와 반론을 함께 정리한 요약본입니다.",
            "mergedText": "[스크랩 합본]\n- 모델 카드\n- 평가 리포트\n- 참고 기사",
        },
    },
    "feedback_sessions": {},
    "feedback_files": {},
    "prediction_state": {},
    "ui_state": {
        "route": "dashboard",
        "workspaceId": "ws_001",
        "workspaceName": "기후 정책 검증 워크스페이스",
        "predictionPopup": {"visible": False},
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
