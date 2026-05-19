# 시나리오별 스크린 개입 프롬프트 설계 (b안)

스크린 개입(screen intervention) LLM 호출에 시나리오별 prompt guidance를 추가하기 위한 설계 문서.

## 1. 배경

### 현재 구조
- `core/prompts.py:277` — `SCREEN_INTERVENTION_SYSTEM_PROMPT` : 단일 시스템 프롬프트 (전 시나리오 공유)
- `core/prompts.py:296` — `SCREEN_INTERVENTION_USER_PROMPT_TEMPLATE` : 단일 유저 템플릿, 슬롯 5개 (`history`, `app_context`, `writing_context`, `routing_hint`, `knowledge_context`)
- 시나리오 구분은 payload의 enum 값(`writing_context.focus_scope`, `tool_routing_hint.tone` / `preferred_action`)으로만 전달됨

### 문제
- `tone=unstick` 같은 enum이 **설명 없이 날것으로** LLM에 전달됨 — 무슨 상황이고 좋은 응답이 뭔지 모름
- `README.md:243`이 이미 지적한 gap: *"`answer_screen_intervention`이 `intervention_type`을 안 읽고 `writing_context` + `tool_routing_hint`만 넣는다"*

### 목표
시나리오(`intervention_type`)별로 "상황 의미 + 좋은 응답의 모양 + 톤"을 풀어주는 guidance를 프롬프트에 추가.

## 2. 선택 근거 (a / b / c)

| 안 | 분기 대상 | 공통 규칙 위치 | 평가 |
|---|---|---|---|
| (a) | 시스템 프롬프트를 시나리오별 5벌 | 5벌 복제 → drift | ✗ 분기 단위가 실제 차이(2~5문장)보다 과함, 시스템 프롬프트 캐시 깨짐 |
| **(b)** | **분기 없음, 작은 guidance 블록만 주입** | **1곳 유지** | **✓ 문제의 실제 모양과 일치, blast radius 최소, 점진 도입 가능** |
| (c) | 유저 템플릿을 시나리오별 5벌 | 일부 5벌 복제 | ✗ 5개 슬롯 구조 복제가 무의미 (5개 다 동일 입력 사용) |

핵심: 시나리오별로 **실제 달라야 하는 양은 2~5문장**뿐. 공통 규칙(언어 정책, 인용 형식, 내부 구현 언급 금지, "짧게")은 전 시나리오 동일 → 큰 객체를 5벌 복제할 이유가 없다. → **(b) 채택**.

## 3. 설계

### 3-1. `core/prompts.py` — 새 dict `SCREEN_SCENARIO_GUIDANCE`

- 기존 두 상수 옆에 추가
- key = `intervention_type` 문자열 (시나리오 `name`)
- value = 2~5문장 영어 산문 (프롬프트 scaffolding은 영어, LLM 답변 언어는 시스템 프롬프트의 언어 정책이 결정)
- **담는 것**: 상황 의미 / 좋은 응답의 모양 / 톤·길이 뉘앙스
- **안 담는 것**: 언어 정책·인용 형식·내부 구현 언급 금지·"짧게" → 전부 시스템 프롬프트 소관. **중복 금지** (중복하면 (a)의 drift 문제가 그대로 옴)
- 각 블록은 해당 시나리오의 `tool_routing_hint`(`tone` / `preferred_action`)와 **일관**되게 — guidance는 그 enum의 산문 설명층

### 3-2. `core/prompts.py` — 유저 템플릿에 슬롯 추가

- 슬롯 `{scenario_guidance}`, 라벨 `SCENARIO GUIDANCE:`
- 위치: `INTERVENTION ROUTING HINT` 섹션 **바로 뒤** — 기계 enum(routing_hint) 옆에 그 산문 설명(guidance)을 붙여 LLM이 같이 읽게
- 기존 5슬롯은 그대로 유지

### 3-3. fallback

- 미지정 `intervention_type`(`none` 포함)일 때: 빈 문자열 대신 generic 한 줄
  - 예: `"Respond helpfully to the on-screen situation, following the general rules above."`
- 효과: 템플릿 균일성 유지 + `format` KeyError 방지 + 새 시나리오 추가 시 dict 미작성이어도 안 깨짐

### 3-4. 시나리오별 guidance 내용

| intervention_type | 상황 | 좋은 응답 | 톤·길이 |
|---|---|---|---|
| `idle_after_writing` | 쓰던 문단에서 타이핑 잠깐 멈춤, 흐름 살아있음 | 마지막 1~2문장 받아 다음 문장 1개 제안 or 방금 쓴 내용 뒷받침 근거 짧게. 이어쓸 게 없으면 no_action | 부드럽게, 흐름 안 끊게, 문장 1개 수준 |
| `whole_document_review` | 상당 분량 써둠, 전체 볼 타이밍 | 개별 문장 교정 X — 논리 전개·구조·누락 논점 위주 핵심 2~3개 | 종합 검토, bullet 2~3개 |
| `long_static_review` | 문서가 오래 정적(편집 없이 열려만 있음), 읽으며 검토 중 추정 | 구체적 오탈자·어색한 표현·사실 오류를 콕 짚기. 추상적 조언 X | 교정자, "여기 이거" 식 구체적 |
| `paragraph_churn` | 한 문단 썼다 지웠다 반복 = 막힘 | 지금 문단 재작성 대안 표현 1~2개. 새 내용 추가가 아니라 막힌 지점 풀기 | unstick, 구체적 재작성 예시 |
| `blank_document_start` | 문서 거의 비어있음, 시작 단계 | 첫 문단 방향 1개 / 간단한 개요 / 도입부 한두 문장 제안 | 가볍게, 부담 X, 선택지 제시하듯 |

최종 영어 문안은 구현 단계에서 위 스펙대로 5개 작성.

### 3-5. `agent/chat_agent.py` — 배선

`answer_screen_intervention` (`chat_agent.py:493-531`):

```python
intervention_type = intervention.get("intervention_type") or "none"
scenario_guidance = SCREEN_SCENARIO_GUIDANCE.get(
    intervention_type, SCREEN_SCENARIO_GUIDANCE_DEFAULT
)
prompt = SCREEN_INTERVENTION_USER_PROMPT_TEMPLATE.format(
    ...,
    scenario_guidance=scenario_guidance,  # 인자 1개 추가
)
```

- `intervention_type`은 payload 최상위 키 (`intervention_dispatcher.py:79`에서 set)
- import에 `SCREEN_SCENARIO_GUIDANCE` 추가, `.format()`에 인자 1개 추가
- `llm.ask(SCREEN_INTERVENTION_SYSTEM_PROMPT, ...)` 호출부는 **무변경**

## 4. 비변경 / 안전성

- 시스템 프롬프트: 무변경 → 캐시·단일 출처 유지
- 유저 템플릿: 기존 5슬롯 유지, 1개만 추가
- 미지정 시나리오 → generic fallback → `format` KeyError 없음
- 점진 도입: dict 엔트리 하나씩 채워도 됨, 채우기 전엔 fallback 적용

## 5. 검증

- 스모크: 5개 `intervention_type` 각각으로 `.format()` 호출 → KeyError 없음 확인
- 실로그: `--screen-debug`로 각 시나리오 1회씩 발화 → 응답 톤이 3-4 스펙과 맞는지 확인

## 6. 구현 작업 순서

1. `core/prompts.py` — `SCREEN_SCENARIO_GUIDANCE` dict + `SCREEN_SCENARIO_GUIDANCE_DEFAULT` 추가, 시나리오별 영어 문안 5개 작성
2. `core/prompts.py` — `SCREEN_INTERVENTION_USER_PROMPT_TEMPLATE`에 `{scenario_guidance}` 슬롯 추가
3. `agent/chat_agent.py` — import + `answer_screen_intervention`의 `.format()` 배선
4. 스모크 + 실로그 검증
