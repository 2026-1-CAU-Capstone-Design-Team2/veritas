# Veritas Proactive (Rule-Based) — 구현 정리

> 사용자가 native editor / external Windows 문서 앱에서 작성 중일 때,
> "언제 / 무엇을 / 어떻게 도울지"를 결정하는 rule-based 시스템.

원본 지시서:
[`veritas_proactive_rule_based_reimplementation.md`](veritas_proactive_rule_based_reimplementation.md)
이전 bandit 구현의 정리: [`PROACTIVE_BANDIT_LEGACY.md`](PROACTIVE_BANDIT_LEGACY.md) (history only)
관련 코드 루트: [`services/proactive/`](services/proactive/), [`api/api_routes/proactive.py`](api/api_routes/proactive.py)

---

## 1. 큰 그림

```
사용자 작성 surface (native editor / external screen)
        │
        ▼
ProactiveOrchestrator.observe(observation)
        │
        ├─ Primitive signals (idle, churn, evidence-need, ...)
        ├─ ActiveAnchor 추출  (cursor/selection/sentence/paragraph/section, confidence)
        ├─ CandidateFactory   → 0~3 anchor-local ProactiveTask 후보 (LLM 호출 없음)
        ├─ RuleEvaluator      → 하드 게이트 + 0..1 rubric score (deterministic)
        ├─ Threshold 비교     → score ≥ adjusted_threshold ? Task : NullPrediction
        └─ Decision 로깅 (raw text 미저장) + 캐시 + timeout/null-outcome 등록
        ▼
generate/stream(decision_id)   ←  task일 때만 LLM 한 번 호출
        ▼
Surface renderer (native ghost / external card / inline-diff)
        ▼ user clicks
ProactiveOrchestrator.record_feedback(decision_id, raw_action)
        │
        ├─ Canonical 매핑 (TAB→accept, ESC→reject, 다시→retry, 위치다름→wrong_anchor, ...)
        ├─ UserAdaptationMemory (EMA + cooldown + suppression + threshold_offset)
        ├─ 로그 (feedback.jsonl / updates.jsonl)
        └─ Pending timeout 해제
```

핵심 commitments:
- **모든 task는 ActiveAnchor에 묶임** — 결론 작성 중에 서론을 비평하는 일은 *구조적으로* 불가능
- **LLM은 task가 선택된 후 단 한 번만 호출** — 후보 비교용 generation 금지
- **policy_state.json (학습 모델) 없음** — 학습은 user_adaptation.json의 threshold/cooldown
- **raw text는 in-memory cache에만** — JSONL/JSON state에는 절대 안 들어감

---

## 2. 핵심 데이터 모델

### 2.1 ActiveAnchor — [`anchors.py`](services/proactive/anchors.py)

```python
@dataclass
class ActiveAnchor:
    document_id: str
    surface: Literal["native_editor", "external_app"]
    cursor_index | selection_start/_end
    sentence_text | paragraph_text | section_heading
    prev_sentence | next_sentence | prev_paragraph | next_paragraph
    anchor_id: str        # stable hash of (doc_id, cursor-bucket, paragraph-hash)
    confidence: float     # 0..1 in spec §3.2 bands
    source: AnchorSource  # native_cursor | uia_caret | ocr_visible_text | ...
```

Confidence 밴드:
- native cursor/selection → 0.90~1.00
- UIA caret/selection → 0.75~0.95
- OCR visible text → 0.20~0.65 (capped)

`MIN_CONFIDENCE_FOR_ACTIVE_SUGGESTION = 0.45` 이하면 어떤 active task도 emit 불가 → NullPrediction.

`anchor_id`는 cursor를 80자 bucket으로 묶고 paragraph hash와 결합 — 사용자가 키 몇 번 쳐도 같은 anchor에 머무름. **`anchor_id|task_type`** 단위로 cooldown이 걸린다.

### 2.2 ProactiveTask — [`proposal_models.py`](services/proactive/proposal_models.py)

```python
TaskType = Literal[
    "next_sentence",
    "paragraph_rewrite",
    "local_copyedit",
    "logic_flow_review",
    "evidence_or_citation_prompt",
    "recovery_or_integration_note",
    "long_paragraph_split",
]

ContextScope = Literal[
    "cursor_previous_sentences",
    "current_sentence",
    "current_paragraph",
    "current_and_previous_paragraph",
    "current_prev_next_paragraphs",
    "claim_window",
    "anchor_diff_region",
    "section_local_excerpt",
]

@dataclass
class ProactiveTask:
    task_type: TaskType
    target_anchor_id: str       # 반드시 ActiveAnchor.anchor_id와 동일
    context_scope: ContextScope
    render_mode: RenderMode
    confidence: float           # CandidateFactory의 a-priori
    evaluator_score: float      # RuleEvaluator의 0..1 rubric

@dataclass
class NullPrediction:
    reason: str
    gate_reasons: list[str]
    evaluator_score: float
    candidate_count: int

Prediction = ProactiveTask | NullPrediction
```

ContextScope **전부 anchor-relative** — `full_document` / `previous_section` 같은 scope은 *존재하지 않음*.

### 2.3 SurfaceCapabilities

```python
SurfaceCapabilities.for_native(inline_diff=False, inline_marker=False)
SurfaceCapabilities.for_external()
```

CandidateFactory가 task를 emit 전에 surface가 그 render mode를 지원하는지 확인. native_inline_diff renderer가 없으면 paragraph_rewrite는 native에서 후보로도 나오지 않음.

---

## 3. Pipeline 단계별

### 3.1 CandidateFactory — [`candidates.py`](services/proactive/candidates.py)

Pure deterministic. 각 task type마다 명시적 gate:

| Task | Gate (요약) |
|---|---|
| `next_sentence` | confidence ≥ 0.55, idle ≥ 2s, paragraph 끝/중간 |
| `paragraph_rewrite` | confidence ≥ 0.60, paragraph ≥ 80자, churn ≥ 0.30 또는 undo/paste |
| `local_copyedit` | confidence ≥ 0.60, 반복 표현/runon 감지 |
| `logic_flow_review` | confidence ≥ 0.65, 이웃 paragraph 존재, 비활성 typing |
| `evidence_or_citation_prompt` | confidence ≥ 0.60, 숫자/연도/% 또는 근거 키워드 감지 |
| `recovery_or_integration_note` | recent diff overlap, paste/undo/large delete |
| `long_paragraph_split` | paragraph ≥ 500자, idle |

최대 3개 후보. **모든 후보 `target_anchor_id == anchor.anchor_id`** — 다른 위치 target은 만들 수 없음.

### 3.2 RuleEvaluator — [`evaluator.py`](services/proactive/evaluator.py)

**Hard gates** (`check_hard_gates`):
- `anchor_missing` / `anchor_confidence_too_low`
- `off_anchor_target` (defensive, factory가 이미 anchor-bound이지만 한 번 더 검증)
- `surface_render_unsupported`
- `context_insufficient`
- `active_typing_not_stable`
- `cooldown_same_anchor_task`
- `same_task_recently_rejected`
- `recent_negative_streak`
- `no_relevant_source_for_strong_evidence_task`

게이트가 하나라도 fail 하면 점수 계산 없이 후보 폐기.

**Rubric score** (spec §6.2):

```
score = + 0.30 · anchor_confidence
        + 0.20 · need_signal           # idle/churn/evidence
        + 0.20 · context_sufficiency   # 필요한 부분이 anchor에 있는지
        + 0.15 · task_fit              # task ↔ situation 매칭
        + 0.10 · source_support
        − 0.20 · interruption_risk
        − 0.15 · recent_negative_rate
```

`ScoreBreakdown` dataclass가 각 항의 contribution을 보존 → /explain endpoint에서 그대로 노출.

**Adjusted threshold** (`adjusted_threshold`):

```
threshold = BASE_SHOW_THRESHOLD (0.62)
          + state.threshold_offset      # accept↓ / reject↑
          + 0.15 · recent_negative_rate
```

cooldown 또는 suppression 활성 시 `+inf` 반환 → 그 task는 영원히 임계값 못 넘김 (현재 cooldown 끝날 때까지).

### 3.3 UserAdaptationMemory — [`adaptation.py`](services/proactive/adaptation.py)

학습된 ML 파라미터 없음. 다음만 누적:

```python
@dataclass
class UserAdaptationState:
    global_stats: FeedbackStats          # accept/reject/retry/timeout EMA
    task_type_stats: dict[str, TaskTypeStats]
        # accept/reject/retry/timeout/wrong_anchor 카운트
        # recent_reject_iso: 윈도우 안 reject 타임스탬프
        # suppressed_until: 3회 reject 후 10분 suppression
    anchor_cooldowns: dict[str, AnchorCooldown]
        # "{anchor_id}|{task_type}" → cooldown_until ISO
    threshold_offset: float               # clamp [-0.10, +0.20]
    prompt_style_flags: dict              # retry 누적 시 prefer_shorter 등
```

**Feedback rule asymmetry** (spec §7.1):

| Canonical | EMA | threshold_offset | anchor cooldown | task_type stats |
|---|---|---|---|---|
| accept | +1 to accept | −0.015 | cleared if matches | +1 accept |
| reject | +1 to reject | +0.030 | set 180s | +1 reject (3회면 suppress 10분) |
| retry | +1 to retry | 0 | — | +1 retry, **task 패널티 없음** |
| timeout | +1 to timeout | +0.010 | set 60s | +1 timeout |
| wrong_anchor | recent_neg ↑ | +0.005 | **set 안 함** | +1 wrong_anchor (reject 안 봄) |

핵심: **wrong_anchor와 retry는 task_type을 패널티하지 않음** — 전자는 anchor 추출이 잘못된 거, 후자는 timing은 맞고 content만 다시 쓰면 되니까.

원자적 JSON 영구화 — [`adaptation.UserAdaptationMemory._atomic_write_json`](services/proactive/adaptation.py).

### 3.4 ContextBundle — [`context_selector.py`](services/proactive/context_selector.py)

scope 별로 anchor의 어느 slice를 사용할지 결정. 절대로 anchor 이웃을 넘어가지 않음.

```
cursor_previous_sentences        → prev_sentence + current_fragment
current_sentence                 → sentence_text (+ paragraph_text 보조)
current_paragraph                → paragraph_text only
current_and_previous_paragraph   → prev_paragraph + paragraph_text + section_heading
current_prev_next_paragraphs     → prev + current + next + section_heading
claim_window                     → 현재 문장 ±2문장 (paragraph 범위 내 crop)
anchor_diff_region               → diff_text + surrounding_paragraph
section_local_excerpt            → section_heading + paragraph_text
```

`ContextBundle`은 `text_parts` (in-memory only)와 `char_counts` (persistable) 분리. JSONL에는 char_counts만 들어감.

### 3.5 ProactiveGenerator — [`generator.py`](services/proactive/generator.py)

ProactiveTask + ContextBundle → SSE events (start/target/delta/done/error).

라우팅:
- native + next_sentence → `ChatAgent.iter_ghostwrite` (system prompt가 이미 pure continuation 강제)
- 그 외 → `ChatAgent.iter_editor_assist` + task-별 lead-in

External lead-in: **`[응답 형식 — 반드시 준수]` 강제**
- 첫 부분: 복사-붙여넣기할 본문만
- 빈 줄 후 `설명:`으로 시작하는 한두 줄
- 본문에 "추천합니다" 같은 메타 발언 금지

Native lead-in (inline-diff용): 머리말/꼬리말/설명 모두 금지 — renderer가 원문을 통째로 교체하니까.

### 3.6 Null Outcome Monitor — [`null_outcome_monitor.py`](services/proactive/null_outcome_monitor.py)

NullPrediction은 30초 horizon으로 등록 → 만료되면 분류:

```python
classify_null_outcome(edit_volume, churn, idle, user_invoked_help, ...) →
    "tn_proxy"  # vol≥40 & churn<0.5 — 사용자가 자연스럽게 계속 작성. 침묵 정답.
    "fn_proxy"  # idle≥25 or churn≥0.6 or user_invoked_help — 개입했어야 함.
    "unknown"   # app 전환 / 데이터 부족
```

`null_outcomes.jsonl`에 기록 — 학습된 모델에 안 들어가고 운영자 대시보드용.

---

## 4. 엔지니어링 결정

### 4.1 패키지 레이아웃 (MVC + Service)

```
services/proactive/                  ← 도메인 로직 (LLM / HTTP / Qt 모름)
  ├─ models.py                       ProactiveObservation / FeedbackRecord 등
  ├─ anchors.py                      ActiveAnchor + confidence bands
  ├─ proposal_models.py              ProactiveTask / NullPrediction / SurfaceCaps
  ├─ features.py                     primitive normalization (사용처 줄어듬)
  ├─ candidates.py                   CandidateFactory — anchor-local
  ├─ evaluator.py                    하드 게이트 + 점수
  ├─ adaptation.py                   UserAdaptationMemory + atomic write
  ├─ context_selector.py             anchor-relative ContextBundle
  ├─ reward.py                       canonical mapping (incl. wrong_anchor)
  ├─ policy_store.py                 JSONL 로그 + adaptation glue
  ├─ timeout_monitor.py              render 타임아웃만
  ├─ null_outcome_monitor.py         TN/FN proxy 분류
  ├─ orchestrator.py                 observe / record_feedback / explain / reset
  ├─ generator.py                    ProactiveTask → SSE
  ├─ screen_bridge.py                screen pipeline 글루
  ├─ telemetry.py                    console + per-workspace log
  ├─ action_space.py                 (deprecated, stub만 유지)
  └─ legacy_bandit/                  frozen reference only
      └─ policies/
          ├─ action_centered_engage.py
          └─ discounted_linucb.py

api/api_routes/proactive.py          FastAPI routes
api/services/proactive_service.py    Pydantic ↔ dataclass 어댑터 (유일 매핑 지점)
```

도메인 코어가 FastAPI/Pydantic을 import하지 않음 → 테스트가 가볍고 다른 transport에 재사용 가능.

### 4.2 Bandit 코드 동결

`services/proactive/legacy_bandit/policies/`로 이동. `legacy_bandit/__init__.py`에 "import하지 마라" 명시. orchestrator/candidates/evaluator/adaptation 어디서도 import하지 않음 — 테스트(`test_proactive_api.py:test_orchestrator_module_does_not_import_bandit`)로 보장.

### 4.3 In-memory Decision Cache (변경 없음)

FIFO 512. `text_parts`는 여기에만, 영구화 파일엔 char_counts와 anchor_id 해시만.

### 4.4 Atomic Persistence (변경 없음)

`user_adaptation.json`은 `tempfile + fsync + os.replace`로 원자화. `policy_state.json` (bandit era)은 더 이상 작성 안 됨 — 워크스페이스가 마지막 bandit run에서 남은 게 있어도 read 안 함.

### 4.5 Per-workspace Lifecycle

AgentRuntime이 워크스페이스 전환 시 orchestrator를 close → 새로 인스턴스화. `_render_timeout_monitor` + `_null_outcome_monitor` 두 daemon thread를 안전하게 stop, telemetry file handle 해제.

### 4.6 Frontend (PySide6)

| Surface | 사용자 동작 | Backend → canonical |
|---|---|---|
| Native ghost | TAB | `tab` → accept |
| Native ghost | ESC / 다른 키 | `esc` → reject |
| Native ghost chip | 다시 | `retry` → retry |
| Native ghost | 20s 무반응 | `timeout` (QTimer) → timeout |
| External card | 복사 | `copy` → accept |
| External card | 거절 (빨강) | `red_reject` → reject |
| External card | 다시 | `retry` → retry |
| External card | **위치 다름** (신규) | `wrong_anchor` → wrong_anchor |
| External card | 45s 무반응 | `timeout` (백엔드 sweeper) → timeout |

`pd_*` event_id 카드만 "위치 다름" 버튼 노출. legacy (도움됨/아쉬움) 카드는 기존 그대로.
→ [`document_assist_window.py:SuggestionCard`](frontend/ui/windows/document_assist_window.py)

### 4.7 Telemetry 단순화

bandit 용어 (UCB, residual, θ̂, warmup) 모두 제거. 새 줄:

```
[proactive][decision] pd_abc... native_editor task=next_sentence anchor=anc_xyz...
  conf=0.92 scope=cursor_previous_sentences render=native_ghost
  score=0.71 threshold=0.62 candidates=1 idle=4.2s churn=0.05 recent_neg=0.05

[proactive][decision] pd_def... external_app prediction=null
  reason=score_below_threshold candidates=2 gates=[]
  best_score=0.54 threshold=0.62

[proactive][feedback] pd_abc... native_editor accept task=next_sentence
  anchor=anc_xyz Δthr=-0.015

[proactive][null_outcome] pd_def... tn_proxy vol=80 churn=0.10 idle=4.3s
```

### 4.8 API surface (변경 최소)

```
POST /api/v1/proactive/observe          → task | null (new schema)
POST /api/v1/proactive/generate/stream  → SSE; null이면 즉시 done
POST /api/v1/proactive/feedback         → canonical 적용, adaptation 업데이트
GET  /api/v1/proactive/explain/{id}     → human-readable trace
GET  /api/v1/proactive/snapshot         → adaptation 상태 dump
POST /api/v1/proactive/reset            → adaptation wipe (log 보존)
```

legacy wrapper:
```
POST /api/v1/editor/suggest             → 내부적으로 observe→generate
POST /api/v1/screen-monitoring/feedback → pd_* prefix면 proactive로
```

---

## 5. 테스트 커버리지

89개 테스트 통과 (3 skip). 카테고리:

```
test_proactive_features.py            primitive 정규화 / evidence-need
test_proactive_reward.py              canonical 매핑 (TAB == 복사 == accept, wrong_anchor)
test_proactive_anchor.py              confidence bands / anchor_id 안정성
test_proactive_candidates.py          surface-aware 마스킹 / 후보 수 / anchor 바인딩
test_proactive_evaluator.py           하드 게이트 / 점수 범위 / 임계값 +inf
test_proactive_adaptation.py          feedback 비대칭 (retry/wrong_anchor)
test_proactive_null_outcome.py        TN/FN proxy 분류
test_proactive_api.py                 E2E observe→feedback, raw text 누출 방지
```

핵심 회귀 가드:
- `test_decisions_jsonl_contains_no_raw_text` — JSONL에 raw text 누출 방지
- `test_user_adaptation_contains_no_raw_text` — JSON state도 동일 보호
- `test_orchestrator_module_does_not_import_bandit` — production path에 bandit 없음
- `test_off_anchor_target_is_rejected` — 다른 위치 target task 차단
- `test_wrong_anchor_does_not_count_as_task_reject` — feedback rule asymmetry
- `test_native_default_is_next_sentence_only` — 미지원 render mode가 native에서 후보가 안 됨
- `test_repeated_reject_suppresses_task_type` — 3회 reject → 10분 suppression
- `test_cooldown_returns_infinity` — anchor/task 쌍 cooldown이 임계값 통과 차단

---

## 6. 확인된 한계

### 6.1 구조적 한계

**(A) anchor 추출 품질이 시스템의 천장**
모든 게이트가 anchor에 묶여 있으므로 OCR-only screen capture가 paragraph를 잘못 잡으면 `wrong_anchor` 폭증 후 threshold가 천천히 올라가는 식으로 degrade. UIA caret 우선 — OCR fallback은 confidence ≤ 0.65로 cap.

**(B) Task-type capability gating은 hard-coded 임계값**
"`paragraph_rewrite`는 paragraph ≥ 80자에 churn ≥ 0.30" 같은 숫자가 candidate factory + evaluator에 흩어져 있음. env var 튜닝 미지원 (의도) — 신뢰성 우선.

**(C) Context는 학습 대상이 아님**
spec MVP대로 task별 default scope를 hard-code. cursor 위치 / 글쓰기 단계에 따라 자동 조정은 안 됨 — operator가 다른 scope를 원하면 코드 수정 필요.

**(D) retry는 task_type을 패널티하지 않지만 무한 다시-쓰기는 막지 않음**
`prompt_style_flags.recent_retry_count`만 증가. 4-5번 retry가 누적되면 operator가 명시적으로 reset 해야 그 패턴이 빠짐.

### 6.2 통합 한계

**(E) Native inline-diff renderer 미구현**
`paragraph_rewrite` / `logic_flow_review` 등은 external_app에서만 후보로 emit. native_editor는 사실상 `next_sentence` only.

**(F) Screen pipeline은 여전히 ChatAgent scenario scheduler 기반**
proactive observe는 *piggy-back*해서 학습용 신호만 받음 — bandit-era와 같은 shadow 학습 형태. 후속에서 candidate factory가 screen capture를 직접 driving 하는 구조로 전환 필요.

**(G) generation-시점 RAG가 evidence task 한정**
`evidence_or_citation_prompt`만 source_snippets를 받을 수 있고, 다른 task는 RAG 없음. 다른 task에 source-grounded rewrite가 필요해지면 ContextBundle 확장 필요.

**(H) Null outcome 분류는 노이지**
임계값 hard-coded (40 chars / 0.5 churn / 25s idle). user_invoked_help signal이 frontend에서 안 들어오면 fn_proxy 검출 못 함.

### 6.3 운영 한계

**(I) Reset은 partial 안 됨**
한 워크스페이스의 adaptation 전체를 reset. "logic_flow_review suppression만 풀고 싶다" 같은 selective reset 미지원.

**(J) anchor_id collision는 이론상 가능**
80-char bucket + paragraph hash로 만들지만, 같은 doc에서 동일한 paragraph가 두 군데 있으면 ID 충돌. 실전에서는 거의 안 일어나지만 worst case 알아둬야 함.

**(K) 다중 사용자 환경 미고려**
adaptation은 워크스페이스 단위. 같은 ws를 여러 명이 쓰면 한 명의 거절이 모두에게 적용.

**(L) Telemetry는 plain text 줄**
구조화된 분석은 JSONL 파싱 필요. Grafana 같은 대시보드 통합 별도 작업.

---

## 7. 후속 작업 우선순위

1. **Screen capture pipeline의 cursor-anchored UIA 우선화** — (A) 완화
2. **Native inline-diff renderer** — (E) 해결, native에서 5가지 task 추가 활성화
3. **Generation-time RAG for evidence task** — (G) 해결
4. **Threshold/cooldown env override** — operator가 코드 수정 없이 튜닝 가능 — (B) 완화
5. **Selective reset endpoint** — (I) 해결
6. **frontend user_invoked_help signal** — (H) 정확도 향상

---

## 8. Bandit 잔재 (frozen)

`services/proactive/legacy_bandit/policies/`에 남아있는 두 클래스:

- `ActionCenteredEngagePolicy` — Greenewald-Tewari action-centered estimator
- `DisjointDiscountedLinUCB` — UCB로 suggestion arm 선택

**production import 절대 금지.** import 시 lint 또는 PR review에서 reject. 이유:
- noisy reward signal로 빠르게 보수화 (3 reject → π_min lock)
- exploration이 매번 *anchor-irrelevant* suggestion을 만들 위험
- "왜 이 결정을 했나" introspection이 θ̂ matrix 해석에 의존

향후 충분한 anchor-quality 데이터가 쌓이면 (Year 2+), `(task_type, anchor_context)` joint 학습은 다시 고려 가능.

---

## 9. 영구화 경로

```
runs/<workspace_id>/proactive_policy/
  ├─ user_adaptation.json      EMA + cooldowns + suppression + threshold_offset
  ├─ decisions.jsonl           모든 observe (task/null), gate_reasons, char_counts only
  ├─ feedback.jsonl            canonical 매핑 + adaptation changes
  ├─ updates.jsonl             adaptation state delta
  ├─ null_outcomes.jsonl       TN/FN proxy 분류
  ├─ pending_timeouts.jsonl    render + null_outcome 미해결 큐
  └─ proactive.log             사람이 읽을 수 있는 timeline
```

기존 `policy_state.json` (bandit era)이 남아있어도 새 코드는 read 안 함 — 안전하게 무시.
