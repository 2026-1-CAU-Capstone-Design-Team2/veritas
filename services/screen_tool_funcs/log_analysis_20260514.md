# Screen Context 로그 분석 — 2026-05-14 16:32~16:33

메모장(`Notepad.exe`, 기아 관련 문서)에서 채팅 + screen context 모니터링이 돌아가는 세션. `long_static_review` 신규 시나리오와 `idle_after_writing`의 `substantial_paragraph` 게이트가 처음으로 실동작한 로그.

---

## 1. 타임라인

| 시각 | event | 문서 chars | 문단 | 결과 | 비고 |
|------|-------|-----------:|------|------|------|
| 16:32:01 | 163201 | 2349 | 295자 | **long_static_review QUEUED** (score 0.8) | 직전 캡처들이 static → 정상 발동. vruntime long_static→13.0. LLM 생성 시작 |
| 16:32:06 | 163206 | 2348 | 11자 "현재 우리나라 또한," | none | `substantial_paragraph=B` (11<20). long_static `review_cooldown=B` |
| 16:32:11 | 163211 | 2010 | 36자 | none | 사용자가 ~338자 삭제. `prolonged_static=B` |
| 16:32:16 | 163216 | 2010 | 36자 | **idle_after_writing QUEUED** (score 0.8, high) | 텍스트 안정 → 발동. vruntime idle→1.0 |
| 16:32:21~41 | (5캡처) | 2010 | 36자 | none | idle `paragraph_cooldown=B` (방금 발동), long_static `review_cooldown=B` |
| 16:32:46 | 163246 | 2010 | 36자 | **idle_after_writing QUEUED** (pending=2) | paragraph_cooldown 만료 → 재발동 |
| 16:32:51 | 163251 | 2010 | 36자 | **long_static_review QUEUED** (pending=3) | `review_cooldown=P` — **첫 발동 후 겨우 50초** |
| ~16:33:00 | — | — | — | assist 163201 완료 | **LLM 59.09초**. 답변이 이미 stale |
| 16:33:21 | 163321 | 2016 | 43자 | none | 사용자 재타이핑 "예시를 드렴" |
| ~16:33:24 | — | — | — | assist 163216 완료 | LLM 18.39초 |
| 16:33:26~36 | — | 2023~2024 | 50자 | none | "예시를 들면 다음과 같다" 입력 중 |
| 16:33:41 | 163341 | 2024 | 50자 | **long_static_review QUEUED** (pending=2) | **3번째 발동** — 2번째 후 또 ~50초 |
| ~16:33:44 | — | — | — | assist 163246 완료 | LLM 19.50초 |
| 16:33:46+ | — | 2029 | 55자 | none | "첫쨰," 입력 중 (로그 끝) |

---

## 2. 정상 동작 확인 (이번 변경분 검증됨)

- **`stable_paragraph` 공통 게이트 완화** — 16:32:06에서 `source=uia_selection_paragraph chars=11`인데도 `stable_paragraph=PASS`. 이전 같으면 BLOCK되어 전 시나리오가 막혔을 상황. 이제 통과하고, 문서 단위 시나리오도 평가받음. **의도대로 동작.**
- **`substantial_paragraph` 시나리오 게이트** — 16:32:06 문단 11자 → `substantial_paragraph=B`로 `idle_after_writing` 차단. 16:32:11+ 문단 36자 → `P`. **정상.**
- **`long_static_review` 발동** — 16:32:01에 직전 정적 구간을 잡아 정상 발동. 답변도 RAG([Document 001]) 인용하며 생성됨.
- **`idle_after_writing` 발동** — 16:32:16에 "더불어 경제적 충격에 대비한..." 문장에 이어쓰기 제안. 답변 on-topic.
- **CFS 산수** — vruntime decay/charge 계산 정확 (long_static 13.0→12.25→10.75→13.50, idle 0.0→1.0→0.75 …). 스케줄러 자체는 정상.

---

## 3. 발견된 문제

### P1 — `review_cooldown`이 사실상 작동 불가 (구조적 버그)

**증상**: `long_static_review`가 16:32:01 → 16:32:51 → 16:33:41, **약 50초 간격으로 반복 발동**. 설정값은 `cooldown_min_seconds=600.0`인데 50초마다 뚫림.

**근본 원인**:
- `screen_context_service.py:117` — `history_events = self.store.load_recent(history_window - 1)` → **최근 9개 이벤트만** 로드 (5초 주기 → 약 45초치).
- `LongStaticReviewScenario._review_cooldown_status`는 이 `history_events`를 뒤져 직전 `long_static_review` 개입을 찾음.
- 직전 개입 이벤트가 **9개 윈도우 밖으로 밀려나면** 게이트는 `no_prior_review` → `passed=True`를 반환.
- 즉 **쿨다운은 ~45초 윈도우 안에서만 강제 가능**. `cooldown_min_seconds`가 윈도우 길이(~45초)보다 크면 그 초과분은 전부 무효.

로그가 이를 그대로 증명: 첫 발동 후 9캡처(=45~50초)가 지나 이벤트가 윈도우에서 사라지자마자 16:32:51에 재발동.

**같은 잠복 버그**: `WholeDocumentReviewScenario._document_cooldown_status` (`cooldown_min_seconds=300.0`)도 동일 구조. 다만 `sustained_writing` 등 추가 게이트가 가려서 아직 증상이 안 드러났을 뿐.

**CFS도 thro틀 못 함**: long_static의 vruntime이 13→13.5→14로 오르긴 하나, ~50초 간격의 decay가 increment를 거의 상쇄. 게다가 ready set에서 경쟁 상대가 거의 없어(`idle`은 자체 cooldown, `whole`은 ready 안 됨) CFS가 long_static을 "지게" 만들 일이 없음. **결국 cooldown 게이트가 유일한 throttle인데 그게 깨져 있어 아무것도 throttle하지 못함.**

### P2 — LLM 레이턴시 >> 캡처 주기 → 큐 적체

- LLM 생성 시간: **59.09초** / 18.39초 / 19.50초. 캡처 주기는 5초.
- 개입이 5~50초마다 큐에 쌓이는데 소비는 건당 20~60초. `pending`이 1→2→3으로 증가.
- 첫 건의 59초 스파이크 동안 캡처 ~10회 + 개입 2건이 추가로 큐잉됨.

### P3 — stale 개입이 그대로 전달됨

- event 163201은 16:32:01 큐잉, 답변은 ~16:33:00 도착 (**59초 지연**).
- 그 사이 문서는 2349자 → 2010자로 바뀌고 사용자는 다른 문장을 쓰고 있었음.
- 163201 답변은 "지금 우리나라 또한,"(미완성 문장) 완성을 제안 — 사용자가 16:32:11쯤 이미 삭제하고 넘어간 내용. **전달 시점에 조언이 무의미.**
- README에 "stale/duplicate drop" 언급이 있으나, 이 케이스는 걸러지지 않고 소비·생성됨.

### P4 — (관찰) `long_static_review`가 전체 검토가 아닌 마지막 문장에 집중

- 163201은 `long_static_review`("문서 전체를 교정") 프롬프트였으나, 실제 답변은 미완성 마지막 문장 이어쓰기에 집중. `idle_after_writing`과 결과가 유사. 프롬프트/`_build_review_text` 설계 점검 필요할 수 있음.

---

## 4. 핵심 버그 상세 — P1

```
[screen_context_service.py:117]
history_events = self.store.load_recent(self.intervention_detector.history_window - 1)
                                        # = load_recent(9) → 최근 9개 ≈ 45초

[scenarios.py: LongStaticReviewScenario._review_cooldown_status]
for event in reversed(history_events):          # 9개만 순회
    ... intervention_type == "long_static_review" 인 이벤트 탐색
if last_review_event is None:
    return {"passed": True, "reason": "no_prior_review"}   # ← 윈도우 밖이면 여기로
```

**문제의 본질**: 쿨다운을 "최근 N개 이벤트 메모리"로 구현했는데, 그 메모리(~45초)가 `cooldown_min_seconds`(600초)보다 훨씬 짧다. 시간 기반 쿨다운을 윈도우 기반 자료구조 위에 올린 불일치.

**방향성 (구현 전 합의 필요)**:
- 쿨다운 판정을 `history_events` 윈도우가 아니라 **별도의 영속 상태**에서 읽어야 함. 후보:
  - `ScenarioScheduler`의 per-document 상태에 `last_fired_at[scenario_name]`을 같이 저장 (이미 디스크 영속·문서 단위라 자연스러움).
  - 또는 `store`가 시나리오별 마지막 개입 시각을 따로 보관.
- `WholeDocumentReviewScenario`도 같은 방식으로 함께 고쳐야 함 (동일 잠복 버그).

---

## 5. 우선순위 요약

| 순위 | 문제 | 영향 | 성격 |
|------|------|------|------|
| P1 | `review_cooldown` 윈도우 한계로 무력화 | `long_static_review` ~50초마다 반복 발동, 노이즈 | 구조적 버그 — 수정 필요 |
| P2 | LLM 레이턴시 ≫ 캡처 주기 | 큐 적체, 응답 지연 누적 | 성능/아키텍처 |
| P3 | stale 개입 전달 | 사용자가 이미 지나간 내용에 조언 | P2의 결과 — 소비 시 staleness 검사 필요 |
| P4 | long_static 답변이 문장 단위에 집중 | 시나리오 의도와 결과 불일치 | 프롬프트 설계 |

P1이 이번 변경(`long_static_review` 추가)이 직접 노출시킨 버그이므로 먼저 다룰 대상. P2/P3는 그 다음 별도 논의.
