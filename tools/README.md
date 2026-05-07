# tools/

역할: AI Agent의 도구 정의, 등록, 실행, 그리고 LLM Tool-Calling 연결을 담당하는 모듈

---

## 개요

`tools/`는 두 가지 실행 경로를 동시에 지원합니다.

1. 비자율형(Deterministic) 도구 실행
2. 자율형(LLM Tool-Calling) 도구 실행

핵심은 `ToolRegistry`를 단일 진입점으로 두고, 워크플로우 또는 LLM이 동일한 도구 구현을 호출하게 만드는 것입니다.

---

## 디렉토리 구조

```text
tools/
├── __init__.py
├── tool.py
├── registry.py
├── loader.py
├── llm_tooling.py
│
├── web_search_tool/
├── fetch_webpage_tool/
├── current_time_tool/
├── term_grounding_tool/
├── query_plan_tool/
├── document_summarize_tool/
└── final_report_tool/
```

---

## 핵심 컴포넌트

### 1) `tool.py`

- `BaseTool`: 모든 도구의 공통 추상 인터페이스
- `ToolResult`: 도구 실행 결과 표준 형식

설계 원칙:

1. 모든 도구는 `name`, `schema`, `run()`을 제공해야 함
2. 예외를 그대로 밖으로 전파하기보다 `ToolResult.error`를 통해 실패를 표현
3. LLM 노출용 함수 스키마는 `tool_schema.json`에서 관리

### 2) `registry.py`

- 도구의 등록/조회/실행을 중앙화
- 호출자는 구현체를 모르고 이름만으로 실행 가능

### 3) `loader.py`

- 앱 시작 시 도구를 생성하고 `ToolRegistry`에 등록
- LLM, RunStoreService 등 의존성 주입

### 4) `llm_tooling.py`

- LLM tool-calling에 노출할 스키마 목록과 공통 `tool_runner`를 생성
- 단계별(`grounding`, `planning` 등) 메시지와 허용 툴 목록만 넘기면 재사용 가능
- 신규 자율형 툴 추가 시 중복 분기 코드를 줄여 유지보수 비용을 낮춤

---

## 자율형 vs 비자율형 (중요)

| 구분 | 실행 결정 주체 | 호출 트리거 | 예시 | 특징 |
|---|---|---|---|---|
| 비자율형 도구 | 코드(워크플로우) | `autosurvey_workflow.py`의 명시적 `registry.get(...).run(...)` | `web_search`, `fetch_webpage`, `document_summarize`, `final_report`, `term_grounding`, `query_plan` | 재현성 높음, 제어 쉬움 |
| 자율형 도구 | LLM | `ask()/ask_json()`에 전달된 `tools` 스키마 + 모델의 `tool_calls` 판단 | `current_time` | 상황 적응력 높음, 대신 호출 빈도/비용 관리 필요 |

### 혼합형 구조

현재 파이프라인은 혼합형입니다.

1. 상위 단계는 비자율형: 워크플로우가 `term_grounding`, `query_plan`을 강제 호출
2. 하위 단계는 자율형: `term_grounding`/`query_plan` 내부 LLM이 필요 시 `current_time`를 호출

즉, "도구 자체"와 "도구를 누가 호출할지"는 분리해서 생각해야 합니다.

---

## 완전 자율형 `current_time` 운영 기준

완전 자율형의 의미:

1. `current_time` 스키마를 LLM 호출에 항상(허용된 단계에서) 노출
2. 호출 여부는 키워드 정규식이 아니라 모델이 판단

권장 정책:

1. 단계 제한(Stage allowlist)은 유지
2. 단계 내부의 키워드 게이트는 제거
3. 프롬프트에는 "필요할 때만 호출" 규칙을 명시

예시(권장):

```python
def _build_llm_tooling(self, user_request: str):
    if self._tool_registry is None:
        return None, None

    if not self._tool_registry.has("current_time"):
        return None, None

    schema = self._tool_registry.get("current_time").schema

    def _tool_runner(name: str, arguments: dict[str, Any]) -> Any:
        if name != "current_time":
            return {"error": f"Unsupported tool: {name}"}

        result = self._tool_registry.call(name, **(arguments or {}))
        if not result.success:
            return {"error": result.error or "current_time tool failed"}

        return result.data or {"content": result.content or ""}

    return [schema], _tool_runner
```

주의:

1. 정규식 기반 `_should_offer_time_tool()`를 남기면 "부분 자율형"입니다.
2. 완전 자율형으로 갈수록 프롬프트 품질과 호출 관찰(log/metrics)이 중요합니다.

---

## 새 도구 추가 가이드

### 공통 절차

1. `tools/<new_tool>/` 디렉토리 생성
2. `tool_schema.json` 작성 (OpenAI function calling 형식)
3. `<new_tool>.py`에서 `BaseTool` 상속 구현
4. `__init__.py` export 추가
5. `loader.py`에 import + register 추가

---

### 자율형 도구 추가 절차

자율형은 "등록"만으로 끝나지 않습니다. "LLM에게 언제 노출할지" 정책을 함께 설계해야 합니다.

1. 노출 단계 정의
2. 해당 단계 클래스의 `LLM_EXPOSED_TOOL_NAMES`에 툴 이름 추가
3. 해당 단계의 `_build_llm_tooling()`에서 `tools.llm_tooling.build_llm_tooling(...)` 호출
4. 필요하면 `expose_predicate`로 툴별 조건부 노출 정책 추가
5. `ask_json(..., tools=..., tool_runner=...)` 전달 확인
6. 프롬프트에 호출 규칙 명시
7. 호출 예산(`max_tool_rounds`)과 실패 fallback 설계

체크포인트:

1. 스키마 인자 최소화: 필수 파라미터만 required
2. 입력 검증: 잘못된 인자에 대한 방어 코드
3. 출력 일관성: JSON 직렬화 가능한 구조 유지
4. 부작용 관리: 쓰기/삭제/외부 전송 도구는 권한 경계 명확화
5. 지연/비용 관리: 호출 빈도, 타임아웃, 재시도 제한

---

### 비자율형 도구 추가 절차

비자율형은 워크플로우가 명시적으로 실행합니다.

1. `autosurvey_workflow.py`에 호출 단계 추가
2. 이전/다음 단계 입력-출력 계약 정의
3. 재실행 시 멱등성(idempotency) 보장
4. 저장소(run_store) 파일 포맷과 호환성 검증

체크포인트:

1. 단계 실패 시 중단/계속 정책
2. 중복 실행 시 덮어쓰기 여부
3. 부분 성공(일부 문서 실패) 처리 전략

---

## 도구 설계 시 필수 고려사항

1. 경계 명확성: 이 도구가 "판단"을 하는지, "실행"만 하는지 구분
2. 관찰 가능성: 최소한 호출 로그(`tool name`, `args`, `success/failure`) 남기기
3. 템플릿 호환성: 대화 메시지 규약 위반 금지
4. 실패 격리: 한 도구 실패가 전체 루프를 무조건 중단하지 않도록 fallback 설계
5. 테스트 시나리오: 호출됨/호출 안됨/잘못된 인자/타임아웃 모두 점검

---

## 빠른 점검 체크리스트

새 도구 추가 후 아래를 확인하세요.

1. `loader.py`에서 register 되었는가
2. `tool_schema.json`의 `function.name`과 `Tool.name`이 같은가
3. 자율형이면 `tools` + `tool_runner`가 실제 LLM 호출까지 전달되는가
4. 비자율형이면 워크플로우 단계에서 호출 경로가 연결되었는가
5. 실패 시 `ToolResult(success=False, error=...)`가 정상 반환되는가

---

## 참고: 현재 저장소의 실행 경로

1. 워크플로우 강제 호출(비자율형): `autosurvey_workflow.py`
2. 자율형 tool-calling 연결: `term_grounding_tool.py`, `query_plan_tool.py`
3. 모델 호출/툴 실행 루프: `llm/llama_server_llm.py`

이 구조를 유지하면, 앞으로 새 도구를 추가할 때도
"워크플로우 강제형"과 "LLM 자율형"을 명확히 분리해서 확장할 수 있습니다.
