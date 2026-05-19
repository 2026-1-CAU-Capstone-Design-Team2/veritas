# PLAN.md — 구현 / 버그수정 로드맵

> 2026-05-19 시점 architecture 자가 점검 결과와 다음 액션 아이템.
> 코드 변경 없이 `feat/lint` 상태를 그대로 평가한 문서입니다.

---

## 0. 자가 점검 결과 한눈에

| 영역 | 평가 | 핵심 근거 |
|---|---|---|
| **계층 분리** | 🟢 양호 | [ARCHITECTURE.md](ARCHITECTURE.md) 가 layer rule 명시, import 방향 일관, prompts 중앙화 |
| **모델 흐름** | 🟢 양호 | dataclass 일관, persistence/loader 가 disk I/O 단일 소유 (§1.2) |
| **LLM 호출 패턴** | 🟡 부분 | client 는 단일 (`LLMClient`) 이지만 10+ 호출처가 timeout/retry/stream 을 제각각 결정 |
| **DB / 영속화** | 🟡 부분 | 3-store (filesystem / SQLite / ChromaDB) 간 transaction 없음, migration 스토리 부재 |
| **에러 처리** | 🟡 부분 | LLM fallback 은 잘 됨, 그러나 frontend 가 모든 예외를 silently 삼킴 → pageSize=200/le=100 같은 버그가 오래 감춰짐 |
| **설정 외부화** | 🟡 부분 | `VerificationConfig` 는 정돈, but reliability/autosurvey 상수는 코드 내부 |
| **테스트** | 🔴 **0** | 테스트 디렉토리/파일/의존성 모두 없음. `VERIFY_DESIGN.md §1.8` 에 "테스트 작성" 원칙이 있지만 실제로는 0 |
| **문서** | 🟢 양호 (한 곳 stale) | 디렉토리별 README 풍부. 단 `VERIFY_DESIGN.md` 는 Task 2 (intent) 기준이라 재작성 필요 |
| **관측성** | 🔴 약함 | `print()` 기반 logging, LLM call latency/실패율 telemetry 없음 |

### 영역별 종합
* **잘 된 것**: layer 가 명확하고 prompts/data model 이 깨끗하게 분리됨. local-first 워크플로우 (file artifacts) 가 디버깅 친화적.
* **눈에 띄는 부채**: 테스트 부재 + observability 부족 → 최근 4 개 PR 이 모두 "심볼릭 버그를 사용자가 발견 → 사후 패치" 패턴. 회귀 비용이 점점 커지는 추세.
* **잠재 폭탄**: agent_runtime 1102 라인 god class, autosurvey_workflow `run_all` 290 라인 nested loop, verify_page 1735 라인 — 한 곳을 건드릴 때 곁가지를 부수기 쉽고, 테스트가 없으니 곁가지 부수기를 알아챌 길이 없음.

---

## 1. 우선순위 매트릭스

```
            영향 ↑
              │
         P0   │   P1     ← 다음 작업 차수에서 진입
       ──────┼──────
         P2   │   P3     ← 시간 여유 있을 때
              │
              └────────→  비용
```

| 코드 | 의미 | 차수 |
|---|---|---|
| **P0** | 사용자 체감 버그 / 설계 일관성 깨짐 — 다음 한두 세션 안에 해결 | 1~3주 |
| **P1** | 구조적 부채 — 더 쌓이기 전에 끊어내야 함 | 2~4주 |
| **P2** | 관측성·QoL — 디버깅 비용 감소 | 4~8주 |
| **P3** | 기능 확장 — 핵심 흐름 안정화 후 | 2026 Q3 |

---

## 2. P0 — 다음 세션 우선 처리

### P0-1. 테스트 부트스트랩 (필수 선행작업)
**문제**: 테스트가 0 건이라 최근 4 건의 사용자 발견 버그 (페이지 사이즈 422, 중복 카드, K-뷰티 medium, LaTeX 이중 이스케이프) 가 모두 회귀 가드 없이 머지됨. 다음 변경의 안전망이 없음.

**작업**:
- `requirements.txt` 에 `pytest`, `pytest-cov` 추가
- `tests/` 디렉터리 생성, 최소한 5 개 unit test 부터:
  1. `tests/verification/test_batch_index.py` — 정규식 파서 (citation marker 추출, multi-line bullet, padding)
  2. `tests/verification/test_reliability_judge.py` — `_derive_level` 결정 매트릭스 (현재 ad-hoc 으로 확인한 11 케이스)
  3. `tests/verification/test_persistence_roundtrip.py` — `ReliabilityResult` 직렬화 ↔ 역직렬화 동등성
  4. `tests/verify_view/test_build_doc_items.py` — duplicate 필터링, fallback (doc_titles 비어있을 때)
  5. `tests/core/test_latex_cleanup.py` — 4 가지 케이스 (정상 idempotent / `\\mathcal` 복구 / `\\\\` 보존 / 산문 미터치)
- `conftest.py` 에 worktree-내 import path 보정
- 실행 명령을 `README.md` 에 한 줄 추가: `pytest tests/`

**산출물**: 5 개 test file, requirements.txt 변경, README 갱신.  
**예상 비용**: 0.5~1 일.  
**가치**: 이후의 모든 작업이 안전망 위에서 진행 가능. **이 작업이 끝나기 전에 P1 이하 진입 금지** 를 권장.

---

### P0-2. `VERIFY_DESIGN.md` 갱신 (or 폐기)
**문제**: `VERIFY_DESIGN.md` 는 retired Task 2 (intent_coverage) 를 정식 단계로 명시하고 있음. 신규 개발자가 읽으면 첫날부터 잘못된 mental model 을 가짐.

**작업**:
- Task 2 섹션을 retired 표시 + reliability section 추가
- `RELIABILITY_JUDGE_PROMPT` 의 4 sub-signal 매트릭스 및 override rule 명시
- `batch_*.md` 의 `[doc_<id>]` citation 계약 명시 (BATCH_SUMMARY_PROMPT 변경)
- "0. 입력 / 출력" 의 출력 파일 목록에 `reliability.json` 추가, `intent_coverage.json` 은 legacy 표시
- §1.8 의 "테스트" 절을 실제 `tests/` 디렉터리 구조와 연결

**산출물**: 갱신된 `VERIFY_DESIGN.md`.  
**예상 비용**: 1-2 시간.

---

### P0-3. Frontend silent exception swallowing
**문제**: [`frontend/ui/pages/verify_page.py:1428-1438`](frontend/ui/pages/verify_page.py:1428) 의 `try/except Exception` 이 어떤 API 오류든 같은 fallback (`{"items": [], "available": False}`) 으로 처리. `pageSize=200/le=100` 422 버그가 이 swallow 때문에 사용자에게 "0개 카드" 로만 보였음. 같은 패턴이 `get_verify_summary`, `get_verify_detail`, `list_verify_results` 호출 모두에 있음.

**작업**:
- `_refresh_data` 의 try/except 에 최소한 `logger.warning(f"verify-fetch failed: {exc}")` 추가
- `ApiError` 와 일반 `Exception` 분리 — `ApiError` 면 HTTP status / detail 을 progress bar 에 "검증 데이터를 가져올 수 없습니다 (HTTP NNN)" 식으로 표시
- frontend dev 화면 (e.g. shift-click 으로 열리는 debug panel) 에 최근 5 개 API error 노출 — 옵션. 우선순위는 첫 두 항목

**작업 위치**: `frontend/ui/pages/verify_page.py`, `frontend/ui/pages/research_page.py` (같은 패턴 추정)  
**예상 비용**: 0.5 일.

---

### P0-4. 중복 탐지 false positive 조사 (`dup_000`/`dup_001` in AI_Agent)
**문제**: `index.json` 에서 `dup_000` = "GitHub - ctallec/world-models" / `dup_001` = "GitHub - werner-duvaud/muzero-general" 가 `duplicate_of: "011"` 로 잘못 묶임. 실제로 셋 다 다른 GitHub 저장소.

**조사 범위**:
- `services/run_store_tool_funcs/` 의 중복 탐지 로직 (likely content hash 또는 URL canonicalization)
- cleanup 단계에서 본문이 비슷한 boilerplate 만 남기면 cleaned content hash 가 같아질 수 있음

**작업**:
- 실제 hash 충돌 원인 reproduce (3 개 GitHub doc 의 clean_md 비교)
- title 또는 URL 의 path component 차이를 dedup signal 에 가산
- false positive 가 발생하면 LLM 한 줄로 "이 둘이 정말 같은 문서인가?" 자문 — 비용 대비 가치 검토 후 결정

**예상 비용**: 0.5~1 일.  
**가치**: documentCount 정직성 + verify 의 inherited verdict 노이즈 제거.

---

## 3. P1 — 구조 부채 (2~4주)

### P1-1. `VerificationConfig` 에 reliability/autosurvey 상수 흡수
**현재 상태**: thresholds 가 세 곳에 흩어져 있음:
- `services/verification/models.py:VerificationConfig` — verify thresholds (잘 정돈)
- `services/verification/reliability/llm_judge.py` — `_DEFAULT_BATCH_SIZE = 5`, `_RELIABILITY_NOTES_MAX = 6`, `_KEY_POINTS_MAX = 5`, `_BATCH_MENTIONS_MAX = 4` (코드 내부)
- `workflows/autosurvey_workflow.py:AutoSurveyWorkflow.__init__` — `max_docs=15`, `collect_batch_size=5`, `scout_docs=3` (생성자 인자)

**영향**: configHash fingerprint 가 reliability batch_size 변경을 감지 못 함 → 캐시가 깨끗하게 무효화되지 않음. 또 사용자가 batch_size 를 튜닝하려면 코드 수정 필요.

**작업**:
- `VerificationConfig` 에 `reliability_batch_size`, `reliability_notes_max`, `reliability_key_points_max`, `reliability_batch_mentions_max` 추가
- `llm_judge.judge_documents` 시그니처에 `cfg: VerificationConfig` 전달
- `AutoSurveyConfig` 새 dataclass 만들어 `max_docs`/`collect_batch_size`/`scout_docs`/`reliability_batch_size` (verify 와 일치하도록) 통합
- fingerprint 가 verify 단계 결과 캐시 무효화에 reliability 상수 포함하도록 보장

**예상 비용**: 1 일.

---

### P1-2. `agent_runtime.py` god class 분리
**현재 상태**: 1102 라인 단일 클래스가 LLM client + tool registry + workflow lifecycle + chat agent + verify progress buffer + screen monitoring + workspace switching + job state 까지 모두 보유. 한 메소드만 건드려도 정신적으로 전체를 로드해야 함.

**작업**:
- 책임 단위로 분리:
  1. `LLMLifecycle` — LLM client 생성/교체/health check
  2. `WorkspaceController` — workspace_id 해석, switch, discover_initial
  3. `ResearchProgress` / `VerifyProgress` — ring buffer 각각 별도 클래스
  4. `ScreenMonitoringController` — screen context 관련 (전체를 옵트인 기능으로 격리)
  5. `AgentRuntime` 는 위 4 개를 composition 으로 보유하는 thin facade
- 외부 호출 site 변경 최소화: 기존 메소드 시그니처를 facade 에서 위임만 함

**제약**: 한 번에 모두 옮기지 말고 한 책임씩 (e.g. screen monitoring 먼저, 가장 격리된 영역) → 머지 → 다음.

**예상 비용**: 2~3 일 (단계적).

---

### P1-3. `DocRecord` 중복 정리
**문제**: 두 군데에 `DocRecord` 가 있음:
- `core/models.py` — autosurvey 측 (수집 단계에서 사용)
- `services/verification/models.py` — verify 측 (artifact_loader 가 disk 에서 재구성)

두 클래스의 필드가 미묘하게 다름 (verify 측에 `is_duplicate`, `clean_md_text`, `summary`, `key_points` 등 추가). 한 쪽이 진화하면 다른 쪽과 schema drift 위험.

**옵션 A**: `core/models.py` 의 `DocRecord` 를 verify 측 필드까지 모두 흡수 → autosurvey 도 같은 클래스 사용  
**옵션 B**: 두 클래스를 명시적으로 분리: `RawDocRecord` (autosurvey side) vs `VerificationDocRecord` (verify side)

**권장**: 옵션 B. 이름이 다르면 schema drift 가 의도적 결정이 됨. autosurvey 가 collect 단계에서 보는 정보와 verify 가 disk 에서 재로드한 정보는 본질적으로 다른 시점/내용이므로 두 모델로 분리하는 게 자연스러움.

**예상 비용**: 0.5~1 일.

---

### P1-4. Vestigial intent code 정리 결단
**현재 상태**: Task 2 (intent_pipeline) 는 default task 에서 제외했지만 코드는 그대로:
- `services/verification/intent/` 전체 디렉터리
- `VerificationArtifacts.intent` 필드 (Optional)
- `intent_coverage.json` 직렬화/역직렬화 함수
- `verify_view.facet_breakdown_for` legacy 함수
- `_KNOWN_TASKS` 에 "intent" 옵트인 가능

**결단 필요**:
- **유지**: tuning/regression 비교 목적으로 옵트인 가능. 코드 dead path 가 5~6 곳에 남음.
- **삭제**: 디렉터리 통째로 제거. 옵트인 경로 닫기. 영구 비활성.

**권장**: 삭제. autosurvey/verify 가 stable 해진 지금, regression 비교를 위해 죽은 코드를 메인 브랜치에 남기는 비용 > 미래에 다시 켤 수 있는 옵션의 가치. 필요하면 git history 로 복원 가능.

**예상 비용**: 0.5 일.

---

## 4. P2 — 관측성 / QoL

### P2-1. Structured logging
**문제**: LLM client 와 모든 tool 이 `print()` 로 로그. 운영 시 stdout 캡처 외 방법이 없고, 필터링/검색이 불가능.

**작업**:
- `core/logging.py` 새 모듈: `get_logger(name)` 헬퍼, RichHandler 우선 + fallback 표준 handler
- `llm/llama_server_llm.py` 의 모든 `print` → `logger.info` / `logger.warning` / `logger.debug`
- LLM call 마다 structured field 로 latency/input_tokens/output_tokens/retries 기록
- 로그 레벨을 환경변수 `VERITAS_LOG_LEVEL=INFO` 로 제어

**예상 비용**: 1~2 일.

---

### P2-2. Verify progress event 타입화
**문제**: `frontend/ui/pages/verify_page.py:50-58` 의 `_STAGE_PROGRESS` 가 손으로 유지하는 dict (`{"sections": 35.0, "reliability": 65.0, ...}`). 새 task 추가 시 frontend 도 따로 수정 필요.

**작업**:
- `services/verification/service.py` 에 `TASK_PROGRESS_HINTS: dict[str, float]` 추가
- API `GET /api/v1/verify/tasks` 새 엔드포인트 (또는 `/verify/summary` 응답에 포함) 으로 현재 task 목록 + progress hint 노출
- frontend `_STAGE_PROGRESS` 를 API 응답으로 대체

**예상 비용**: 0.5 일.

---

### P2-3. Workspace migration script
**문제**: 신규 schema 가 도입될 때 (예: reliability.json 에 `request_alignment` 추가) 기존 워크스페이스는 자동으로 갱신되지 않음. 사용자가 verify 를 재실행해야 함.

**작업**:
- `scripts/migrate_workspaces.py` 만들기:
  - 모든 `runs/*/verification/reliability.json` 스캔
  - schema version 검사 (configHash 가 다르거나 누락 필드 감지)
  - 재실행이 필요한 workspace 목록 출력
  - 옵션: `--apply` 로 자동 재실행

**예상 비용**: 1 일.

---

### P2-4. `core/prompts.py` 분리
**현재 상태**: 단일 파일에 14 개 prompt + 800+ 라인. 한 prompt 만 보고 싶어도 파일을 전체 스크롤.

**작업**:
- `core/prompts/` 디렉터리로 변환:
  - `core/prompts/__init__.py` — 기존 import 호환을 위한 re-export
  - `core/prompts/autosurvey.py` — TERM_GROUNDING / INITIAL_PLANNER / REPLANNER / DOC_SUMMARY / DOC_CHUNK_NOTES / DOC_SUMMARY_REDUCE / BATCH_SUMMARY / FINAL
  - `core/prompts/cleanup.py` — DOCUMENT_CLEANUP
  - `core/prompts/verify.py` — VERIFY_FLOW_PLANNER / RELIABILITY_JUDGE
  - `core/prompts/chat.py` — SYSTEM / TOOL_CHAT / RAG / QUERY_REWRITE / SCREEN_INTERVENTION

기존 `from core.prompts import X` 는 그대로 동작 (re-export).

**예상 비용**: 0.5 일.

---

## 5. P3 — 기능 확장

### P3-1. Sections splitter / ordering 개선
이전 design 토론에서 "추후" 로 미뤄둔 항목:
- Kiwi → KSS 같은 더 나은 한국어 문장 splitter 도입 (쉼표 중간에서 잘리는 문제)
- 같은 섹션 안 문장들의 LLM 기반 logical ordering (현재는 fit_score 순)

**예상 비용**: 3~5 일.

### P3-2. Conflict 패널 UI 강화
Task 3 (consensus/conflict) 결과가 현재 카드 footer 한 줄 + Issues dialog 한 항목으로만 나타남. 상세 다이얼로그에 "갈리는 입장" 패널 (두 클러스터의 KP 발췌 비교) 을 추가하면 검증 가치 ↑.

**예상 비용**: 1~2 일.

### P3-3. Legacy batch 재생성 도구
오래된 batch_*.md (citation marker 없음) 를 가진 워크스페이스를 위한 명시적 재요약 명령. CLI: `python main.py rebuild-batches <workspace>`.

**예상 비용**: 0.5 일.

### P3-4. Verify telemetry dashboard
모든 워크스페이스의 verify 결과 (높음/중간/낮음 분포, 평균 elapsed, low_reliability 원인 분포) 를 dashboard 페이지에 집계. 시간이 지나면서 LLM judge 의 calibration drift 를 감지 가능.

**예상 비용**: 2~3 일.

---

## 6. 권장 실행 순서

```
WEEK 1
  P0-1  테스트 부트스트랩         (1d)  ← gating: 이 후에 모든 P1+ 진입
  P0-2  VERIFY_DESIGN.md 갱신     (2h)
  P0-3  Frontend exception 처리   (0.5d)
  P0-4  dup detection 조사        (1d)

WEEK 2~3
  P1-4  Intent 코드 제거          (0.5d) ← 큰 변경 전에 dead code 청소
  P1-1  Config 통합               (1d)
  P1-3  DocRecord 분리            (0.5d)
  P1-2  agent_runtime 분리        (2~3d, 단계적)

WEEK 4 이후
  P2 항목들 (logging, migration script, prompts 분리)

P3 는 P2 완료 후
```

**Gating principle**: P0-1 (테스트) 이 끝나기 전에 어떤 P1+ 도 진입하지 않는다. 회귀 보호가 없는 상태에서 god class 를 분리하는 건 위험.

---

## 7. 의도적으로 plan 에서 제외한 것

- **AutoSurvey replan 로직 재설계**: 현재 gap-driven + recovery fallback 이 작동 중. 큰 리팩터링 가치 < 안정성 가치.
- **RAG 채팅 UX**: 본 작업과 무관, 별도 트랙.
- **Screen monitoring 기능**: 옵트인 기능이라 우선순위 ↓. agent_runtime 분리할 때 자연스레 정리됨.
- **ChromaDB 대체**: SQLite-backed Chroma 가 현재 잘 동작. 더 큰 corpus 가 필요해질 때까지 보류.
- **LaTeX cleanup 의 pylatexenc 대체**: 규칙 기반이 충분히 동작 + idempotent. 라이브러리 의존성 추가 가치 < 현재의 단순함.

---

## 8. 미해결 질문 (다음 결정 필요)

1. **테스트 backend**: pytest + simple fixtures 로 충분한가? 아니면 vector store 등의 통합 테스트도 docker-compose 로 띄울까? — 현재 권장: pytest + 모킹 only, 통합은 수동.
2. **logging**: standard `logging` 으로 갈지, `structlog` / `loguru` 도입할지. — 현재 권장: standard. 의존성 최소.
3. **CI**: GitHub Actions 로 PR 마다 pytest 자동 실행할지. — 권장: P0-1 완료 후 즉시 도입.
4. **VerificationConfig override**: workspace 별 `verification_config.json` 으로 사용자 튜닝 허용할지. P1-1 의 후속.

---

_Last updated: 2026-05-19_
_Author: Architecture audit by Claude (auto-generated)_
