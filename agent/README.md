# agent/

**역할**: AI Agent 오케스트레이션 로직 - 도구 선택, 실행, 결과 처리를 조율 (확장용)

---

## 📋 개요

`agent/` 디렉토리는 LLM과 도구(Tool)들을 연결하여 복잡한 태스크를 수행하는 에이전트 로직을 담당합니다. 현재는 README만 존재하며, 향후 동적 도구 선택이 필요한 에이전트 구현 시 이 디렉토리에 추가됩니다.

> **참고**: 현재 프로젝트에서는 정적 파이프라인인 `workflows/AutoSurveyWorkflow`가 도구 조합을 처리합니다. LLM이 동적으로 도구를 선택하는 ReAct 패턴 등이 필요할 때 이 디렉토리를 활용하세요.

---

## 📁 디렉토리 구조

```
agent/
└── README.md    # 현재 파일 (에이전트 구현 가이드)
```

---

## 🏗️ Agent vs Workflow 비교

| 구분 | Agent | Workflow |
|------|-------|----------|
| **결정 주체** | LLM이 동적으로 결정 | 사전 정의된 순서 |
| **도구 선택** | 상황에 따라 LLM이 선택 | 코드에서 고정 |
| **유연성** | 높음 | 낮음 |
| **예측 가능성** | 낮음 | 높음 |
| **사용 케이스** | 탐색적 태스크, 대화형 | 정형화된 파이프라인 |
| **현재 구현** | 미구현 | `AutoSurveyWorkflow` |

---

## 🔧 에이전트 구현 가이드

### 기본 에이전트 패턴

```python
# agent/base_agent.py
from abc import ABC, abstractmethod
from llm.llama_server_llm import LLMClient
from tools import ToolRegistry

class BaseAgent(ABC):
    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry
    
    @abstractmethod
    def run(self, query: str) -> str:
        """에이전트 실행"""
        pass
```

### ReAct 패턴 구현 예시

```python
# agent/react_agent.py
from .base_agent import BaseAgent

class ReActAgent(BaseAgent):
    """Reasoning + Acting 패턴 에이전트"""
    
    def run(self, query: str) -> str:
        thoughts = []
        max_iterations = 5
        
        for _ in range(max_iterations):
            # 1. Reasoning: 다음 행동 결정
            action = self._plan_next_action(query, thoughts)
            
            if action["type"] == "tool_call":
                # 2. Acting: 도구 실행
                tool_name = action["tool"]
                tool_args = action["args"]
                result = self.registry.call(tool_name, **tool_args)
                thoughts.append({
                    "action": action,
                    "observation": result.content or result.error
                })
                
            elif action["type"] == "final_answer":
                return action["answer"]
        
        return self._synthesize_answer(query, thoughts)
    
    def _plan_next_action(self, query: str, thoughts: list) -> dict:
        # LLM에게 사용 가능한 도구와 현재까지의 생각을 전달
        tools_schema = self.registry.list_schemas()
        prompt = self._build_reasoning_prompt(query, thoughts, tools_schema)
        return self.llm.ask_json(
            "You are a reasoning agent...",
            prompt,
            reasoning=True
        )
```

### Tool-Use 패턴 구현 예시

```python
# agent/tool_use_agent.py
from .base_agent import BaseAgent

class ToolUseAgent(BaseAgent):
    """단일 턴 도구 사용 에이전트"""
    
    def run(self, query: str) -> str:
        # 1. LLM에게 도구 선택 요청
        tools_schema = self.registry.list_schemas()
        decision = self.llm.ask_json(
            self._build_system_prompt(tools_schema),
            query,
            reasoning=True
        )
        
        # 2. 도구 호출
        if decision.get("tool_call"):
            tool_name = decision["tool_call"]["name"]
            tool_args = decision["tool_call"]["arguments"]
            result = self.registry.call(tool_name, **tool_args)
            
            # 3. 결과 기반 최종 응답 생성
            return self._generate_response(query, result)
        
        # 도구 없이 직접 응답
        return decision.get("answer", "")
```

---

## 🛠️ 에이전트 추가 가이드

### Step 1: 에이전트 클래스 작성

```python
# agent/my_agent.py
from llm.llama_server_llm import LLMClient
from tools import ToolRegistry

class MyAgent:
    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry
    
    def run(self, query: str) -> str:
        # 에이전트 로직 구현
        pass
```

### Step 2: `__init__.py` 작성

```python
# agent/__init__.py
from .my_agent import MyAgent

__all__ = ["MyAgent"]
```

### Step 3: `main.py`에 통합 (선택)

```python
# main.py
from agent import MyAgent

if args.mode == "agent":
    agent = MyAgent(llm=llm, registry=registry)
    result = agent.run(args.query)
```

---

## 📐 설계 원칙

1. **의존성 주입**: LLM과 ToolRegistry를 생성자에서 주입받음
2. **상태 최소화**: 가능한 한 stateless하게 설계
3. **오류 처리**: 도구 실패 시 graceful한 폴백 전략 구현
4. **로깅**: 디버깅을 위한 충분한 로깅 포함

---

## 🔗 의존성

```
agent/ (향후 구현 시)
├── base_agent ──▶ llm/LLMClient
│             ──▶ tools/ToolRegistry
│
├── react_agent ──▶ base_agent
│
└── tool_use_agent ──▶ base_agent
```

**의존성 방향**:
- `agent/` → `tools/` (Registry를 통한 도구 호출)
- `agent/` → `llm/` (LLM 직접 호출)
- `agent/` → `services/` (상태 관리, 선택적)
