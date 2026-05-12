# core/

> RAG prompt update: RAG-specific prompts now belong to this directory in `core/prompts.py`: `RAG_SYSTEM_PROMPT`, `QUERY_REWRITE_SYSTEM_PROMPT`, `QUERY_REWRITE_PROMPT`, `RAG_USER_PROMPT_TEMPLATE`, `RAG_EMPTY_CONTEXT_PROMPT_TEMPLATE`, `TOOL_CHAT_SYSTEM_PROMPT`, and `TOOL_CHAT_USER_PROMPT_TEMPLATE`. `core/` still remains dependency-free and only owns shared data models and prompt templates.

**역할**: 프로젝트 전역에서 사용되는 공유 데이터 모델 및 프롬프트 템플릿 정의

---

## 📋 개요

`core/` 디렉토리는 프로젝트의 기반이 되는 데이터 구조와 LLM 프롬프트 템플릿을 정의합니다. 다른 모든 모듈이 이 모듈을 참조할 수 있으며, 이 모듈은 다른 프로젝트 모듈에 의존하지 않습니다.

---

## 📁 디렉토리 구조

```
core/
├── models.py     # 공유 데이터 모델 (dataclass)
└── prompts.py    # LLM 프롬프트 템플릿
```

---

## 📦 모듈 상세

### 1. `models.py` - 데이터 모델

리서치 파이프라인에서 사용되는 핵심 데이터 구조입니다.

#### `DocRecord` 클래스

```python
@dataclass
class DocRecord:
    doc_id: str              # 문서 고유 ID (예: "001", "002")
    title: str               # 문서 제목
    url: str                 # 원본 URL
    final_url: str           # 리다이렉트 후 최종 URL
    domain: str              # 도메인 (예: "example.com")
    search_query: str        # 이 문서를 찾은 검색 쿼리
    text_path: str           # 추출된 텍스트 파일 경로
    html_path: str           # 원본 HTML 파일 경로
    summary_path: str        # 요약 파일 경로
    duplicate_of: Optional[str] = None   # 중복 시 원본 문서 ID
    duplicate_score: float = 0.0         # 중복 유사도 점수
```

**사용 예**:
```python
from core.models import DocRecord

record = DocRecord(
    doc_id="001",
    title="AI Ethics Overview",
    url="https://example.com/ai-ethics",
    final_url="https://example.com/ai-ethics",
    domain="example.com",
    search_query="AI ethics research",
    text_path="./output/corpus/raw_text/001.txt",
    html_path="./output/corpus/raw_html/001.html",
    summary_path="./output/summary/doc_001.md",
)
```

---

### 2. `prompts.py` - LLM 프롬프트 템플릿

각 도구에서 사용되는 LLM 프롬프트를 중앙에서 관리합니다.

#### 프롬프트 목록

| 상수명 | 사용처 | 역할 |
|--------|--------|------|
| `SYSTEM_PROMPT` | 전역 | 기본 시스템 프롬프트 |
| `PLANNER_PROMPT` | `query_plan_tool` | 검색 쿼리 계획 생성 |
| `DOC_SUMMARY_PROMPT` | `document_summarize_tool` | 개별 문서 요약 |
| `BATCH_SUMMARY_PROMPT` | `document_summarize_tool` | 배치 요약 |
| `FINAL_PROMPT` | `final_report_tool` | 최종 보고서 생성 |

#### 프롬프트 상세

**SYSTEM_PROMPT**:
```python
SYSTEM_PROMPT = """You are a careful research assistant running on a local model.
Return concise, factual, structured answers.
Do not invent sources or URLs.
When asked for JSON, return valid JSON only.
"""
```

**PLANNER_PROMPT** (검색 계획):
```python
PLANNER_PROMPT = """Convert the user's research request into a JSON spec.
Return JSON only with this schema:
{
  "topic": string,
  "goal": string,
  "search_queries": [string, ...],
  "must_cover": [string, ...],
  "keywords": [string, ...]
}
Generate 5-8 search queries. Keep them diverse and web-search friendly.
"""
```

**DOC_SUMMARY_PROMPT** (문서 요약):
```python
DOC_SUMMARY_PROMPT = """Summarize the document for later synthesis.
Return JSON only with this schema:
{
  "title": string,
  "source_type": string,
  "summary": string,
  "key_points": [string, ...],
  "reliability_notes": [string, ...],
  "keywords": [string, ...]
}
Keep it concise. Prefer 1-2 sentence summary and 3-5 key points.
"""
```

**BATCH_SUMMARY_PROMPT** (배치 요약):
```python
BATCH_SUMMARY_PROMPT = """You are given multiple document summaries.
Create a markdown batch note with these sections:
# Batch Summary
## Repeated Findings
## New Findings
## Reliability Notes
## Gaps / Next Search Directions
Be concise and remove redundant statements.
"""
```

**FINAL_PROMPT** (최종 보고서):
```python
FINAL_PROMPT = """Create the final markdown report.
Required sections:
# Final Research Brief
## User Request
## Executive Summary
## Consolidated Findings
## Repeated / Well-Supported Points
## Conflicts or Uncertainties
## Source Notes
## Remaining Gaps
Rules:
- Deduplicate overlapping content.
- Mention support frequency when relevant.
- Be concrete and concise.
"""
```

---

## 🔧 사용 예시

### 모델 사용

```python
from core.models import DocRecord
from dataclasses import asdict

# 레코드 생성
record = DocRecord(
    doc_id="001",
    title="Example",
    url="https://example.com",
    # ... 나머지 필드
)

# JSON 직렬화
record_dict = asdict(record)
```

### 프롬프트 사용

```python
from core.prompts import PLANNER_PROMPT, DOC_SUMMARY_PROMPT

# LLM 호출 시 사용
plan = llm.ask_json(PLANNER_PROMPT, user_request, reasoning=True)
summary = llm.ask_json(DOC_SUMMARY_PROMPT, document_text, reasoning=False)
```

---

## 🛠️ 확장 가이드

### 새 데이터 모델 추가

```python
# core/models.py
from dataclasses import dataclass
from typing import Optional

@dataclass
class MyNewModel:
    id: str
    name: str
    metadata: Optional[dict] = None
```

### 새 프롬프트 추가

```python
# core/prompts.py
MY_NEW_PROMPT = """Your new prompt template here.
Return JSON with this schema:
{
  "field1": string,
  "field2": [string, ...]
}
"""
```

---

## 📐 설계 원칙

1. **의존성 없음**: `core/`는 다른 프로젝트 모듈에 의존하지 않음
2. **불변 데이터**: dataclass로 정의하여 데이터 무결성 보장
3. **중앙 집중 관리**: 모든 프롬프트를 한 곳에서 관리
4. **타입 안전성**: 타입 힌트 필수 적용

---

## 🔗 의존성 관계

```
core/
├── models.py ──▶ (의존성 없음, 표준 라이브러리만 사용)
└── prompts.py ──▶ (의존성 없음, 순수 문자열 상수)

사용처:
├── services/run_store_tool_funcs/ ──▶ core/models.DocRecord
├── tools/query_plan_tool/ ──────────▶ core/prompts.PLANNER_PROMPT
├── tools/document_summarize_tool/ ──▶ core/prompts.DOC_SUMMARY_PROMPT, BATCH_SUMMARY_PROMPT
├── tools/final_report_tool/ ────────▶ core/prompts.FINAL_PROMPT
└── workflows/autosurvey_workflow/ ──▶ core/models.DocRecord
```

**핵심 규칙**:
- 모든 모듈이 `core/`를 참조할 수 있음
- `core/`는 절대로 다른 프로젝트 모듈을 import하지 않음
- 순환 의존성 방지의 기반 모듈
