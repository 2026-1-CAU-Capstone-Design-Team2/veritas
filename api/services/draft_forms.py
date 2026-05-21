"""Built-in draft form registry + tone-and-manner sampling profiles.

This is the backend home of the built-in form catalog that the draft wizard
used to hard-code in ``frontend/ui/pages/draft_page.py`` (the comment there said
"백엔드 레지스트리로 이전 예정"). Two pieces of domain data live here:

* :data:`FORM_CATEGORIES` — the 5 대분류 × 3 소분류 form catalog, each subtype
  carrying a default section skeleton. Exposed over ``GET /api/v1/draft/forms``
  so the frontend can become a thin renderer of this single source of truth.
* :data:`TONE_PROFILES` — maps the three UI tone choices (격식체 / 중립 / 캐주얼)
  to concrete llama-server sampling parameters. The user never edits these
  numbers directly; picking a tone in the UI selects a profile. The mapping
  treats "격식 ↔ 캐주얼" as "deterministic ↔ expressive": formal writing favors
  conventional, predictable phrasing (low temperature, tight nucleus), while
  casual writing benefits from more lexical variety (higher temperature, wider
  top_k / top_p). The *writing-strategy* text for each tone is the prose
  counterpart in :data:`core.prompts.draft.DRAFT_TONE_GUIDE`.

Keeping the catalog + profiles as plain data (no LLM, no I/O) makes them trivial
to serve, validate against, and unit-test.
"""

from __future__ import annotations

from typing import Any


# ----------------------------------------------------------------- form catalog
# 대분류 → 소분류 → 기본 섹션 골격. The section list is only a *default* outline;
# the user reorders / edits it in the wizard before generation.
FORM_CATEGORIES: list[dict[str, Any]] = [
    {
        "key": "report",
        "label": "보고/분석",
        "subtypes": [
            {"key": "weekly", "label": "주간 보고", "sections": ["요약", "주요 진행 사항", "이슈 / 리스크", "다음 주 계획"]},
            {"key": "result", "label": "결과 보고", "sections": ["개요", "추진 배경", "수행 내용", "결과 및 성과", "결론 및 제언"]},
            {"key": "status", "label": "현황 분석", "sections": ["분석 개요", "현황", "문제점", "원인 분석", "개선 방향"]},
        ],
    },
    {
        "key": "proposal",
        "label": "제안/기획",
        "subtypes": [
            {"key": "business", "label": "사업 제안서", "sections": ["제안 배경", "제안 내용", "기대 효과", "추진 일정", "예산"]},
            {"key": "plan", "label": "기획안", "sections": ["기획 의도", "목표", "주요 내용", "실행 계획", "기대 효과"]},
            {"key": "marketing", "label": "마케팅 플랜", "sections": ["시장 분석", "타깃", "전략", "채널 / 실행", "성과 지표"]},
        ],
    },
    {
        "key": "record",
        "label": "기록/정리",
        "subtypes": [
            {"key": "minutes", "label": "회의록", "sections": ["회의 개요", "참석자", "안건", "논의 내용", "결정 사항", "후속 조치"]},
            {"key": "memo", "label": "업무 메모", "sections": ["목적", "핵심 내용", "참고 사항", "To-Do"]},
            {"key": "research", "label": "리서치 요약", "sections": ["조사 목적", "조사 방법", "주요 발견", "시사점"]},
        ],
    },
    {
        "key": "notice",
        "label": "안내/공지",
        "subtypes": [
            {"key": "internal", "label": "사내 공지", "sections": ["제목", "공지 배경", "주요 내용", "유의 사항", "문의처"]},
            {"key": "customer", "label": "고객 안내", "sections": ["인사말", "안내 내용", "적용 일정", "유의 사항", "문의 안내"]},
            {"key": "event", "label": "이벤트 안내", "sections": ["이벤트 개요", "참여 방법", "혜택", "기간", "유의 사항"]},
        ],
    },
    {
        "key": "academic",
        "label": "학술/조사",
        "subtypes": [
            {"key": "paper", "label": "조사 보고서", "sections": ["서론", "연구 방법", "결과", "논의", "결론", "참고문헌"]},
            {"key": "review", "label": "리뷰 / 고찰", "sections": ["개요", "배경", "주요 논점", "비교 분석", "결론"]},
            {"key": "abstract", "label": "초록", "sections": ["연구 목적", "방법", "결과", "결론"]},
        ],
    },
]


# --------------------------------------------------------------- tone profiles
# Each profile is split into the two dicts ``LLMClient.ask`` consumes:
# ``samplingParams`` (temperature / top_p / presence_penalty) and
# ``extraSamplingParams`` (top_k / min_p / repeat_penalty, sent via extra_body).
TONE_PROFILES: dict[str, dict[str, Any]] = {
    "격식체": {
        "key": "formal",
        "label": "격식체",
        "samplingParams": {"temperature": 0.4, "top_p": 0.85, "presence_penalty": 1.2},
        "extraSamplingParams": {"top_k": 20, "min_p": 0.0, "repeat_penalty": 1.05},
    },
    "중립": {
        "key": "neutral",
        "label": "중립",
        "samplingParams": {"temperature": 0.7, "top_p": 0.9, "presence_penalty": 1.3},
        "extraSamplingParams": {"top_k": 30, "min_p": 0.0, "repeat_penalty": 1.05},
    },
    "캐주얼": {
        "key": "casual",
        "label": "캐주얼",
        "samplingParams": {"temperature": 0.95, "top_p": 0.95, "presence_penalty": 1.5},
        "extraSamplingParams": {"top_k": 50, "min_p": 0.0, "repeat_penalty": 1.0},
    },
}

DEFAULT_TONE = "중립"

LENGTHS = ["짧게", "보통", "길게"]
DEFAULT_LENGTH = "보통"


# ------------------------------------------------------------------- lookups

def tone_labels() -> list[str]:
    return list(TONE_PROFILES.keys())


def resolve_tone(tone: str | None) -> dict[str, Any]:
    """Return the sampling profile for ``tone``, falling back to 중립.

    The lookup is by the UI label (격식체 / 중립 / 캐주얼). An unknown or empty
    value resolves to the neutral profile so a malformed payload still produces
    a sensible draft instead of a 4xx.
    """
    label = str(tone or "").strip()
    return TONE_PROFILES.get(label, TONE_PROFILES[DEFAULT_TONE])


def resolve_length(length: str | None) -> str:
    label = str(length or "").strip()
    return label if label in LENGTHS else DEFAULT_LENGTH


def find_category(category_key: str | None) -> dict[str, Any] | None:
    key = str(category_key or "").strip()
    return next((c for c in FORM_CATEGORIES if c["key"] == key), None)


def find_subtype(category_key: str | None, subtype_key: str | None) -> dict[str, Any] | None:
    category = find_category(category_key)
    if category is None:
        return None
    key = str(subtype_key or "").strip()
    return next((s for s in category["subtypes"] if s["key"] == key), None)


def forms_payload() -> dict[str, Any]:
    """The catalog the frontend renders the wizard from (single source of truth)."""
    return {
        "categories": FORM_CATEGORIES,
        "tones": [
            {"key": profile["key"], "label": label}
            for label, profile in TONE_PROFILES.items()
        ],
        "defaultTone": DEFAULT_TONE,
        "lengths": LENGTHS,
        "defaultLength": DEFAULT_LENGTH,
    }


__all__ = [
    "FORM_CATEGORIES",
    "TONE_PROFILES",
    "DEFAULT_TONE",
    "LENGTHS",
    "DEFAULT_LENGTH",
    "tone_labels",
    "resolve_tone",
    "resolve_length",
    "find_category",
    "find_subtype",
    "forms_payload",
]
