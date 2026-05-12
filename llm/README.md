# llm/

## 최신 서버 포트 규칙

- 채팅/생성 LLM 서버는 기본 `127.0.0.1:8080`입니다.
- embedding 서버는 기본 `127.0.0.1:8081`입니다.
- API runtime은 `VERITAS_EMBED_PORT`가 없으면 8081을 사용합니다.
- CLI `main.py`도 `--embed-port` 기본값을 8081로 사용합니다.
- `LLMClient` 자체는 `embed_host`/`embed_port`를 명시하지 않으면 chat endpoint를 embedding endpoint로 재사용할 수 있지만, VERITAS API/CLI 진입점은 분리 서버 구성을 기본으로 넘깁니다.

> Tool-calling update: multi-turn RAG chat uses a two-step path: `LLMClient.collect_tool_outputs(...)` lets the model decide whether to call `rag`, then the final answer is generated through normal `LLMClient.ask(..., stream=True)` without tools. The model decides from the schema; no regex-based user-prompt gating is part of the LLM layer.

**역할**: LLM(Large Language Model) 백엔드와의 통신을 담당하는 클라이언트 모듈

---

## 📋 개요

`llm/` 디렉토리는 llama-server 등 OpenAI API 호환 LLM 백엔드와의 통신을 추상화합니다. Chat Completion과 Embedding 기능을 통합된 인터페이스로 제공합니다.

---

## 📁 디렉토리 구조

```
llm/
└── llama_server_llm.py    # LLMClient 클래스 정의
```

---

## 🏗️ 핵심 컴포넌트

### `LLMClient` 클래스

```python
class LLMClient:
    """OpenAI-compatible client for llama-server (LLM + Embeddings)."""
    
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        embed_host: str | None = None,    # 별도 임베딩 서버 (선택)
        embed_port: int | None = None,
        stream_summary: bool = False,      # 스트리밍 출력 옵션
        stream_reasoning: bool = False,
        trace_latency: bool = True,        # 지연시간 로깅
    ): ...
```

---

## 🔧 주요 메서드

### 1. `ask()` - 텍스트 응답 생성

```python
def ask(
    self,
    system_prompt: str,
    user_prompt: str,
    reasoning: bool = False,    # 추론 모드 (think 태그 활성화)
    stream: bool = False,
    stream_label: str = "",
) -> str:
```

**사용 예**:
```python
client = LLMClient()
response = client.ask(
    system_prompt="You are a helpful assistant.",
    user_prompt="파이썬의 장점을 설명해주세요."
)
```

### 2. `ask_json()` - JSON 응답 생성 및 파싱

```python
def ask_json(
    self,
    system_prompt: str,
    user_prompt: str,
    reasoning: bool = False,
    max_retries: int = 2,       # JSON 파싱 실패 시 재시도
) -> dict[str, Any]:
```

**사용 예**:
```python
result = client.ask_json(
    system_prompt="Respond in JSON format.",
    user_prompt="다음 텍스트에서 키워드를 추출하세요: ..."
)
# result: {"keywords": ["AI", "machine learning", ...]}
```

### 3. `embed()` / `embed_batch()` - 임베딩 생성

```python
def embed(self, text: str) -> list[float]:
    """단일 텍스트 임베딩"""

def embed_batch(self, texts: list[str]) -> list[list[float]]:
    """배치 임베딩 (8개씩 분할 처리)"""
```

**사용 예**:
```python
embedding = client.embed("검색할 문장")
# embedding: [0.123, -0.456, ...]

embeddings = client.embed_batch(["문장1", "문장2", "문장3"])
```

---

## ⚙️ 설정 파라미터

### Sampling Parameters

```python
SAMPLING_PARAMS = {
    "temperature": 1.0,
    "top_p": 0.95,
    "presence_penalty": 1.5,
}

EXTRA_SAMPLING_PARAMS = {
    "top_k": 20,
    "min_p": 0.0,
    "repeat_penalty": 1.0,
}
```

### Reasoning Mode

`reasoning=True` 설정 시:
- `<think>...</think>` 태그 내에서 추론 과정 생성
- 더 정교한 답변 생성 가능
- 토큰 사용량 증가

---

## 🔌 백엔드 연결 구조

```
┌─────────────────────────────────────────────────────┐
│                     LLMClient                        │
├─────────────────────┬───────────────────────────────┤
│   Chat Client       │      Embed Client             │
│  (OpenAI SDK)       │     (OpenAI SDK)              │
└─────────┬───────────┴───────────────┬───────────────┘
          │                           │
          ▼                           ▼
   ┌──────────────┐           ┌──────────────┐
   │ llama-server │           │ embed-server │
   │ :8080/v1     │           │ :8081/v1     │
   │ /chat/       │           │ /embeddings  │
   │ completions  │           │              │
   └──────────────┘           └──────────────┘
```

- **단일 서버**: `host`, `port`만 지정
- **분리 서버**: `embed_host`, `embed_port` 추가 지정

---

## 🛠️ 사용 예시

### 기본 사용

```python
from llm.llama_server_llm import LLMClient

# 초기화
llm = LLMClient(
    host="127.0.0.1",
    port=8080,
    trace_latency=True,
)

# 텍스트 응답
response = llm.ask(
    "You are a helpful assistant.",
    "AI 윤리의 주요 쟁점은 무엇인가요?",
    reasoning=True,  # 추론 모드 활성화
)

# JSON 응답
plan = llm.ask_json(
    PLANNER_PROMPT,
    user_request,
    reasoning=True,
)
```

### 임베딩 서버 분리

```python
llm = LLMClient(
    host="127.0.0.1",
    port=8080,
    embed_host="127.0.0.1",
    embed_port=8081,
)

# 채팅은 8080, 임베딩은 8081 사용
response = llm.ask(...)
embedding = llm.embed("텍스트")
```

### 스트리밍 출력

```python
llm = LLMClient(
    stream_summary=True,   # 요약 시 스트리밍
    stream_reasoning=True, # 추론 과정 스트리밍
)

# 스트리밍으로 응답 생성
response = llm.ask(
    system_prompt,
    user_prompt,
    stream=True,
    stream_label="summary",
)
```

---

## 📐 설계 원칙

1. **Adapter Pattern**: OpenAI SDK를 통해 다양한 백엔드 추상화
2. **Fail-Safe JSON 파싱**: 마크다운 코드블록, `<think>` 태그 등 자동 처리
3. **Batch 최적화**: 임베딩 배치 처리 시 청크 분할로 안정성 확보
4. **Observability**: `trace_latency=True`로 성능 모니터링

---

## 🔗 의존성

- **openai**: OpenAI Python SDK
- 상위 모듈(`tools/`, `workflows/`, `services/`)에서 이 모듈을 사용
- 이 모듈은 다른 프로젝트 모듈에 의존하지 않음 (독립적)
