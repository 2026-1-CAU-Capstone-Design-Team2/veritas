# Screen Tools Refactor

화면 개입(intervention) 판단 구조를 **고정 시퀀스 게이트**에서 **공통 게이트 + 시나리오 체크**로 전환한 리팩터 기록.

---

## 1. 기존 구조와 문제점

### 기존 구조

모든 capture가 아래 5단계 게이트를 **순차적으로 전부 통과**해야만 LLM 개입 여부(`should_consider_llm`)를 줄 수 있었다.

```
editing_app → dwell → stable_paragraph → typing_pause → cool_down
```

### 문제점

- 5개 게이트가 하나의 경로로 **일원화(하드코딩)** 되어 있었다.
- `typing_pause`, `cool_down`이 전역 사전 조건으로 박혀 있어, 상황별로 다른 개입 패턴을 표현할 수 없었다.
  - 예: "한 문단 쓰고 멈춤"과 "문서 전반을 길게 쓰고 멈춤"은 멈춤 판정·쿨다운 기준이 달라야 하는데, 고정 시퀀스는 한 가지 형태만 강제했다.
- 게이트 조건들이 서로 충돌해, LLM 개입 자체는 가능했지만 **알맞은 상황에 개입하지 못했다.**

---

## 2. 신규 구조: default gating + scenario check

판단을 두 층으로 분리했다.

```
[default gating]                          [scenario check]
editing_app → dwell → stable_paragraph  →  scenario fan-out → CFS 선택
   (모든 시나리오 공통 진입 조건)              (시나리오별 게이트는 시나리오 안에 캡슐화)
```

- **default gating** — 기존과 동일하게 screen tools에서 3가지 공통 게이트로 시퀀스 검사를 수행한다. 하나라도 실패하면 즉시 `intervention_type="none"`으로 종료한다.
- **scenario check** — 3가지 기본 게이트를 통과하면 시나리오 체킹에 들어간다. `typing_pause` / `cool_down` 같은 조건은 전역 게이트에서 빠지고, 각 시나리오 객체 내부의 게이트로 옮겨졌다.

핵심 진입점: [`InterventionDetector.decide()`](intervention_detector.py:69)

---

## 3. Default gating — 3가지 공통 게이트

[`intervention_detector.py:92-141`](intervention_detector.py:92) 에서 수행. 세 게이트를 모두 통과해야 시나리오 체크로 넘어간다.

| 게이트 | 통과 조건 | 실패 blocker |
|--------|-----------|--------------|
| `editing_app` | `active_app_type ∈ {document, presentation, spreadsheet, code_editor}` | `not_editing_app` |
| `dwell` | `history_count >= 5` 그리고 `dwell_ratio >= 0.5` (같은 `document_key`에 충분히 머묾) | `insufficient_dwell` |
| `stable_paragraph` | 현재 문단이 충분한 길이·신뢰도를 가짐 (UIA/app text: 20자·conf 0.8 / OCR: 40자·conf 0.55) | `unstable_current_paragraph` |

하나라도 실패하면 [`intervention_detector.py:126-141`](intervention_detector.py:126) 에서 short-circuit 종료.

---

## 4. Scenario check — fan-out + CFS 선택

3가지 기본 게이트를 통과하면 시나리오 체킹 단계로 진입한다.

1. **fan-out** — 등록된 모든 `ScenarioType.evaluate()`를 호출한다 ([`intervention_detector.py:152-153`](intervention_detector.py:152)). ready 여부와 무관하게 전부 평가해 telemetry에 남긴다.
2. **CFS 선택** — ready된 시나리오가 여럿이면 `ScenarioScheduler.select_and_charge()`가 vruntime이 가장 낮은 시나리오 **하나만** 고른다 ([`intervention_detector.py:158-168`](intervention_detector.py:158)).
3. 선택된 시나리오 이름으로 `InterventionDecision`을 만든다. ready 시나리오가 없으면 `intervention_type="none"`.

이전 구조와 달리 "어떤 상황에 개입할지"는 고정 시퀀스가 아니라 **시나리오 집합 + CFS 스케줄러**가 결정한다.

---

## 5. 시나리오 구조

각 시나리오는 [`scenarios.py`](scenarios.py) 의 `ScenarioType` 서브클래스로, 자기 게이트 · priority · CFS 가중치(`initial_vruntime`, `vruntime_increment`)를 캡슐화한다.

### 공통 입력 / 출력

- `ScenarioContext` — 한 capture 사이클에서 모든 시나리오가 공유하는 입력 스냅샷.
- `ScenarioEvaluation` — 시나리오별 평가 결과 통일 포맷 (`ready`, `score`, `priority`, `reasons`, `blockers`, `gate_results`, `metadata`).

### 현재 등록된 시나리오

| 시나리오 | initial_vruntime | vruntime_increment | 내부 게이트 |
|----------|-----------------:|-------------------:|-------------|
| `IdleAfterWritingScenario` | `0.0` | `1.0` | `typing_pause`, `paragraph_cooldown` |
| `WholeDocumentReviewScenario` | `-10.0` | `5.0` | `sustained_writing`, `idle_after_sustained`, `document_cooldown` |

> 기존 전역 게이트였던 `typing_pause` / `cool_down`이 `IdleAfterWritingScenario` **내부 게이트**로 이동했다. `WholeDocumentReviewScenario`는 같은 의도(멈춤 + 쿨다운)를 자기 상황에 맞는 별도 게이트(`idle_after_sustained`, `document_cooldown`)로 다시 정의한다 — 시나리오마다 다른 판정을 쓸 수 있게 된 것이 이번 리팩터의 핵심.

함수·메서드 단위 상세는 [`scenarios_reference.md`](scenarios_reference.md) 참고.

---

## 6. 관련 파일

| 파일 | 역할 |
|------|------|
| [`intervention_detector.py`](intervention_detector.py) | default gating + scenario fan-out + CFS 선택 오케스트레이션 |
| [`scenarios.py`](scenarios.py) | `ScenarioType` 추상 베이스 및 개별 시나리오 정의 |
| [`scenario_scheduler.py`](scenario_scheduler.py) | CFS-like vruntime 스케줄러 (`select_and_charge`, lazy decay, reset, persistence) |
| [`README.md`](README.md) | 모듈 전체 동작·수집 파이프라인·payload·저장 구조 문서 |
