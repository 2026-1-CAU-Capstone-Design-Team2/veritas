# workflows/

## 최신 API 연동 결과

- API의 research job은 `AutoSurveyWorkflow.run_all()` 완료 후 `summary/index.json`과 `final.md`를 읽어 UI 표시용 메타데이터를 구성합니다.
- `summary/index.json`의 records는 조사 결과 화면에서 문서 제목/링크/전체 문서 수로 표시됩니다.
- `final.md`는 문서 화면의 요약본 영역에서 markdown으로 렌더링됩니다.
- workflow 자체는 기존처럼 plan, collect, summarize, final 단계를 담당하고, RAG embedding index 생성은 workflow 완료 후 API runtime에서 수행합니다.
- API runtime은 workflow 시작 전에 같은 term-grounding logic으로 얻은 첫 `grounded_terms` 문자열을 조사별 workspace 폴더명으로 사용합니다. workflow는 최종 workspace 폴더 안에서 다시 grounding, plan, collect, summarize, final 단계를 수행합니다.

> RAG workflow update: `AutoSurveyWorkflow` still orchestrates the deterministic research pipeline. RAG chat is now backed by the registered `rag` tool after indexing, so workflow phases should continue to call explicit tools for plan/collect/summarize/final while chat sessions may expose `rag` to the LLM through tool-calling.

**역할**: 여러 도구를 조합한 파이프라인 정의 및 오케스트레이션

---

## 📋 개요

`workflows/` 디렉토리는 여러 도구를 조합하여 복잡한 작업 흐름을 정의합니다. 현재 `AutoSurveyWorkflow`가 웹 리서치 파이프라인 전체를 오케스트레이션합니다.

---

## 📁 디렉토리 구조

```
workflows/
├── __init__.py
└── autosurvey_workflow.py    # 자동 서베이 워크플로우
```

---

## 🏗️ AutoSurveyWorkflow

웹 리서치의 전체 파이프라인을 관리하는 핵심 워크플로우입니다.

### 클래스 정의

```python
class AutoSurveyWorkflow:
    def __init__(
        self,
        registry,              # ToolRegistry 인스턴스
        run_store_service,     # RunStoreService 인스턴스
        *,
        max_docs: int = 15,    # 최대 수집 문서 수
    ):
```

### 파이프라인 단계

```
┌──────────┐    ┌───────────┐    ┌─────────────┐    ┌─────────┐
│  Plan    │───▶│  Collect  │───▶│  Summarize  │───▶│  Final  │
│          │    │           │    │             │    │         │
│query_plan│    │web_search │    │document_    │    │final_   │
│          │    │fetch_page │    │summarize    │    │report   │
└──────────┘    └───────────┘    └─────────────┘    └─────────┘
```

### 주요 메서드

#### `run_all()` - 전체 파이프라인 실행

```python
def run_all(
    self,
    user_request: str,
    *,
    force_plan: bool = False,        # 기존 계획 무시하고 새로 생성
    overwrite_summaries: bool = False,  # 기존 요약 덮어쓰기
) -> dict[str, Any]:
    """
    전체 파이프라인 실행:
    1. Plan: 검색 쿼리 계획 생성
    2. Collect: 웹 검색 및 문서 수집 (clean_md 저장)
    3. (루프 내) 배치 요약 → gap 분석 → replan 반복
    4. (루프 종료 후) per-doc 요약 일괄 수행
    5. Final: 최종 보고서 생성
    
    Returns:
        {
            "plan": {...},
            "collect_result": {"record_count": int},
            "summarize_result": {...},
            "final_result": {"final_path": str}
        }
    """
```

#### 개별 단계 메서드

```python
def run_plan(self, user_request: str, *, force_plan: bool = False) -> dict:
    """1단계: 검색 쿼리 계획 생성"""

def run_collect(self, plan: dict) -> dict:
    """2단계: 웹 검색 및 문서 수집"""

def run_summarize(
    self, *, overwrite: bool = False,
    doc_ids: list[str] | None = None, phase: str = "all",
) -> dict:
    """문서 요약. clean_md를 읽는다.
    phase="batch"   → 배치 요약만 (수집 루프 안에서 gap 분석/replan용)
    phase="per_doc" → per-doc 요약만 (조사 종료 시 1회, summary/doc_*.md)
    phase="all"     → 둘 다 (standalone --phase summarize)

    per-doc 루프가 도는 phase("per_doc"/"all")에서는 document_summarize
    tool에 progress_callback(_on_summarize_progress)을 넘겨, 문서별
    document_summarize / doc_summarized / doc_failed 진행 이벤트를
    실시간으로 흘려보낸다 — 진행률 막대 전진 + doc_*.md 카드 즉시 활성화."""

def run_final(self, *, user_request: str | None = None) -> dict:
    """4단계: 최종 보고서 생성"""
```

---

## 🔧 사용 예시

### CLI를 통한 실행

```bash
# 전체 파이프라인
python main.py "AI 윤리에 대한 최신 연구" --output-dir ./output

# 단계별 실행
python main.py --phase plan "리서치 주제" --output-dir ./output
python main.py --phase collect --output-dir ./output
python main.py --phase summarize --output-dir ./output
python main.py --phase final --output-dir ./output
```

### 코드에서 직접 사용

```python
from llm.llama_server_llm import LLMClient
from tools.loader import build_registry
from workflows import AutoSurveyWorkflow

# 초기화
llm = LLMClient(host="127.0.0.1", port=8080)
registry, run_store_service = build_registry(
    llm=llm,
    run_root="./output",
)

# 워크플로우 생성
workflow = AutoSurveyWorkflow(
    registry=registry,
    run_store_service=run_store_service,
    max_docs=15,
)

# 전체 실행
result = workflow.run_all(
    user_request="AI 윤리에 대한 최신 연구 동향",
    force_plan=False,
    overwrite_summaries=False,
)

print(f"최종 보고서: {result['final_result']['final_path']}")
```

### 개별 단계 실행

```python
# 1. 계획 수립
plan = workflow.run_plan("AI 윤리 연구", force_plan=True)
print(f"검색 쿼리: {plan['search_queries']}")

# 2. 문서 수집
collect_result = workflow.run_collect(plan)
print(f"수집된 문서: {collect_result['record_count']}개")

# 3. 요약 생성
summarize_result = workflow.run_summarize(overwrite=False)

# 4. 최종 보고서
final_result = workflow.run_final()
```

---

## 📊 내부 동작 상세

### Plan 단계
1. 사용자 요청을 `RunStoreService`에 저장
2. `query_plan` 도구를 호출하여 검색 쿼리 생성
3. 계획을 `plan.json`에 저장

### Collect 단계
1. 각 검색 쿼리에 대해 `web_search` 실행
2. 검색 결과의 각 URL에 대해:
   - 이미 수집된 URL인지 확인
   - `fetch_webpage`로 페이지 수집
   - 중복 문서 검사 (Jaccard 유사도 0.82 이상)
   - 고유 문서만 **보존 문서**로 저장 — `doc_id`는 `_fetch_one`에서 미리
     할당하지 않고 `write_fetched_record`가 write 시점에 보존 문서 수로
     연속 할당(`000`, `001`, ...)한다. fetch 실패(`fetch_error_*`)와
     중복(`dup_*`)은 별도 id 네임스페이스로 분리되어 보존 문서 번호를
     소비하거나 가로채지 않는다.
3. `max_docs`에 도달하면 중단 (`_kept_record_count()` = 보존 문서 수 기준)

### Summarize 단계 — clean_md를 읽는 두 개의 독립 소비자 (체인 아님)
- **배치 요약** (수집 루프 안, `phase="batch"`): 사이클의 새 문서 `clean_md`를
  `batch_size`개씩 묶어 배치 노트(`summary/batch_*.md`)를 만들고, 그 안의 Gap
  섹션이 다음 replan 신호가 됩니다. per-doc 요약이 아니라 clean_md를 직접 읽어
  요약-of-요약으로 인한 손실을 피합니다.
- **per-doc 요약** (수집 루프 종료 후 1회, `phase="per_doc"`): 모든 `clean_md`를
  문서별로 요약해 `summary/doc_*.md`를 만듭니다. replan에 관여하지 않는 UX
  디스크립터(출처 카드/인용/검증용)이므로 루프 임계 경로에서 빼 종료 단계에
  일괄 수행합니다. 다만 이 일괄 단계 안에서도 `document_summarize` tool에
  넘긴 `progress_callback`(`_on_summarize_progress`)을 통해 문서 하나가
  시작/완료/실패할 때마다 진행 이벤트를 실시간 emit합니다 — 그래서 UI
  진행률 막대가 per-doc 구간에서 멈췄다 튀지 않고 문서마다 전진하고,
  `doc_*.md` 출처 카드도 요약이 끝나는 즉시 하나씩 활성화됩니다.

### Final 단계
1. 모든 배치 요약 로드
2. `final_report` 도구로 최종 보고서 생성
3. `final.md`로 저장

### RAG 인덱싱 (workflow 종료 후, API runtime이 수행)
RAG는 `summary/`가 아니라 **`clean_md/`** 를 인덱싱합니다. 요약은 lossy하므로
답변 근거는 정제된 원문(`clean_md/<doc_id>.md`)에서 가져옵니다.

---

## 🛠️ 새 워크플로우 추가 가이드

### Step 1: 워크플로우 클래스 작성

```python
# workflows/my_workflow.py
from typing import Any
from core.models import DocRecord

class MyWorkflow:
    """커스텀 워크플로우 설명."""
    
    def __init__(self, registry, run_store_service, **config):
        self.registry = registry
        self.run_store_service = run_store_service
        self.config = config
    
    def run_all(self, **kwargs) -> dict[str, Any]:
        step1_result = self._step1(kwargs)
        step2_result = self._step2(step1_result)
        return {
            "step1": step1_result,
            "step2": step2_result,
        }
    
    def _step1(self, inputs: dict) -> dict:
        # 도구 호출
        result = self.registry.get("some_tool").run(**inputs)
        return result.data
    
    def _step2(self, prev_result: dict) -> dict:
        # 다음 단계 로직
        pass
```

### Step 2: `__init__.py`에 등록

```python
# workflows/__init__.py
from .autosurvey_workflow import AutoSurveyWorkflow
from .my_workflow import MyWorkflow

__all__ = ["AutoSurveyWorkflow", "MyWorkflow"]
```

### Step 3: `main.py`에 통합 (선택)

```python
# main.py
from workflows import MyWorkflow

if args.phase == "my_workflow":
    workflow = MyWorkflow(registry, run_store_service)
    result = workflow.run_all(**kwargs)
```

---

## 📐 설계 원칙

1. **명확한 단계 분리**: 각 단계는 독립적으로 실행 가능
2. **상태 위임**: 상태 관리는 `RunStoreService`에 위임
3. **도구 조합**: 직접 로직 구현 대신 도구를 조합하여 사용
4. **재시작 가능**: 중단 후 이어서 실행 가능한 구조

---

## 🔗 의존성 관계

```
main.py
    │
    └──▶ workflows/AutoSurveyWorkflow
              │
              ├──▶ tools/ToolRegistry
              │         │
              │         ├──▶ query_plan_tool
              │         ├──▶ web_search_tool
              │         ├──▶ fetch_webpage_tool
              │         ├──▶ document_summarize_tool
              │         └──▶ final_report_tool
              │
              └──▶ services/RunStoreService
                        │
                        └──▶ core/models.DocRecord
```

**의존성 방향**:
- `workflows/` → `tools/` (Registry를 통한 도구 호출)
- `workflows/` → `services/` (상태 관리 위임)
- `workflows/` → `core/` (데이터 모델 사용)
