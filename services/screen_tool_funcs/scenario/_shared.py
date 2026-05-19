"""시나리오 공유 자원 — 정규식 / 사전 / 이벤트 조회 헬퍼.

모든 카테고리 시나리오가 import해서 사용하는 모듈상수와 헬퍼 함수.

내용:
- 정규식: 약어 / 헤딩 / 번호리스트 / TODO 마커 / 코드 fence / bullet / 인용 / 통계 / 인용 마커
- 사전: 한국어 접속어 / 약한 강조어 (둘 다 닫힌 어휘 집합)
- 헬퍼: 이벤트 dict에서 document_key / paragraph_fingerprint / active_editor_text 추출

도메인/분야별로 달라지는 어휘(예: 기술 jargon)는 의도적으로 하드코딩 하지 않음 —
시나리오는 사용자/환경 무관하게 동작해야 함.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any


# ============================================================
# 정규식 — 한국어 환경 호환 (\b 대신 ASCII lookaround 사용)
# ============================================================

# 약어: 대문자 3-5자, ASCII 글자/숫자가 앞뒤로 안 붙은 경우에만 매치.
# \b는 한국어 조사 뒤(WTO는)에서 실패하므로 lookbehind/lookahead 명시.
_ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{2,4}(?![A-Za-z0-9])")

# 헤딩: Markdown `#`, 아라비아 번호 `1.`/`1)`, 한국어 학술 `제N장/N편/N절/N항/N부`
_HEADING_RE = re.compile(
    r"(?:^|\n)\s*("
    r"#{1,6}\s+\S"                        # Markdown #
    r"|\d+[.)]\s+\S"                       # 1. 또는 1)
    r"|제?\s*\d+\s*(?:장|편|절|항|부)\s+\S"  # 제1장 / 1장 / 1편 / 1절 / 1항 / 1부
    r")"
)

# 번호 리스트 항목 — 다양한 enumeration 표기 카운트 용도
# 아라비아 `1.`/`1)`, 괄호 `(1)`, 동그라미 ①-⑳, 한국어 enumeration 14자(가/나/다 ... /하)
_NUMBERED_ITEM_RE = re.compile(
    r"(?:^|\n)\s*("
    r"\d+[.)]"                                # 1. 또는 1)
    r"|\(\d+\)"                               # (1)
    r"|[①-⑳]"                                # ① ~ ⑳
    r"|[가나다라마바사아자차카타파하][.)]"     # 가./나) ... 14자 enumeration
    r")\s+\S"
)

# 미해결 작업 마커
# - 영어 CS 컨벤션: TODO/FIXME/XXX/HACK/TBD/NOTE
# - 한국어 편집 컨벤션: [보강]/[확인]/[수정]/[추가]/[미정]
# - 일반 의문 표기: [?]
# 그룹 1은 영어 키워드 캡처(메타데이터용). 나머지 분기는 비-캡처.
_TODO_MARKER_RE = re.compile(
    r"\b(TODO|FIXME|XXX|HACK|TBD|NOTE)\b"
    r"|\[\s*\?\s*\]"
    r"|\[(?:보강|확인|수정|추가|미정)\]"
)

# 코드 블록 fence (` ``` `)
_CODE_FENCE_RE = re.compile(r"(?:^|\n)\s*```")

# bullet 라인 — ASCII (`-`, `*`, `•`), 한국어 (`·`, `▶`, `◆`, `◇`), 번호 (`1.`, `1)`, `(1)`, ①-⑳)
_BULLET_LINE_RE = re.compile(
    r"^\s*("
    r"[-*•·▶▷◆◇▪◦]"          # ASCII/한국어 bullet 문자
    r"|\d+[.)]"                # 1. / 1)
    r"|\(\d+\)"                # (1)
    r"|[①-⑳]"                # ① ~ ⑳
    r")\s+"
)

# 큰따옴표(ASCII/Korean curly/CJK 「」/『』) 안 20자+ 인용
_QUOTE_RE = re.compile(
    r'(?:[\"“”«][^\"“”«»]{20,}[\"“”»])'  # ASCII/curly/guillemet
    r'|(?:「[^」]{20,}」)'                  # CJK 단일 인용부
    r'|(?:『[^』]{20,}』)'                  # CJK 이중 인용부
)

# 통계/년도 패턴 — 숫자+단위(ASCII or 한국어) 또는 19xx/20xx 년도.
# 한국어 단위는 \b 대신 그냥 매치 (한국어 조사 뒤에도 통과).
# 단위는 길이 긴 것부터 (mm/cm/km이 m보다 먼저 매치되게).
_STATISTIC_RE = re.compile(
    r'(?<![A-Za-z0-9])\d+(?:\.\d+)?'
    r'(?:%|퍼센트|kg|km|mm|cm|t|배|만|억|조|개|명|회|번|점|위|등|권|가지|시간|분|초|일|월|년|세|달러|원)'
    r'|(?<!\d)(?:19|20)\d{2}년?(?!\d)'
)

# 인용 마커 — [1], [Document X], (저자, 2023)
_CITATION_MARKER_RE = re.compile(
    r'\[\d+\]|\[Document\s+[^\]]+\]|\([가-힣A-Za-z]+(?:\s*외)?,\s*\d{4}\)'
)


# ============================================================
# 사전 (text_quality 시나리오용) — 도메인 맞춰 확장 가능
# ============================================================

# 한국어 접속어 — transition_word_overuse 사전
KO_TRANSITION_WORDS: tuple[str, ...] = (
    "그러나", "하지만", "또한", "그리고", "그래서", "따라서",
    "한편", "반면", "오히려", "그렇지만", "더불어",
)

# 한국어 약한 강조어 — weak_modifier_overuse 사전
KO_WEAK_MODIFIERS: tuple[str, ...] = (
    "매우", "정말", "아주", "굉장히", "되게", "엄청", "너무", "꽤",
)


# ============================================================
# 이벤트 조회 헬퍼 — capture 이벤트 dict에서 핵심 필드 추출
# ============================================================


def _event_document_key(event: dict[str, Any]) -> str:
    """임의의 이벤트(현재 스냅샷 또는 디스크 저장 이벤트)에서 document_key 추출.
    우선순위: 최상위 document_key → intervention.metadata.document_key → process|title fallback.
    """
    direct = str(event.get("document_key") or "").strip()
    if direct:
        return direct
    intervention = event.get("intervention") or {}
    metadata = intervention.get("metadata") or {}
    nested = str(metadata.get("document_key") or "").strip()
    if nested:
        return nested
    window = event.get("window") or {}
    process_name = str(window.get("process_name") or "").lower()
    title = " ".join(str(window.get("window_title") or "").split()).lower()
    title = re.sub(r"\s+", " ", title).strip()
    return f"{process_name}|{title}"


def _event_paragraph_fingerprint(event: dict[str, Any]) -> str:
    """이벤트에서 문단 지문(fingerprint) 추출.
    우선순위: 최상위 paragraph_fingerprint → metadata.paragraph_fingerprint
    → current_paragraph_text를 정규화 + SHA1 해시.
    """
    direct = str(event.get("paragraph_fingerprint") or "").strip()
    if direct:
        return direct
    intervention = event.get("intervention") or {}
    metadata = intervention.get("metadata") or {}
    nested = str(metadata.get("paragraph_fingerprint") or "").strip()
    if nested:
        return nested
    filtered = event.get("filtered") or {}
    text = str(filtered.get("current_paragraph_text") or "")
    normalized = " ".join(text.split()).strip().lower()[:500]
    if not normalized:
        return ""
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _norm_active_text(event: dict[str, Any]) -> str:
    """이벤트의 active_editor_text를 공백 정규화하여 반환.
    캡처간 diff 시나리오(edit_diff)에서 캡처 간 길이/유사도 비교에 사용.
    """
    filtered = event.get("filtered") or {}
    return " ".join(str(filtered.get("active_editor_text") or "").split()).strip()
