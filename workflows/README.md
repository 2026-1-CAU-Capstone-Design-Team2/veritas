# workflows/

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
    2. Collect: 웹 검색 및 문서 수집
    3. Summarize: 개별 요약 및 배치 요약
    4. Final: 최종 보고서 생성
    
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

def run_summarize(self, *, overwrite: bool = False) -> dict:
    """3단계: 문서 요약 생성"""

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
   - 고유 문서만 저장
3. `max_docs`에 도달하면 중단

### Summarize 단계
1. 비중복 문서 목록 로드
2. 각 문서에 대해 `document_summarize` 실행
3. 5개 문서마다 배치 요약 생성
4. 요약 파일을 `summary/` 디렉토리에 저장

### Final 단계
1. 모든 배치 요약 로드
2. `final_report` 도구로 최종 보고서 생성
3. `final.md`로 저장

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
