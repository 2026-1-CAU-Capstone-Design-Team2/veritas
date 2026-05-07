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
            "name": "AI 안전성 브리프 워크스페이스",
            "detail": "웹 조사 9건 · 검증 완료 7건 · 경영진 보고용",
            "status": "verified",
            "lastWorkedAt": "2026-04-08T09:15:00Z",
        },
        {
            "workspaceId": "ws_003",
            "name": "규제 대응 메모 워크스페이스",
            "detail": "웹 조사 15건 · 검증 완료 11건 · 안내문/공지문 적합",
            "status": "verified",
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
        {"type": "document", "name": "시장동향_요약_0406.pdf", "createdAt": "2026-04-08T11:35:00Z"},
        {"type": "draft", "name": "규제 대응 메모 초안 v3", "createdAt": "2026-04-08T10:40:00Z"},
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
            "title": "오픈모델 리스크 노트",
            "matchRate": 71,
            "level": "중간",
            "issues": [
                "리스크 레벨 분류 기준이 본문 중간에서 바뀌어 해석 혼선이 발생합니다.",
                "외부 인용 2건이 최신 버전 문서와 문구 차이가 있어 재검증이 필요합니다.",
            ],
        },
        {
            "docId": "doc_13",
            "workspaceId": "ws_001",
            "title": "포럼 스냅샷",
            "matchRate": 39,
            "level": "낮음",
            "issues": [
                "주요 주장에 대한 신뢰 가능한 1차 출처가 확인되지 않았습니다.",
                "수치 인용이 캡처 기반이라 원문 링크를 통한 사실 검증이 필요합니다.",
                "결론 문단의 표현이 단정적이므로 조건부 표현으로 완화가 필요합니다.",
            ],
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
            "summary": (
                "APAC 지역은 AI 거버넌스에서 투명성, 감사 가능성, 책임 추적성을 우선 요구합니다.\n"
                "엔터프라이즈 환경에서는 모델 위험평가와 데이터 출처 관리 체계가 필수로 강화되고 있습니다.\n"
                "운영 단계에서는 보안 통제, 로그 보존, 인시던트 대응 절차를 문서화해야 규제 대응이 수월합니다."
            ),
            "mergedText": (
                "[문서 1] APAC 지역 AI 규제 동향\n"
                "- 주요 내용: 국가별 규제 프레임워크에서 고위험 AI 분류와 보고 의무가 확대되고 있습니다.\n"
                "- 핵심 포인트: 정책 준수 증빙을 위해 모델 개발/배포 이력의 추적성이 요구됩니다.\n\n"
                "[문서 2] 엔터프라이즈 LLM 벤치마크 2026\n"
                "- 주요 내용: 정확도뿐 아니라 안정성, 환각률, 재현성 지표가 도입되고 있습니다.\n"
                "- 핵심 포인트: 운영 단계 평가에서 데이터 거버넌스와 접근 제어가 성능만큼 중요합니다."
            ),
        },
        "ws_002": {
            "summary": "AI 안전성 관련 근거와 반론을 함께 정리한 요약본입니다.",
            "mergedText": "[스크랩 합본]\n- 모델 카드\n- 평가 리포트\n- 참고 기사",
        },
        "ws_003": {
            "summary": "규제 대응 안내문과 공지문 작성에 필요한 근거를 요약한 문서입니다.",
            "mergedText": "[스크랩 합본]\n- 규제 변경 공지\n- 내부 대응 메모\n- 고객 안내 초안",
        },
    },
    "feedback_sessions": {},
    "feedback_files": {},
    "prediction_state": {},
    "research_jobs": {},
    "document_assist_sessions": {},
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
