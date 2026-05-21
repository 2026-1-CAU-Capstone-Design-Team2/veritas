# 시나리오별 스크린 개입 프롬프트 구조

`services/screen_tool_funcs`가 현재 지원하는 **23개 시나리오**가 각각 어떤 조건에서 발동하고, 발동 시 LLM 프롬프트를 어떻게 변형하는지 정리한 레퍼런스.

- 코드 출처: [`scenario/`](scenario/) 패키지, [`intervention_dispatcher.py`](intervention_dispatcher.py), [`screen_context_service.py`](screen_context_service.py), [`scenario_scheduler.py`](scenario_scheduler.py), [`../../core/prompts/chat.py`](../../core/prompts/chat.py), [`../../agent/chat_agent.py`](../../agent/chat_agent.py)
- 관련 문서: [`scenarios_reference.md`](scenarios_reference.md) (코드 API 레퍼런스, writing_flow 5개 한정), [`scenario_prompts_design.md`](scenario_prompts_design.md) (산문 guidance 설계 — **구현 완료**), [`vruntime_convention.md`](vruntime_convention.md)

---

## 1. "프롬프트 구조"란 — 캡처에서 LLM 프롬프트까지

```
폴링 캡처(5초)                screen_context_service.capture_once()
  │
  ▼ 공통 게이트 통과분만        intervention_detector.decide()
  │   (editing_app · dwell · stable_paragraph)
  ▼ 시나리오별 evaluate()       각 ScenarioType.evaluate() → ScenarioEvaluation(ready/score/gates)
  │   ready 후보 중 CFS 선택     scenario_scheduler.select_and_charge()
  ▼ 선택된 시나리오 1개          intervention.intervention_type = <name>
  │
  ▼ 페이로드 조립               intervention_dispatcher._build_payload()
  │   ├─ writing_context        ← scenario.writing_context_overrides()
  │   └─ tool_routing_hint      ← scenario.tool_routing_hint_overrides()
  ▼ 디스크 큐                   store.enqueue_intervention() → intervention_queue.json
  │
  ▼ 소비 (ChatAgent 별도 스레드) answer_screen_intervention()
      ├─ intervention_type → SCREEN_SCENARIO_GUIDANCE 조회 (산문)
      └─ SCREEN_INTERVENTION_USER_PROMPT_TEMPLATE.format(...) → LLM
```

시나리오가 프롬프트에 영향을 주는 **세 갈래**:

1. **`writing_context.focus_scope`** — LLM에게 어떤 텍스트 범위를 보여줄지 (`recent_writing` vs `full_document`). ← `writing_context_overrides()`
2. **`tool_routing_hint.tone` / `preferred_action`** — 어떤 톤으로 어떤 행동을 하라는 enum 힌트. ← `tool_routing_hint_overrides()`
3. **`{scenario_guidance}` 산문** — 소비부(`answer_screen_intervention`)가 `intervention_type`으로 `SCREEN_SCENARIO_GUIDANCE`를 조회해 주입하는 **2~5문장 설명**. (§5)

> ℹ️ **시나리오별 산문 guidance (구현 완료)**: 위 1·2의 enum에 더해, 각 시나리오는 **2~5문장 산문 guidance**가 `{scenario_guidance}` 슬롯으로 함께 전달된다 — `tone="unstick"`이 *어떤 상황이고 어떻게 답해야 하는지*를 LLM이 같이 읽는다. [`scenario_prompts_design.md`](scenario_prompts_design.md)의 (b)안이 **23개 시나리오 전부**에 대해 구현됨 ([`core/prompts/chat.py`](../../core/prompts/chat.py)의 `SCREEN_SCENARIO_GUIDANCE`). → §5.

---

## 2. 공통 프롬프트 골격

### 2-1. 유저 프롬프트 템플릿 — `SCREEN_INTERVENTION_USER_PROMPT_TEMPLATE`

[`core/prompts/chat.py:181`](../../core/prompts/chat.py). 슬롯 6개. 시나리오 무관하게 동일하며, 시나리오 차이는 `{writing_context}` / `{routing_hint}`의 값 + `{scenario_guidance}` 산문으로 들어간다.

| 슬롯 | 채워지는 내용 |
|------|---------------|
| `{history}` | 최근 채팅 히스토리 |
| `{app_context}` | 활성 윈도우 (process, title, app_type, document_key) |
| `{writing_context}` | 작성 컨텍스트 (아래 2-2) — **focus_scope가 여기 들어감** |
| `{routing_hint}` | 라우팅 힌트 (아래 2-3) — **tone / preferred_action이 여기 들어감** |
| `{scenario_guidance}` | 선택된 시나리오의 2~5문장 산문 설명 (`SCREEN_SCENARIO_GUIDANCE`) — §5 |
| `{knowledge_context}` | RAG 지식베이스 스니펫 |

시스템 프롬프트(`SCREEN_INTERVENTION_SYSTEM_PROMPT`, [chat.py:162](../../core/prompts/chat.py))는 전 시나리오 공유 — 언어 정책, 인용 형식, "짧게 답하라", 내부 구현 언급 금지 등 공통 규칙만 담는다.

### 2-2. `writing_context` 기본형 (`_writing_context_for_type`, [dispatcher:117](intervention_dispatcher.py))

```jsonc
{
  "full_text": "<active_editor_text 전체>",
  "full_text_chars": 1234,
  "current_paragraph": "<현재 문단>",
  "recent_sentences": "<최근 1~2문장>",
  "focused_sentence": "<변경 지점 문장>",
  "paragraph_source": "app_text|ui_automation|ocr",
  "changed_text": "<직전 캡처 대비 변경분>",
  "confidence": 0.0,
  "focus_scope": "recent_writing"   // ← 시나리오가 override (recent_writing | full_document)
}
```

`focus_scope`만 시나리오가 바꾼다. `full_document` 시나리오(whole_document_review, long_static_review 등)는 추가로 `recent_sentences` / `full_document_excerpt`에 리뷰용 전체 텍스트(최대 6000자, 초과 시 앞/뒤 발췌)를 채운다.

### 2-3. `tool_routing_hint` 기본형 (`_tool_routing_hint`, [dispatcher:229](intervention_dispatcher.py))

```jsonc
{
  "intervention_type": "<name>",
  "tone": "neutral",                 // ← 시나리오가 override
  "allowed_actions": [
    "continue_writing", "provide_supporting_material", "search_sources",
    "revise_current_paragraph", "review_whole_document", "no_action"
  ],
  "preferred_action": "no_action",   // ← 시나리오가 override
  "signals": {
    "research_needed": false,        // 현재 문단에 근거/자료/출처/통계… 마커 있으면 true
    "has_recent_change": false,
    "has_focused_sentence": false
  }
}
```

> ⚠️ **enum 불일치**: `allowed_actions`는 6개로 고정인데, Phase 4 시나리오(structure/markers/text_quality/edit_diff)는 `expand_outline_item`, `request_citation`, `offer_backup` 등 **목록에 없는 `preferred_action` 값**을 쓴다. 산문 guidance(§5)가 의미를 보충하지만, enum 자체의 정합성은 별개 문제다. → §6 개선 포인트.

### 2-4. 공통 게이트 (전 시나리오 prerequisite)

`evaluate()`는 아래 3개 공통 게이트를 통과한 캡처에 대해서만 호출된다 (`intervention_detector`).

| 공통 게이트 | 의미 |
|-------------|------|
| `editing_app` | foreground 앱이 텍스트 편집 대상 (notepad/word/hwp/code/notion…) |
| `dwell` | 같은 창에 충분히 머무름 (history/dwell_ratio 임계) |
| `stable_paragraph` | 캡처 신뢰성 확보 (소스 품질·confidence) |

이후 시나리오별 게이트 + 시나리오 무관 **전역 throttle**(`min_global_fire_interval_sec`, 기본 10초)까지 통과해야 실제 발화. CFS 스케줄러가 ready 후보 중 vruntime 최소값 1개를 선택.

---

## 3. 시나리오 → 프롬프트 매핑 요약 (전 23개)

`focus_scope` / `tone` / `preferred_action`이 곧 "프롬프트 변형"의 핵심이고, 여기에 시나리오별 산문 guidance(§5)가 더해진다. `init_vrt`=initial_vruntime, `incr`=vruntime_increment (CFS 스케줄링 가중치, 낮을수록 먼저 선택).

### 3-1. writing_flow — 작성 흐름 (5개, [writing_flow.py](scenario/writing_flow.py))

| name | focus_scope | tone | preferred_action | priority | init_vrt / incr | cooldown |
|------|-------------|------|------------------|----------|-----------------|----------|
| `idle_after_writing` | recent_writing | gentle_continuation | continue_writing¹ | medium | 0.0 / 1.0 | 60s |
| `whole_document_review` | full_document | comprehensive_review | review_whole_document | high | -10.0 / 5.0 | 300s² |
| `long_static_review` | full_document | proofreading_review | review_whole_document | low | 10.0 / 3.0 | 240s |
| `paragraph_churn` | recent_writing | unstick | revise_current_paragraph | medium | 3.0 / 2.0 | 150s |
| `blank_document_start` | full_document | kickoff | continue_writing | low | 8.0 / 2.0 | 600s |

¹ 동적: `research_needed`면 `provide_supporting_material`, 문단 짧고 focus 없으면 `no_action`.
² 시간(300s) **AND** 직전 발동 이후 +200자 추가 둘 다 충족해야 통과 (시간+글자수 결합 cooldown).

### 3-2. structure — 문서 구조 (5개, [structure.py](scenario/structure.py))

| name | focus_scope | tone | preferred_action | priority | init_vrt / incr | cooldown |
|------|-------------|------|------------------|----------|-----------------|----------|
| `outline_phase` | full_document | outline_expand | expand_outline_item | medium | 0.0 / 2.0 | 180s |
| `heading_added` | recent_writing | section_kickoff | open_section | medium | 0.0 / 2.0 | 240s |
| `long_paragraph_written` | recent_writing | structure_split | suggest_paragraph_break | medium | 0.0 / 2.0 | 240s |
| `numbered_list_growth` | full_document | list_extend | suggest_list_item | medium | 0.0 / 2.0 | 180s |
| `code_block_present` | full_document | code_review | comment_on_code | medium | 0.0 / 2.0 | 300s |

### 3-3. markers — 단순 마커 (3개, [markers.py](scenario/markers.py))

| name | focus_scope | tone | preferred_action | priority | init_vrt / incr | cooldown |
|------|-------------|------|------------------|----------|-----------------|----------|
| `acronym_introduced` | recent_writing | clarify | suggest_definition | medium | 0.0 / 2.0 | 300s |
| `todo_marker_present` | full_document | task_summary | summarize_todos | low | 5.0 / 2.0 | 600s |
| `many_question_marks` | recent_writing | research_focus | highlight_key_questions | medium | 0.0 / 2.0 | 240s |

### 3-4. text_quality — 텍스트 품질 (6개, [text_quality.py](scenario/text_quality.py))

| name | focus_scope | tone | preferred_action | priority | init_vrt / incr | cooldown |
|------|-------------|------|------------------|----------|-----------------|----------|
| `quote_inserted` | recent_writing | attribution_check | suggest_attribution | medium | 0.0 / 2.0 | 300s |
| `citation_missing` | full_document | evidence_check | request_citation | medium | 0.0 / 2.0 | 300s |
| `factual_claim_made` | recent_writing | verify | verify_claim | medium | 0.0 / 2.0 | 240s |
| `repeated_phrase_in_paragraph` | recent_writing | rephrase | suggest_alternative_wording | medium | 0.0 / 2.0 | 180s |
| `transition_word_overuse` | recent_writing | smooth_flow | reduce_transitions | medium | 0.0 / 2.0 | 300s |
| `weak_modifier_overuse` | recent_writing | tighten | concretize_modifiers | medium | 0.0 / 2.0 | 300s |

### 3-5. edit_diff — 캡처간 편집 변화 (4개, [edit_diff.py](scenario/edit_diff.py))

| name | focus_scope | tone | preferred_action | priority | init_vrt / incr | cooldown |
|------|-------------|------|------------------|----------|-----------------|----------|
| `scattered_edits` | full_document | consistency | consistency_pass | medium | 0.0 / 2.0 | 300s |
| `large_deletion` | recent_writing | backup | offer_backup | medium | 0.0 / 2.0 | 180s |
| `copy_paste_growth` | recent_writing | integrate | integrate_pasted_content | medium | 0.0 / 2.0 | 240s |
| `undo_cycle_detected` | recent_writing | settle | resolve_undo_cycle | medium | 0.0 / 2.0 | 240s |

> vruntime 규칙: `priority`만 선언하면 `_PRIORITY_VRUNTIME_DEFAULTS`(high `-5/3`, medium `0/2`, low `5/2`)에서 자동 도출. writing_flow 5개만 클래스 attribute로 명시 override (자주/드물게 발동 조정). 나머지 18개는 전부 default. 자세한 내용은 [vruntime_convention.md](vruntime_convention.md).

---

## 4. 카테고리별 트리거 게이트 상세

각 시나리오의 `evaluate()`가 검사하는 게이트와 임계값. 모든 시나리오는 시간 cooldown 게이트(`_time_cooldown_status`, scheduler의 `last_fired_at` 기반)를 공유 prerequisite로 가진다 — 아래 표에서는 시나리오 고유 게이트만 명시.

### 4-1. writing_flow (캡처간 텍스트 비교 기반)

| name | 고유 게이트 | 발동 조건(기본 임계값) | 점수 |
|------|-------------|------------------------|------|
| `idle_after_writing` | `typing_pause` + `substantial_paragraph` | 안정 캡처 ≥2 & 직전에 의미있는 변화(≥10자) & 현재 문단 ≥20자 & idle 유사도 ≥0.985 | 0.8 |
| `whole_document_review` | `sustained_writing` + `idle_after_sustained` + `document_cooldown` | window 8캡처에서 누적 추가 ≥300자 & 활성 캡처 ≥4 → 이후 안정 캡처 ≥2 (sim ≥0.97) | 0.9 |
| `long_static_review` | `prolonged_static` | 연속 정적 캡처 ≥3 & 문서 ≥200자 & 유사도 ≥0.99 (편집 없이 읽는 중) | 0.8 |
| `paragraph_churn` | `small_churn` + `substantial_paragraph` | window 6에서 변경 캡처 ≥3 & 캡처당 변화 ≤15자 & 순변화 ≤25자 & 문단 ≥20자 | 0.8 |
| `blank_document_start` | `near_empty_document` | 연속 ≥3 캡처가 모두 ≤30자 | 0.8 |

세 "정적/작성" 시나리오는 서로 배타적 동작: idle은 "멈춤", whole_document는 "대량작성 후 멈춤", long_static은 "변화 전무"를 노린다.

### 4-2. structure (현재 캡처 텍스트 정규식 매칭, 캡처간 비교 없음)

| name | 고유 게이트 | 발동 조건(기본 임계값) | 점수 |
|------|-------------|------------------------|------|
| `outline_phase` | `outline_shape` | 줄 ≥5 & 평균 줄 길이 ≤60자 & 짧은 줄 비율 ≥0.5 (개요 모양) | 0.5 |
| `heading_added` | `heading_present` | 헤딩 마커 존재 (`#`, `1.`, `제1장/1절` 등, 최대 3개) | 0.5 |
| `long_paragraph_written` | `long_paragraph` | 현재 문단 ≥500자 | 0.5 |
| `numbered_list_growth` | `numbered_list` | 번호 리스트 항목 ≥3개 (`1.`, `(1)`, `①`, `가.` 등) | 0.5 |
| `code_block_present` | `code_block` | 코드 fence(` ``` `) ≥1 | 0.5 |

### 4-3. markers (단순 카운트/매칭)

| name | 고유 게이트 | 발동 조건(기본 임계값) | 점수 |
|------|-------------|------------------------|------|
| `acronym_introduced` | `acronym_present` | 대문자 약어(3-5자) 존재, 최대 5개 (한국어 조사 뒤도 인식) | 0.5 |
| `todo_marker_present` | `todo_marker` | `TODO/FIXME/XXX/HACK/TBD/NOTE` 또는 `[?]`, `[보강/확인/수정/추가/미정]` 존재 | 0.5 |
| `many_question_marks` | `many_questions` | 현재 문단 내 `?` ≥3개 | 0.5 |

### 4-4. text_quality (정규식 + 닫힌 한국어 사전)

| name | 고유 게이트 | 발동 조건(기본 임계값) | 점수 |
|------|-------------|------------------------|------|
| `quote_inserted` | `quote_present` | 큰따옴표/「」/『』 안 20자+ 인용 존재 | 0.5 |
| `citation_missing` | `citation_missing` | 통계/년도 ≥2개 **AND** 인용 마커(`[1]`, `(저자, 2023)` 등) 0개 | 0.5 |
| `factual_claim_made` | `factual_claim` | 통계/년도 패턴 ≥1개 (`30%`, `2023년`, `5만 명` 등) | 0.5 |
| `repeated_phrase_in_paragraph` | `repeated_phrase` | 문단 단어 ≥20 & 같은 2-gram 반복 ≥3회 | 0.5 |
| `transition_word_overuse` | `transition_overuse` | 접속어(`그러나/하지만/또한/따라서`…) ≥4회 | 0.5 |
| `weak_modifier_overuse` | `weak_modifier_overuse` | 약한 강조어(`매우/정말/아주/굉장히`…) ≥4회 | 0.5 |

> 사전(`KO_TRANSITION_WORDS`, `KO_WEAK_MODIFIERS`)은 [`scenario/_shared.py`](scenario/_shared.py)에 닫힌 어휘 집합으로 정의. 도메인별 jargon은 의도적으로 하드코딩하지 않음.

### 4-5. edit_diff (`same_document_events` 캡처간 비교 필수)

| name | 고유 게이트 | 발동 조건(기본 임계값) | 점수 |
|------|-------------|------------------------|------|
| `scattered_edits` | `scattered_edits` | window 5에서 변경 캡처 ≥3 (0<변화≤30자) & 서로 다른 문단 지문 ≥2 | 0.5 |
| `large_deletion` | `large_deletion` | 직전 캡처 대비 ≥100자 삭제 | 0.5 |
| `copy_paste_growth` | `copy_paste_growth` | 직전 캡처 대비 ≥200자 추가 (paste 의심) | 0.5 |
| `undo_cycle_detected` | `undo_cycle` | 최근 3캡처가 A→B→A 진동 (sim(A,C) ≥0.98, sim(A,B)·sim(B,C) <0.98) | 0.5 |

---

## 5. 시나리오별 산문 guidance 층 (구현 완료)

[`scenario_prompts_design.md`](scenario_prompts_design.md)가 지적한 gap — enum(`tone`/`preferred_action`)만으로는 LLM이 상황을 이해 못 함 — 을 메우는 산문층이 구현되어 있다. 4개 구성요소 모두 존재:

| 구성요소 | 위치 | 상태 |
|----------|------|------|
| `SCREEN_SCENARIO_GUIDANCE` dict (23개) + `_DEFAULT` | [`core/prompts/chat.py:210`](../../core/prompts/chat.py) | ✅ |
| `core.prompts` 재노출 | [`core/prompts/__init__.py`](../../core/prompts/__init__.py) | ✅ |
| `{scenario_guidance}` 슬롯 | `SCREEN_INTERVENTION_USER_PROMPT_TEMPLATE` ([chat.py:193](../../core/prompts/chat.py)) | ✅ |
| 소비부 배선 | [`chat_agent.answer_screen_intervention`](../../agent/chat_agent.py) (`.get` + `.format(scenario_guidance=...)`) | ✅ |

**동작**: `answer_screen_intervention`이 `intervention_type`을 `SCREEN_SCENARIO_GUIDANCE`에서 찾아(없거나 `none`이면 `SCREEN_SCENARIO_GUIDANCE_DEFAULT`) 템플릿의 `{scenario_guidance}` 슬롯에 주입. 그래서 `tone="unstick"` enum 옆에 *"사용자가 같은 문단을 고치며 막혀 있으니, 새 내용 말고 기존 논지 안에서 대안 표현 1~2개를 제시하라"* 같은 산문이 함께 LLM에 들어간다.

예시 (`paragraph_churn`):
> The user has been writing and deleting within the same paragraph; they are stuck on phrasing. Offer 1-2 concrete rewrites of the current paragraph… Stay strictly within the user's existing argument and concepts; do not introduce new ideas… The goal is to unstick their phrasing, not to expand the argument.

> 📌 **이력 메모**: 한때 잘못된 merge로 `SCREEN_SCENARIO_GUIDANCE` 정의부(+슬롯)가 유실돼 소비부만 남아 `ImportError`로 런타임이 크래시한 적이 있다(`core/prompts.py` → `core/prompts/` 리팩터(P2-4)와 겹친 회귀). `fix: screen_recording 프롬프트 복구`(72016f1) 등에서 23개 문안 전부 복구됨.

---

## 6. 정리 — 현재 구조의 특징과 개선 포인트

**구조 요약**
- 시나리오 = (트리거 게이트 묶음) + (focus_scope) + (tone, preferred_action) + (산문 guidance) + (CFS 가중치).
- 프롬프트 변형은 enum 2종(focus_scope, tone/preferred_action) + 산문 guidance로 표현 → 템플릿 1벌 유지, blast radius 최소.
- 23개 모두 [`screen_context_service.py:92`](screen_context_service.py) 한 곳에서 인스턴스화·등록되어 detector/scheduler/dispatcher에 공유 주입.

**개선 포인트 (영향 큰 순)**
1. ~~산문 guidance 미구현~~ → **구현 완료** (§5). 23개 시나리오 전부 산문 문안 + `{scenario_guidance}` 슬롯 배선됨. (남은 일: 실로그로 시나리오별 응답 톤이 문안 의도와 맞는지 검증)
2. **`allowed_actions` enum 불일치** (§2-3) — Phase 4 시나리오의 `preferred_action`(18종 신규)이 base 6종 목록에 없음. 목록 확장 또는 매핑 정의 필요.
3. **점수 평탄화** — Phase 4 시나리오 18개가 전부 priority=medium, score 0.5로 동일 → ready set 충돌 시 변별이 vruntime(거의 다 0.0/2.0)에만 의존. 우선순위 차등화 여지.
