# `scenarios.py` 기능 정리

화면 개입(intervention) 시나리오를 정의하는 모듈. 각 시나리오는 자신의 게이트 함수, 우선순위, CFS 스케줄링 파라미터를 들고 있는 객체이며, `evaluate()`로 통일된 평가 결과를 반환한다.

> 공통 게이트(`editing_app · dwell · stable_paragraph`)를 모두 통과한 캡처에 대해서만 `evaluate()`가 호출된다. 시나리오별 게이트는 이 안에 캡슐화된다.

---

## 모듈 레벨 함수

### `_event_document_key(event) -> str`
임의의 이벤트(현재 스냅샷이든 디스크에 저장된 이벤트든)에서 `document_key`를 추출한다. 우선순위: 최상위 `document_key` → `intervention.metadata.document_key` → 윈도우의 `process_name|window_title` 조합으로 fallback.

### `_event_paragraph_fingerprint(event) -> str`
이벤트에서 문단 지문(fingerprint)을 추출한다. 우선순위: 최상위 `paragraph_fingerprint` → `intervention.metadata.paragraph_fingerprint` → 현재 문단 텍스트를 정규화 후 SHA1 해시. 텍스트가 없으면 빈 문자열.

---

## 데이터 클래스

### `ScenarioContext`
한 캡처 사이클 동안 모든 시나리오가 공유하는 입력 스냅샷.
- `window`, `filtered`, `history_events`, `same_document_events`, `document_key`, `paragraph_fingerprint`
- `last_fired_at: dict[str, float]` — 문서 단위 `{시나리오명: 마지막 발동 unix_ts}`. detector가 scheduler 상태에서 읽어 채움. 시간 기반 cooldown 게이트가 사용.
- `last_fired_doc_chars: dict[str, int]` — 문서 단위 `{시나리오명: 마지막 발동 시점의 정규화 문서 길이}`. `last_fired_at`과 짝. `whole_document_review`의 "리뷰 이후 추가된 글자 수" 판정에 사용.

### `ScenarioEvaluation`
시나리오별 평가 결과를 담는 통일 포맷.
- 필드: `name`, `ready`, `score`, `priority`, `reasons`, `blockers`, `gate_results`, `metadata`

---

## `ScenarioType` (추상 베이스 클래스)

모든 개입 시나리오의 추상 베이스. 게이트 함수, 우선순위, CFS 파라미터(`initial_vruntime`, `vruntime_increment`)를 보유한다.

**클래스 상수**
- `_PRIORITY_VRUNTIME_DEFAULTS: dict[str, tuple[float, float]]` — `priority` 문자열에서 `(initial_vruntime, vruntime_increment)` default를 도출하는 매핑. 현재 `{"high": (-5.0, 3.0), "medium": (0.0, 2.0), "low": (5.0, 2.0)}`. 새 시나리오가 클래스 attribute로 vruntime 값을 명시하지 않으면 `__init__`이 이 default를 자동 적용. 자세한 컨벤션은 `vruntime_convention.md` 참조.

| 메서드 | 역할 |
|--------|------|
| `__init__` | `initial_vruntime` / `vruntime_increment`를 외부에서 주입받아 instance-level 오버라이드. 서브클래스가 클래스 attribute로 vruntime 값을 명시하지 않으면 `_PRIORITY_VRUNTIME_DEFAULTS`에서 priority 기반 default를 자동 적용 |
| `_default_vruntime_for_priority(priority)` | (classmethod) priority 문자열 → `(initial_vruntime, vruntime_increment)` default tuple. 모르는 priority는 medium fallback |
| `evaluate(context)` | **(추상)** 시나리오별 게이트를 실행하고 통일된 `ScenarioEvaluation`을 반환 |
| `writing_context_overrides()` | 이 시나리오가 발동될 때 `writing_context`에 병합할 부분 필드를 반환. 기본은 빈 dict(오버라이드 없음) |
| `tool_routing_hint_overrides()` | 이 시나리오가 발동될 때 `tool_routing_hint`에 병합할 부분 필드(`tone`, `preferred_action` 등)를 반환. 기본은 빈 dict |
| `_gate_result(passed, reason, extra)` | 게이트 결과를 `{passed, reason, ...extra}` 형태의 dict로 표준화하는 헬퍼 |
| `_has_substantial_paragraph(filtered, *, min_chars=20)` | 현재 문단이 개입 대상으로 쓸 만큼 충분한 길이인지 판정하는 **공유 헬퍼**. 문단 단위 시나리오만 자기 게이트로 호출하고, 문서 단위 시나리오는 호출하지 않으므로 짧은 문단에 막히지 않음. 공통 `stable_paragraph` 게이트는 더 이상 문단 길이를 검사하지 않으며(캡처 신뢰성만 확인), 그 책임이 이 헬퍼로 옮겨졌음 |
| `_time_cooldown_status(last_fired_at, *, min_seconds=None)` | **공유 시간 기반 cooldown 헬퍼.** `last_fired_at[self.name]`(직전 발동 시각)과 현재 시각을 비교해 `elapsed >= min_seconds`면 통과. `min_seconds=None`이면 `self.cooldown_min_seconds`를 사용. 4개 시나리오(`idle_after_writing`, `long_static_review`, `paragraph_churn`, `blank_document_start`)가 자신의 게이트로 호출. scheduler 상태에서 읽으므로 발동 기록이 회전으로 잊히지 않아 긴 cooldown도 실제로 강제됨 |

---

## `IdleAfterWritingScenario(ScenarioType)`

**시나리오**: 사용자가 글을 쓰다가 같은 문단에서 멈춤 → 부드러운 이어쓰기 제안.
- `name = "idle_after_writing"`, `priority = "medium"`, `initial_vruntime = 0.0`, `vruntime_increment = 1.0` (명시 override — convention default보다 increment 절반으로 자주 발동 허용)

| 메서드 | 역할 |
|--------|------|
| `__init__` | 임계값 파라미터 설정 (`min_paragraph_chars`, `min_changed_chars`, `min_idle_captures`, `idle_similarity_threshold`, `cooldown_events`, `cooldown_min_seconds=60.0`) |
| `evaluate(context)` | **4개 게이트**(`typing_pause`, `paragraph_cooldown`, `time_cooldown`, `substantial_paragraph`)를 평가 → score/reasons/blockers 누적 후 `ready` 판정 |
| `writing_context_overrides()` | `focus_scope`를 `"recent_writing"`으로 설정 |
| `tool_routing_hint_overrides()` | `tone="gentle_continuation"` 설정. 리서치 필요 여부/문단 길이에 따라 `preferred_action`을 `continue_writing` / `provide_supporting_material` / `no_action` 중 선택 |
| `_typing_pause_status(events)` | 최근 캡처들이 안정적으로 멈춰 있는지 검사. 안정 캡처 수가 임계 이상이고, 멈추기 직전에 의미 있는 텍스트 변화가 있었는지로 `ready` 판정 |
| `_passes_paragraph_cooldown()` | 최근 `cooldown_events`개 이력 중 같은 문서·같은 문단 지문으로 이미 개입한 적이 있으면 차단(중복 방지) |
| `_normalized_active_text(event)` | 이벤트의 `active_editor_text`를 공백 정규화하여 반환 |
| `_meaningful_text_change(prev, cur)` | 두 텍스트 사이에 "의미 있는" 변화가 있었는지 판정 (길이 증가량·diff 비율 기반) |
| `_is_same_idle_text(prev, cur)` | 두 텍스트가 사실상 동일한(idle) 상태인지 판정. `(안정여부, 유사도, 길이차)` 튜플 반환 |

시간 기반 cooldown은 베이스의 `_time_cooldown_status`를 그대로 사용(자체 메서드 없음).

**게이트 4종 (`evaluate` 내부)**

| 게이트 | 판정 | 점수 기여 |
|--------|------|-----------|
| `typing_pause` | `_typing_pause_status` 결과 — 작성 후 멈춤이 관찰됨 | +0.5 |
| `paragraph_cooldown` | `_passes_paragraph_cooldown` 결과 — 같은 문단 중복 개입 아님 (이벤트 기반 dedupe) | +0.3 |
| `time_cooldown` | `_time_cooldown_status`(베이스 헬퍼) — 직전 발동으로부터 `cooldown_min_seconds` 경과 | **없음** (prereq) |
| `substantial_paragraph` | `_has_substantial_paragraph`(베이스 헬퍼) — 현재 문단이 `min_paragraph_chars` 이상 | **없음** (prereq) |

(점수 범위 0.0~0.8 유지. 두 prereq 게이트는 blocker 누적에만 기여.)

---

## `WholeDocumentReviewScenario(ScenarioType)`

**시나리오**: 지속적인 대량 작성 후 멈춤 → 문서 전체 리뷰 보조.
- `name = "whole_document_review"`, `priority = "high"`, `initial_vruntime = -10.0`(더 먼저 선택됨), `vruntime_increment = 5.0`(연속 발동 시 CFS로 강하게 throttle), `review_char_limit = 6000`

| 메서드 | 역할 |
|--------|------|
| `__init__` | 임계값 파라미터 설정 (`sustained_window`, `sustained_min_added_chars`, `sustained_min_active_captures`, `idle_after_sustained_captures`, `idle_similarity_threshold`, `cooldown_min_seconds`, `cooldown_min_added_chars`) |
| `evaluate(context)` | `sustained_writing`, `idle_after_sustained`, `document_cooldown` 세 게이트를 평가 → score/reasons/blockers 누적 후 `ready` 판정 |
| `writing_context_overrides()` | `focus_scope`를 `"full_document"`로 설정하고, 리뷰용 텍스트를 `recent_sentences` / `full_document_excerpt`에 채움 |
| `tool_routing_hint_overrides()` | `tone="comprehensive_review"`, `preferred_action="review_whole_document"` 설정 |
| `_build_review_text(text)` | 리뷰 대상 텍스트를 만든다. `review_char_limit` 이하면 전체를, 초과하면 앞/뒤 발췌 + 중간 생략 안내 문구를 붙여 반환 |
| `_sustained_writing_status(events)` | 최근 윈도우 내에서 누적 추가 문자 수·활성 캡처 수가 임계 이상인지로 "지속적 작성" 여부 판정 |
| `_idle_after_sustained_status(events)` | 지속 작성 이후 텍스트가 안정적으로 멈춰 있는 캡처 수가 임계 이상인지 판정 |
| `_document_cooldown_status(*, last_fired_at, last_fired_doc_chars, current_chars)` | **시간 + 글자수 기반 cooldown.** `last_fired_at`(직전 발동 시각)과 `last_fired_doc_chars`(그 시점의 문서 길이)를 scheduler 상태에서 읽어, `elapsed >= cooldown_min_seconds` **그리고** `추가된 글자 수 >= cooldown_min_added_chars`일 때만 통과. history 윈도우 스캔이 아니므로 발동 기록이 회전으로 잊히지 않음 |
| `_normalized_active_text(event)` | 이벤트의 `active_editor_text`를 공백 정규화하여 반환 |

---

## `LongStaticReviewScenario(ScenarioType)`

**시나리오**: 편집기를 오래 켜둔 채 변화 없음(작성 완료 후 다시 읽는 중) → 오탈자 교정 + 이어쓸/추가할 내용 제안. 다른 두 시나리오와 **반대 방향** — "작성을 목격"하는 대신 "변화 없음을 목격"한다.
- `name = "long_static_review"`, `priority = "low"`, `initial_vruntime = 10.0`(높게 잡아 ready set 경쟁 시 CFS가 마지막에 선택), `vruntime_increment = 3.0`, `review_char_limit = 6000`

| 메서드 | 역할 |
|--------|------|
| `__init__` | 임계값 파라미터 설정 (`min_static_captures=3`, `min_document_chars=200`, `idle_similarity_threshold=0.99`, `cooldown_min_seconds=240.0`) |
| `evaluate(context)` | `prolonged_static`, `review_cooldown` 두 게이트를 평가 → score/reasons/blockers 누적 후 `ready` 판정. priority는 ready면 `medium`, 아니면 `low`. `review_cooldown` 게이트는 베이스의 `_time_cooldown_status`를 호출 |
| `writing_context_overrides()` | `focus_scope`를 `"full_document"`로 설정하고, 리뷰용 텍스트를 `recent_sentences` / `full_document_excerpt`에 채움 |
| `tool_routing_hint_overrides()` | `tone="proofreading_review"`, `preferred_action="review_whole_document"` 설정 |
| `_build_review_text(text)` | 리뷰 대상 텍스트를 만든다. `review_char_limit` 이하면 전체를, 초과하면 앞/뒤 발췌 + 중간 생략 안내 문구를 붙여 반환. 프롬프트는 "오랫동안 편집 없이 열어둠 → 교정·추가 제안" 맥락으로 구성 |
| `_prolonged_static_status(events)` | `active_editor_text`가 최근 `min_static_captures`개 연속 캡처에서 사실상 불변이고, 문서가 `min_document_chars` 이상인지 판정 |
| `_normalized_active_text(event)` | 이벤트의 `active_editor_text`를 공백 정규화하여 반환 |
| `_is_static_text(prev, cur)` | 두 텍스트가 사실상 정적(static)인지 판정. `(정적여부, 유사도)` 튜플 반환 |

시간 기반 cooldown은 베이스의 `_time_cooldown_status`를 그대로 사용(자체 메서드 없음). 이전엔 `_review_cooldown_status`라는 동일 로직의 자체 메서드가 있었으나 T1-1에서 베이스로 통합됨.

---

## `ParagraphChurnScenario(ScenarioType)`

**시나리오**: 같은 문단 안에서 작성·삭제를 반복하는 churn 상태 → 막힌 표현을 풀어주는 대안 제시.
- `name = "paragraph_churn"`, `priority = "medium"`, `initial_vruntime = 3.0`(명시 override — idle보다 약간 뒤), `vruntime_increment = 2.0`

| 메서드 | 역할 |
|--------|------|
| `__init__` | 임계값 파라미터 설정 (`churn_window=6`, `min_changed_captures=3`, `max_capture_delta=15`, `max_net_change=25`, `min_paragraph_chars=20`, `cooldown_min_seconds=150.0`) |
| `evaluate(context)` | `small_churn`, `churn_cooldown`, `substantial_paragraph` 세 게이트를 평가 → score/reasons/blockers 누적 후 `ready` 판정. `churn_cooldown`은 베이스의 `_time_cooldown_status` 호출 |
| `writing_context_overrides()` | `focus_scope`를 `"recent_writing"`으로 설정 |
| `tool_routing_hint_overrides()` | `tone="unstick"`, `preferred_action="revise_current_paragraph"` 설정 |
| `_small_churn_status(events)` | 최근 `churn_window`개 캡처 윈도우에서 같은 문단 내 작은 변화(`max_capture_delta` 이내)가 `min_changed_captures` 회 이상 관찰되고, 순변화량은 `max_net_change` 이내인지로 churn 판정 |
| `_normalized_active_text(event)` | 이벤트의 `active_editor_text`를 공백 정규화하여 반환 |

시간 기반 cooldown은 베이스의 `_time_cooldown_status`를 그대로 사용. 이전엔 `_churn_cooldown_status` 자체 메서드가 있었으나 T1-1에서 베이스로 통합됨.

**게이트 3종 (`evaluate` 내부)**

| 게이트 | 판정 | 점수 기여 |
|--------|------|-----------|
| `small_churn` | `_small_churn_status` 결과 — 작은 churn 패턴 관찰됨 | +0.5 |
| `churn_cooldown` | `_time_cooldown_status` — 직전 발동으로부터 `cooldown_min_seconds` 경과 | +0.3 |
| `substantial_paragraph` | `_has_substantial_paragraph` — 현재 문단이 `min_paragraph_chars` 이상 | **없음** (prereq) |

---

## `BlankDocumentStartScenario(ScenarioType)`

**시나리오**: 거의 빈 문서를 열고 머물러 있음 → 시작 방향/개요 제안.
- `name = "blank_document_start"`, `priority = "low"`, `initial_vruntime = 8.0`(명시 override — low 중에서도 더 뒤로), `vruntime_increment = 2.0`

| 메서드 | 역할 |
|--------|------|
| `__init__` | 임계값 파라미터 설정 (`max_document_chars=30`, `min_blank_captures=3`, `cooldown_min_seconds=600.0`) |
| `evaluate(context)` | `near_empty_document`, `start_cooldown` 두 게이트를 평가 → score/reasons/blockers 누적 후 `ready` 판정. `start_cooldown`은 베이스의 `_time_cooldown_status` 호출 |
| `writing_context_overrides()` | `focus_scope`를 `"full_document"`로 설정 |
| `tool_routing_hint_overrides()` | `tone="kickoff"`, `preferred_action="continue_writing"` 설정 |
| `_near_empty_status(events)` | 최근 `min_blank_captures`개 연속 캡처에서 문서 글자수가 `max_document_chars` 이하로 유지되는지 판정 |
| `_normalized_active_text(event)` | 이벤트의 `active_editor_text`를 공백 정규화하여 반환 |

시간 기반 cooldown은 베이스의 `_time_cooldown_status`를 그대로 사용. 이전엔 `_start_cooldown_status` 자체 메서드가 있었으나 T1-1에서 베이스로 통합됨.

**게이트 2종 (`evaluate` 내부)**

| 게이트 | 판정 | 점수 기여 |
|--------|------|-----------|
| `near_empty_document` | `_near_empty_status` 결과 — 빈/짧은 문서가 연속 캡처 동안 유지 | +0.5 |
| `start_cooldown` | `_time_cooldown_status` — 직전 발동으로부터 `cooldown_min_seconds` 경과 | +0.3 |

---

## cooldown / throttle 레이어

### cooldown 데이터 출처 (P1 수정 반영)
시간 기반 cooldown 게이트는 더 이상 `history_events`(최근 ~9개 캡처 ≈ 45초)를 스캔하지 않는다. `ScenarioScheduler`의 문서 단위 영속 상태에 기록된 `last_fired_at` / `last_fired_doc_chars`를 `ScenarioContext`로 받아 **시각을 직접 비교**한다. 덕분에 캡처 history 윈도우 크기와 무관하게 `cooldown_min_seconds`가 실제로 강제된다.

### T1-1 통합 — `_time_cooldown_status` 단일화
이전엔 시나리오마다 동일 로직의 cooldown 메서드(`_review_cooldown_status`, `_churn_cooldown_status`, `_start_cooldown_status`, idle의 `_time_cooldown_status`)를 각자 보유했음. T1-1에서 4개를 베이스의 `_time_cooldown_status`로 통합. 자체 메서드 0개, 모든 시간 cooldown은 `self._time_cooldown_status(context.last_fired_at)` 호출. `WholeDocumentReviewScenario`만 시간+글자수 결합 cooldown(`_document_cooldown_status`)이라 자체 메서드 유지.

### T1-4 전역 throttle (시나리오 무관 rate limit)
`ScenarioScheduler.min_global_fire_interval_sec` (default `10.0`초)이 시나리오 무관 전역 throttle을 제공. `select_and_charge` 진입부에서 `now - state.last_global_fire_at < min_global_fire_interval_sec`이면 발화 거부(반환값 `None`). 즉 시나리오별 cooldown과 별개로, 어떤 시나리오든 마지막 발화로부터 N초 안에는 또 안 뜸. 20+ 시나리오 환경에서 LLM 호출 폭주 방지의 안전망 역할. `0`으로 설정하면 비활성.

### vruntime 컨벤션
새 시나리오는 `priority`("high"/"medium"/"low")만 선언하면 `_PRIORITY_VRUNTIME_DEFAULTS`에서 `(initial_vruntime, vruntime_increment)`를 자동 도출. 자세한 컨벤션·사용 패턴은 `vruntime_convention.md` 참조.
