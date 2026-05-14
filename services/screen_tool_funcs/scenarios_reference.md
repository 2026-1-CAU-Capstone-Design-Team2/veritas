# `scenarios.py` 기능 정리

화면 개입(intervention) 시나리오를 정의하는 모듈. 각 시나리오는 자신의 게이트 함수, 우선순위, CFS 스케줄링 파라미터를 들고 있는 객체이며, `evaluate()`로 통일된 평가 결과를 반환한다.

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
- 필드: `window`, `filtered`, `history_events`, `same_document_events`, `document_key`, `paragraph_fingerprint`

### `ScenarioEvaluation`
시나리오별 평가 결과를 담는 통일 포맷.
- 필드: `name`, `ready`, `score`, `priority`, `reasons`, `blockers`, `gate_results`, `metadata`

---

## `ScenarioType` (추상 베이스 클래스)

모든 개입 시나리오의 추상 베이스. 게이트 함수, 우선순위, CFS 파라미터(`initial_vruntime`, `vruntime_increment`)를 보유한다.

| 메서드 | 역할 |
|--------|------|
| `__init__` | `initial_vruntime` / `vruntime_increment`를 외부에서 주입받아 클래스 기본값을 오버라이드 |
| `evaluate(context)` | **(추상)** 시나리오별 게이트를 실행하고 통일된 `ScenarioEvaluation`을 반환 |
| `writing_context_overrides()` | 이 시나리오가 발동될 때 `writing_context`에 병합할 부분 필드를 반환. 기본은 빈 dict(오버라이드 없음) |
| `tool_routing_hint_overrides()` | 이 시나리오가 발동될 때 `tool_routing_hint`에 병합할 부분 필드(`tone`, `preferred_action` 등)를 반환. 기본은 빈 dict |
| `_gate_result(passed, reason, extra)` | 게이트 결과를 `{passed, reason, ...extra}` 형태의 dict로 표준화하는 헬퍼 |

---

## `IdleAfterWritingScenario(ScenarioType)`

**시나리오**: 사용자가 글을 쓰다가 같은 문단에서 멈춤 → 부드러운 이어쓰기 제안.
- `name = "idle_after_writing"`, `priority = "medium"`, `initial_vruntime = 0.0`, `vruntime_increment = 1.0`

| 메서드 | 역할 |
|--------|------|
| `__init__` | 임계값 파라미터 설정 (`min_paragraph_chars`, `min_changed_chars`, `min_idle_captures`, `idle_similarity_threshold`, `cooldown_events`) |
| `evaluate(context)` | `typing_pause`와 `paragraph_cooldown` 두 게이트를 평가 → score/reasons/blockers 누적 후 `ready` 판정 |
| `writing_context_overrides()` | `focus_scope`를 `"recent_writing"`으로 설정 |
| `tool_routing_hint_overrides()` | `tone="gentle_continuation"` 설정. 리서치 필요 여부/문단 길이에 따라 `preferred_action`을 `continue_writing` / `provide_supporting_material` / `no_action` 중 선택 |
| `_typing_pause_status(events)` | 최근 캡처들이 안정적으로 멈춰 있는지 검사. 안정 캡처 수가 임계 이상이고, 멈추기 직전에 의미 있는 텍스트 변화가 있었는지로 `ready` 판정 |
| `_passes_paragraph_cooldown()` | 최근 `cooldown_events`개 이력 중 같은 문서·같은 문단 지문으로 이미 개입한 적이 있으면 차단(중복 방지) |
| `_normalized_active_text(event)` | 이벤트의 `active_editor_text`를 공백 정규화하여 반환 |
| `_meaningful_text_change(prev, cur)` | 두 텍스트 사이에 "의미 있는" 변화가 있었는지 판정 (길이 증가량·diff 비율 기반) |
| `_is_same_idle_text(prev, cur)` | 두 텍스트가 사실상 동일한(idle) 상태인지 판정. `(안정여부, 유사도, 길이차)` 튜플 반환 |

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
| `_document_cooldown_status()` | 같은 문서에 대한 직전 리뷰 개입 이후 충분한 시간 경과 + 충분한 문자 추가가 있었는지 검사. 경과 시간을 못 구하면 fail-closed(쿨다운 활성으로 간주) |
| `_normalized_active_text(event)` | 이벤트의 `active_editor_text`를 공백 정규화하여 반환 |
| `_elapsed_seconds(event)` | 이벤트의 `captured_at` ISO 타임스탬프를 파싱해 현재까지 경과 초를 반환. 파싱 실패 시 `None` |
