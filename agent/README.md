# agent/

`agent/`는 LLM과 tool registry를 연결하는 대화형 agent loop를 담당합니다.

현재 구현:

```text
agent/chat_agent.py
```

---

## ChatAgent 책임

`ChatAgent`는 다음만 담당합니다.

```text
1. multi-turn chat loop
2. recent chat history formatting
3. chat 단계 tool allowlist 적용
4. LLM tool-call decision 실행
5. tool result 수집
6. current user message + current tool result 기반 final answer 생성
7. 완성된 assistant answer를 history에 1회 append
```

`ChatAgent`가 하지 않는 것:

```text
- web search 직접 수행
- AutoSurvey 내부 단계 실행 순서 관리
- RAG indexing/retrieval 구현
- 정규표현식/단어 목록 기반 tool 강제 라우팅
```

---

## Chat-visible tools

현재 chat agent가 LLM에게 expose하는 tool은 아래 3개입니다.

```text
current_time
rag_search
autosurvey
```

`web_search`, `fetch_webpage`, `query_plan`, `document_summarize`, `final_report`는 chat agent에 직접 expose하지 않습니다.

새로운 조사가 필요하면 LLM은 `autosurvey`를 호출하고, AutoSurvey workflow 내부가 web 검색/문서 수집/요약을 수행합니다.

---

## Tool selection 원칙

ChatAgent는 user query를 코드로 분류하지 않습니다.

```text
허용:
- stage-level allowlist
- tool schema description
- system prompt policy
- tool result validation/fallback

금지:
- "이 단어가 있으면 rag_search" 같은 trigger
- 정규표현식으로 autosurvey 강제 호출
- 특정 tool을 위해 chat loop에 개별 예외 분기 추가
```

---

## Message append 규칙

한 user turn의 순서:

```text
User message
  → optional tool-call decision
  → allowed tool execution
  → final answer generation from current message + current tool result
  → append (user, assistant) to chat_history exactly once
```

이 구조는 tool이 추가되어도 변하지 않아야 합니다. 새 tool은 schema와 registry에 추가하고 allowlist에 포함시키면 됩니다.
