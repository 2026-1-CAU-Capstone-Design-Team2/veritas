# services/proactive — Rule-based Proactive Pipeline

> 사용자가 native editor / external Windows 문서 앱에서 작성 중일 때
> "언제 / 무엇을 / 어떻게 도울지"를 결정. Bandit/RL은 사용하지 않고,
> 어휘 키워드 features도 사용하지 않음. 결정은 deterministic.

상위 문서:
- 아키텍처 전반: [`../../PROACTIVE_RULE.md`](../../PROACTIVE_RULE.md)
- 원본 지시서: [`../../veritas_proactive_rule_based_reimplementation.md`](../../veritas_proactive_rule_based_reimplementation.md)

---

## 1. 디렉토리 구조

```
services/proactive/
  models.py              ProactiveObservation / FeedbackRecord 등 데이터 모델
  anchors.py             ActiveAnchor (커서/선택 영역의 confidence 기반 표현)
  proposal_models.py     ProactiveTask / NullPrediction / SurfaceCapabilities
  features.py            primitive 정규화 (lexical keyword 금지)
  candidates.py          CandidateFactory — anchor-local, no LLM
  evaluator.py           hard gates + 0..1 rubric score
  adaptation.py          UserAdaptationMemory (threshold/cooldown/suppression)
  context_selector.py    ContextBundle 빌더 (anchor-relative only)
  generator.py           ProactiveTask + ContextBundle → SSE 라우터
  reward.py              canonical feedback 매핑
  policy_store.py        per-workspace JSONL 로그 + adaptation glue
  timeout_monitor.py     render 타임아웃 sweeper
  null_outcome_monitor.py TN/FN proxy 분류기
  telemetry.py           console + per-workspace log file
  screen_bridge.py       ChatAgent 스크린 파이프라인 글루
  orchestrator.py        observe / record_feedback / explain
  action_space.py        deprecated stub (bandit era 잔재)
  legacy_bandit/         frozen reference — NEVER import in production
```

Prompt 문자열은 이 디렉토리에 두지 않음. 모두
[`../../core/prompts/proactive.py`](../../core/prompts/proactive.py)에 모음
(`FORMAT_CONTRACT_EXTERNAL`, `LEAD_IN_EXTERNAL`, `LEAD_IN_NATIVE`,
`native_retry_lead_in`, `lead_in_for`).

---

## 2. 금지 원칙 (영구 규칙)

다음은 **재도입 금지** — 회귀 가드 테스트
([`tests/test_proactive_features.py::NoKeywordModulesTests`](../../tests/test_proactive_features.py))
가 실패하게 됨:

1. **Hard-coded 어휘 키워드** (예: `("근거", "출처", "자료", ...)`).
   도메인/언어 일반화를 깨고 모델 편향을 만듦. 어휘 정보가 필요하면
   학습된 신호 (RAG retrieval 등) 로만.
2. **Bandit / RL 정책으로 production 결정**. 
   `services.proactive.legacy_bandit/`의 코드를 production path에서
   import하면 안 됨 (
   [`tests/test_proactive_api.py::test_orchestrator_module_does_not_import_bandit`](../../tests/test_proactive_api.py)
   가 가드).
3. **Generator 자체에 prompt 문자열 정의**.
   모든 prompt copy는 `core/prompts/`로.
4. **High-frequency observe 단계에서 RAG retrieval 호출**.
   generation 시점에만 호출 가능.

---

## 3. Native Reject Ladder (이 문서가 주관하는 규칙)

`services/proactive/orchestrator.py`에 in-memory 구현
(`_anchor_reject_state: dict[key, _AnchorRejectState]`).
JSON 영구화 **안 함** — 세션 단위 UX 게이트이지 학습 상태가 아님.

ladder state는 **anchor_id 정확 일치 → cursor 근접(proximity) 매칭** 순으로 조회된다
(`_match_state_key_locked`). raw `anchor_id`는 주변 단락 텍스트 해시를 포함하므로 스페이스
한 번만 쳐도 새 id가 만들어진다 — 그러면 (a) 3-reject cooldown을 타이핑으로 우회할 수 있고
(b) "ESC → 몇 글자 타이핑 → 재제안" 흐름에서 ladder가 누적되지 않는다. 그래서 ladder는
**같은 document + cursor가 `NATIVE_ANCHOR_PROXIMITY_CHARS`(기본 24자) 이내**면 동일한
편집 지점으로 보고 같은 state에 매칭한다 (`VERITAS_PROACTIVE_ANCHOR_PROXIMITY_CHARS`로 조정).
이 window는 **의도적으로 작다** — 스페이스/소규모 편집은 흡수하되, 사용자가 **다른 문장·
문단으로 커서를 옮기면 cooldown이 즉시 풀려야** 하기 때문(문장 크기였던 옛 120자는 lock이
영구처럼 느껴지게 했다).

### 3.1 규칙

| Reject 횟수 (같은 anchor) | 다음 observe에서의 동작 | Prompt에 들어가는 실제 context |
|---|---|---|
| 0 | `iter_ghostwrite`로 standard ghost continuation | (편집기 raw prefix, 최대 ~1500자) |
| **1** | `editor_assist`로 전환 + `[금지: 이전 거절된 제안]` + 변경 지시 | **`[직전 N문장]` — prefix에서 마지막 2~3 문장 추출하여 prompt body에 그대로 삽입** |
| **2** | (1)과 동일 + "방향/주제 자체를 다르게" 강화 지시 | **`[현재 문단 전체]` — `observation.current_paragraph` 텍스트를 통째로 prompt body에 삽입** |
| **3** | 이 anchor에 대해 **180초 cooldown 발동** — 모든 후속 observe가 `prediction=null reason=anchor_reject_cooldown`으로 막힘 | — (no prompt) |

문단 텍스트는 **명시적 라벨 블록 (`[현재 문단 전체]: ...`)으로 prompt에
그대로 들어갑니다** — instruction-only가 아닙니다. 구현: 
[`generator.py:_native_retry_context_block`](generator.py) +
[`core/prompts/proactive.py:native_retry_lead_in`](../../core/prompts/proactive.py).

retry 경로(level≥1)는 `editor_assist("continue")`를 쓰지만 **additive grounding**으로
호출된다(`additive_grounding=True`): 워크스페이스 RAG 인덱스가 있으면 근거로 쓰되, 없으면
plain 생성으로 fallback 한다. 따라서 자료조사/로컬 corpus가 없는 워크스페이스에서 거절해도
재제안이 `EditorGroundingUnavailable`로 죽지 않는다. (사용자 클릭 quick action은 여전히
hard-gate.)

Constants (`orchestrator.py` 상단):
```python
NATIVE_ANCHOR_REJECT_LIMIT = 3
NATIVE_ANCHOR_REJECT_COOLDOWN_S = 180.0
```

**`last_rejected_text` 전달**: reject(ESC/타이핑 덮어쓰기)뿐 아니라 "다시"(retry)도 직전
거절 텍스트를 기억하며, observe는 reject_level과 **무관하게** 이 텍스트를 task metadata로
전달한다 (retry는 count를 올리지 않으므로 reject_level이 0이어도 전달돼야 함). generator의
native-retry 경로는 `reject_level >= 1` **또는** non-empty `avoid_text` 중 하나면 발동하므로,
"다시" 한 번에도 직전 제안을 피한 새 문장이 나온다.

### 3.2 보장되는 동작

- **다른 문장/문단으로 이동 시 lock 해제** — proximity window(기본 24자)를 벗어난 cursor는 새 편집 지점 → 새 reject ladder, 기존 cooldown 무영향. (window 안의 작은 편집은 같은 ladder로 누적된다 — §3 상단 참조.)
- **작은 cursor jitter에도 cooldown 유지** — 3-reject 후 스페이스 한 번(또는 몇 글자 타이핑)으로 anchor_id가 바뀌어도 proximity 매칭으로 같은 cooled state를 찾아 `anchor_reject_cooldown`을 유지한다.
- **Accept 한 번이 ladder를 초기화** — 그 지점의 reject_count, cooldown_until, last_rejected_text 모두 지움. "사용자가 결국 만족한 신호".

### 3.3 Adaptation layer와의 관계

`adaptation.py`가 관리하는 두 가지 게이트는 **native에서는 모두 비활성화**:

1. **per-(anchor, task) JSON cooldown** —
   `apply_feedback(surface="native_editor")`일 때 `_apply_reject`/
   `_apply_timeout`이 `anchor_cooldowns` 추가를 skip.
2. **global task-type suppression** (`task_type_stats[t].suppressed_until`)
   — `_apply_reject(suppress_task_type=False)`로 호출되어
   `recent_reject_iso`에 append도 안 함. native는 `next_sentence` 하나뿐이라
   "5번 reject 후 task type 전체 차단"이 사실상 "전부 차단"이 됨 → rule 4
   ("타 anchor에서는 작동")와 충돌. orchestrator의 in-memory ladder가
   **유일한** native 게이트.

다른 anchor에서 native 작업을 시작하면 ladder state가 fresh이므로 `same_task_recently_rejected` / `cooldown_same_anchor_task` 모두 통과합니다. 같은 anchor로 돌아오면 ladder state 그대로.

External (Word/PPT 등)에서는 JSON 기반 cooldown + task-type suppression 그대로 유지 — external은 6가지 task type이 있어 한 type suppression이 다른 type을 막지 않음.

#### Legacy 잔재 청소

이전 버전(pre-fix)에서는 native reject도 `recent_reject_iso`에 누적했습니다. 그 stale ISO들은 `_apply_reject`가 호출될 때만 GC되는데, native에서 더 이상 append가 안 일어나면 영원히 남게 됨. **load 시점에 `_gc_locked_internal`을 한 번 돌려 청소**합니다 ([`adaptation.py:_load_or_init`](adaptation.py)).

### 3.4 "다시" 버튼 (retry)

`다시`는 reject_count를 **증가시키지 않음**. 그러나
- `last_rejected_text` 슬롯은 갱신 (frontend가 보내는 `generated_text` metadata)
- 다음 observe는 reject_level 그대로 (사용자가 reject 신호를 명시적으로 안 줬으므로)
- 다음 generator path는 retry lead-in 적용 (last_rejected_text가 있으므로)

즉 `다시`는 "내용을 다시 써줘"라는 부드러운 신호. 같은 anchor에 reject + 다시를 섞어도 reject_count는 reject만 누적.

---

## 4. 점수 계산 / Threshold

[`evaluator.py`](evaluator.py):

```
score = 0.30·anchor_confidence + 0.20·need_signal + 0.20·context_sufficiency
      + 0.15·task_fit + 0.10·source_support
      − 0.20·interruption_risk − 0.15·recent_negative_rate
```

`BASE_SHOW_THRESHOLD = 0.50` (cold-start 친화적). `threshold_offset`은
accept(−0.015) / reject(+0.030) / timeout(+0.010) / wrong_anchor(+0.005)로
누적. `recent_negative_rate` × 0.15도 더해짐.

`threshold = +inf`는 sentinel — "cooldown 또는 task_type suppression이 활성"이라는 의미.
콘솔에서는 `threshold=BLOCKED(cooldown_or_suppression)`으로 표시.

---

## 5. Env Override (운영자용)

코드 수정 없이 튜닝 가능한 환경 변수:

| Var | 기본 | 역할 |
|---|---|---|
| `VERITAS_PROACTIVE_LOG` | 0 | proactive 로그를 stdout에도 출력 |
| `VERITAS_PROACTIVE_ANCHOR_COOLDOWN_S` | 60 | adaptation 계층의 anchor cooldown 초 (external만 적용) |
| `VERITAS_PROACTIVE_ANCHOR_TIMEOUT_COOLDOWN_S` | 30 | timeout 시 anchor cooldown 초 |
| `VERITAS_PROACTIVE_SUPPRESSION_REJECTS` | 5 | task_type suppression 발동 reject 횟수 (rolling 15분) |
| `VERITAS_PROACTIVE_SUPPRESSION_S` | 300 | suppression 지속 초 |
| `VERITAS_PROACTIVE_SUPPRESSION_WINDOW_S` | 900 | reject 카운팅 rolling 윈도우 |
| `VERITAS_PROACTIVE_SCREEN` | 1 | screen pipeline → proactive 라우팅 ON/OFF |

Native reject ladder의 상수 (`NATIVE_ANCHOR_REJECT_LIMIT`,
`NATIVE_ANCHOR_REJECT_COOLDOWN_S`)는 현재 env override 미지원 — 필요하면
`orchestrator.py` 상수 수정.

---

## 6. 영구화 파일

`runs/<workspace_id>/proactive_policy/`:

```
user_adaptation.json      EMA / threshold_offset / anchor_cooldowns (external 전용) /
                          task_type_stats / suppressed_until
decisions.jsonl           모든 observe — gate_reasons, evaluator_breakdown, primitive,
                          context char_counts (NO raw text)
feedback.jsonl            canonical 매핑 + adaptation changes + metadata
updates.jsonl             adaptation state delta
null_outcomes.jsonl       TN/FN proxy 분류
pending_timeouts.jsonl    render 타임아웃 큐
proactive.log             사람이 읽을 수 있는 timeline (telemetry.py)
```

**Native reject ladder는 의도적으로 영구화하지 않음** — `orchestrator._anchor_reject_state` 딕셔너리만. 앱 재시작 시 초기화. 운영 비용 (file I/O loop) 최소화.

---

## 7. Telemetry 예시

`python launcher.py --proactive-debug` 실행 시 콘솔 출력:

```
[proactive][decision] pd_abc... native_editor task=next_sentence anchor=anc_xyz
  conf=0.95 scope=cursor_previous_sentences render=native_ghost
  score=0.789 threshold=0.500 candidates=1 idle=0.5s churn=0.05 recent_neg=0.10

[proactive][feedback] pd_abc... native_editor reject task=next_sentence anchor=anc_xyz Δthr=+0.030

[proactive][decision] pd_def... native_editor task=next_sentence anchor=anc_xyz
  conf=0.95 scope=cursor_previous_sentences render=native_ghost
  score=0.786 threshold=0.530 candidates=1 idle=0.5s churn=0.05 recent_neg=0.28
  # 이번 observe는 generator가 native_retry_lead_in 경로로 갑니다
  # (task.metadata["last_rejected_text"]가 설정되어 있으므로)

[proactive][feedback] pd_def... native_editor reject ...
[proactive][feedback] pd_ghi... native_editor reject ...

[proactive][decision] pd_jkl... native_editor prediction=null
  reason=anchor_reject_cooldown candidates=0 gates=[per_anchor_3_reject_cooldown]
  threshold=BLOCKED(cooldown_or_suppression)
  # ← 3번 reject → 180초 cooldown 발동
```
