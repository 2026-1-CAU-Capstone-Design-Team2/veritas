# tools/

`tools/`는 Veritas의 실행 가능한 capability를 정의하고 `ToolRegistry`를 통해 일관된 방식으로 호출하게 만드는 계층입니다.

---

## 핵심 원칙

```text
Tool = 하나의 실행 가능한 capability
Workflow = 여러 tool을 묶은 deterministic pipeline
Service = 상태/비즈니스 로직 소유자
Agent = LLM과 tool registry를 연결하는 대화 loop
```

Tool은 가능한 한 입력을 받아 작업을 수행하고 `ToolResult`를 반환해야 합니다. Tool이 chat history, agent loop, workflow orchestration을 직접 소유하면 안 됩니다.

---

## 현재 주요 tool

```text
current_time_tool/
  - 현재 날짜/시간 정보 반환
  - chat-visible

rag_tool/
  - rag_search
  - RAGService.retrieve()를 호출하는 thin wrapper
  - chat-visible

autosurvey_tool/
  - AutoSurveyWorkflow를 하나의 high-level tool처럼 호출하는 adapter
  - chat-visible
  - chat 호출 시 max_docs <= 5 hard cap

web_search_tool/
fetch_webpage_tool/
term_grounding_tool/
query_plan_tool/
document_summarize_tool/
final_report_tool/
  - AutoSurvey workflow 내부에서 deterministic하게 사용
  - chat agent에는 직접 expose하지 않음
```

---

## Chat-visible allowlist

현재 `agent/chat_agent.py`의 기본 allowlist:

```python
DEFAULT_OPTIONAL_TOOL_NAMES = (
    "current_time",
    "rag_search",
    "autosurvey",
)
```

이 allowlist는 “chat 단계에서 LLM에게 보여줄 수 있는 tool 목록”만 의미합니다. 실제 어떤 tool을 호출할지는 LLM이 system prompt와 각 `tool_schema.json`의 description을 보고 결정합니다.

---

## 금지되는 설계

Chat tool selection을 위해 아래 방식을 추가하지 마세요.

```text
- 정규표현식 기반 query routing
- 특정 단어/문구가 있으면 특정 tool을 강제 호출하는 if-else
- should_force_rag(), should_expose_autosurvey() 같은 query pattern router
- 특정 tool만을 위한 chat loop 예외 분기
```

새 tool의 사용 조건은 다음 위치에 반영합니다.

```text
1. tool_schema.json description
2. core/prompts.py의 chat system prompt
3. stage allowlist
```

---

## 구성 요소

### `tool.py`

```python
@dataclass
class ToolResult:
    success: bool
    content: str | None = None
    data: Any | None = None
    error: str | None = None

class BaseTool(ABC):
    @property
    def name(self) -> str: ...

    @property
    def schema(self) -> dict[str, Any]: ...

    def run(self, **kwargs) -> ToolResult: ...
```

### `registry.py`

Tool 등록/조회/실행을 중앙화합니다.

```python
registry.register(tool)
registry.call("tool_name", **kwargs)
```

### `llm_tooling.py`

Stage-level allowlist를 받아 LLM tool-calling용 schema와 공통 runner를 생성합니다.

```python
llm_tools, llm_tool_runner = build_llm_tooling(
    registry,
    stage_label="chat",
    allowed_tool_names=("current_time", "rag_search", "autosurvey"),
)
```

---

## 새 tool 추가 절차

1. `tools/<new_tool>/` 디렉토리 생성
2. `tool_schema.json` 작성
3. `BaseTool` 구현
4. `__init__.py` export
5. `tools/loader.py` 또는 `main.py` wiring에서 등록
6. 자율형 chat-visible tool이면 stage allowlist에 추가
7. 사용 조건은 schema description과 prompt에 작성

---

## RAG 관련 책임 분리

```text
services/rag_service.py
  - indexing
  - retrieval
  - query rewriting
  - document-grounded answer generation

tools/rag_tool/
  - rag_search wrapper only
  - retrieve() 결과를 LLM tool output으로 반환

agent/chat_agent.py
  - rag_search 결과를 받아 최종 답변 생성
```

---

## AutoSurvey 관련 책임 분리

```text
workflows/autosurvey_workflow.py
  - deterministic AutoSurvey pipeline
  - internal web_search/fetch/summarize/final orchestration

tools/autosurvey_tool/
  - workflow adapter
  - chat에서 AutoSurvey를 하나의 high-level tool로 노출
  - chat max_docs hard cap 적용
```
