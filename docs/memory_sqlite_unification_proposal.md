# Memory Layer 저장소 SQLite 단일화 제안서

**대상 브랜치**: `feat/59-memory`
**작성 기준일**: 2026-05-30
**전제 환경**: 로컬 Qwen3.5 4B~9B GGUF + llama-server, 단일 사용자
**역할**: 본 문서는 리뷰/설계 제안. 코드 수정은 codex 전담.

---

## 0. 한 줄 요약

JSONL 시대에 설계된 저장 구조를 SQLite로 확장하면서 **이중 저장(JSONL+SQLite)** 이 남았고, 그 결과 (1) 과도한 fallback 코드, (2) 분산된 저장 파일, (3) turn당 8회 connect/close I/O 가 생겼다. **SQLite를 단일 진실 소스로 통일**하면 셋 다 동시에 해소된다.

---

## 1. 현재 상태 (측정 기반)

### 1-A. 저장 파일이 워크스페이스당 10종, 그중 3쌍이 중복

| 파일 | 용도 | 상태 |
|---|---|---|
| `fifo_queue.jsonl` | FIFO 레거시 로그 | ⚠️ `fifo.sqlite3`와 **중복** |
| `fifo.sqlite3` | FIFO 실저장 | |
| `recall_storage.jsonl` | recall 미러 로그 | ⚠️ `recall.sqlite3`와 **중복** |
| `recall.sqlite3` | recall 검색(FTS5) | |
| `archival/items.jsonl` | archival 미러 로그 | ⚠️ `archival.sqlite3`와 **중복** |
| `archival/archival.sqlite3` | archival 검색(FTS5) | |
| `summaries.jsonl` | 요약 누적 | JSONL only |
| `working_context.json` | working record list | JSON only |
| `invocations.jsonl` | 호출 감사 로그 | JSONL only (append-only 감사용, 적합) |
| `memory_state.json` | — | 💀 **dead** (`store.py` 외 참조 0) |

근본 원인: 현재 구조는 JSONL로 먼저 설계한 것을 SQLite로 확장하며 재구성한 것. **JSONL을 "호환 로그"로 영구 유지하기로 한 결정**이 중복과 fallback을 강제한다.

### 1-B. Fallback 코드 규모 (제거 대상)

`recall_storage.py` / `archival_storage.py`는 **테이블명만 다른 동일 코드가 복붙**돼 있다 (각 252줄):

- `_sqlite_disabled` 플래그 + 모든 공개 메서드의 try/except
- `_search_jsonl` (SQLite 실패 시 키워드 풀스캔 폴백)
- `_ensure_sqlite_current` (JSONL↔SQLite 동기화 재빌드)
- `_rebuild_sqlite_from_rows`, `_jsonl_id_count`, `_synced_signature`, `_jsonl_signature`

> grep 측정: recall 21건 + archival 20건의 fallback-관련 식별자 참조. 두 파일 합 504줄 중 검색 핵심 로직은 ~40줄, 나머지 상당수가 이중저장 동기화/폴백 보일러플레이트.

### 1-C. I/O 측정 — turn당 connect 8회

```
prepare() 1회 → sqlite3.connect 6회
commit()  1회 → sqlite3.connect 2회
합계: 채팅 1 turn당 connect/close 8회
```

원인: 모든 storage 메서드가 `with closing(self._connect())` 패턴. **연산마다 파일 open → 작업 → close**. WAL/PRAGMA 설정 없음 (grep 0). SQLite의 페이지 캐시(인메모리) 이점을 매번 버린다. 즉 "임베디드 DB"가 아니라 "매번 여닫는 파일"로 사용 중.

---

## 2. 설계 원칙

1. **단일 진실 소스(Single Source of Truth)**: 각 데이터는 한 곳에만 산다. SQLite.
2. **Fallback 최소화**: SQLite(stdlib + WAL)는 로컬 단일 사용자 환경에서 사실상 실패하지 않는다. 실패 시 조용한 폴백 대신 **명확한 에러**.
3. **Connection 재사용**: storage별 connection 1개를 열어두고 재사용. OS/SQLite 페이지 캐시가 자연히 인메모리 역할.
4. **호환성 점진 폐기**: JSONL은 1회 마이그레이션 후 폐기. 사람이 읽을 감사 로그는 `invocations.jsonl` 하나만 유지.

---

## 3. 제안 — 3단계

각 단계는 독립적으로 머지 가능하며, 앞 단계가 뒤 단계의 전제다.

### Phase A — Fallback 제거 + JSONL 미러 폐기 (우려 1·2)

**목표**: 이중 저장 종료. SQLite 단일 소스화.

1. **공통 베이스 클래스 `FtsMemoryStore` 도입**
   - recall/archival의 복붙된 7개 메서드(`_connect`, `_ensure_schema`, `_upsert_row`, `_search_sqlite`, `_tail_sqlite`, `_fts_query`, `_sqlite_row_to_dict`)를 베이스로 흡수.
   - `recall`/`archival`은 테이블 접두어(`recall_`/`archival_`)만 다른 서브클래스.
   - 예상 감소: recall 252 + archival 252 → 베이스 ~150 + 서브클래스 각 ~30 = **약 250줄 절감**.

2. **JSONL 미러 쓰기 제거**
   - `RecallStorage.append` / `ArchivalStorage.insert`에서 `append_jsonl` 호출 삭제. SQLite만 기록.
   - `FifoStorage`는 이미 SQLite 단일 — `_ensure_sqlite_from_legacy_if_needed`의 레거시 import만 유지(1회 마이그레이션용), 그 외 JSONL 쓰기 없음 확인.

3. **Fallback 경로 삭제**
   - `_sqlite_disabled`, `_search_jsonl`, `_ensure_sqlite_current`, `_rebuild_sqlite_from_rows`, `_jsonl_id_count`, `_synced_signature`, `_jsonl_signature` 전부 제거.
   - SQLite 연산은 try/except로 감싸지 않고 자연 전파(또는 최상위 1곳에서만 로깅).

4. **1회 마이그레이션**
   - 부팅/첫 접근 시 `recall_storage.jsonl`, `archival/items.jsonl`이 있고 대응 `*.sqlite3`가 없거나 미완이면 import. **FIFO와 동일한 marker 테이블 방식** 사용(이번에 도입한 `fifo_meta.legacy_migrated` 패턴 재사용) — partial migration 안전.
   - import 시 `token_count`는 새 TokenCounter로 recount(옛 chars//3 값 폐기).
   - import 완료 후 JSONL은 `*.jsonl.migrated`로 rename(즉시 삭제 대신, 안전망).

5. **`memory_state.json` 제거** (dead file).

**검증**:
- 단위: append→search→tail round-trip이 SQLite만으로 동작.
- 마이그레이션: 레거시 JSONL → SQLite import + recount + marker.
- 회귀: 기존 69개 테스트. 단 JSONL 폴백을 검증하던 테스트(`test_*_falls_back_to_jsonl_search`, `test_search_rebuilds_*`)는 **삭제 또는 재작성** 필요 — 폴백 자체가 사라지므로.

**규모**: 변경 ~−400줄(순감), 신규 베이스 클래스 ~150줄. 테스트 재작성 포함.

---

### Phase B — 저장 파일 통합 (우려 2 마무리)

**목표**: 워크스페이스당 memory 파일을 `memory.sqlite3` 1개 + `invocations.jsonl` 1개로.

1. **단일 `memory.sqlite3`에 테이블 통합**
   - `fifo_items`, `recall_items` + `recall_fts`, `archival_items` + `archival_fts`, `summaries`, `working` 를 한 DB 파일에.
   - 워크스페이스 격리는 파일 경로로 유지(`runs/<ws>/memory/memory.sqlite3`) — 기존 격리 모델과 호환.

2. **`summaries.jsonl` → `summaries` 테이블**, **`working_context.json` → `working` 테이블**
   - working은 이미 record 구조(`{id, text, source, confidence, tags, updated_at}`, PR-5)라 테이블 매핑이 자연스럽다.
   - `load_latest_summary`는 `SELECT ... ORDER BY created_at DESC LIMIT 1`.

3. **`store.py` 슬림화**
   - JSONL 헬퍼(`read_jsonl`, `read_jsonl_tail`, `write_jsonl_atomic`, `truncate`, `_read_tail_lines`) 제거 또는 `invocations.jsonl` 전용으로 축소.
   - `MemoryStore`는 경로 + DB 핸들 팩토리로 단순화.

**검증**: 워크스페이스 전환/생성 시 단일 파일 생성, 기존 흐름 회귀.

**규모**: 중간. `store.py` 256 → ~100줄, queue/working/summary 경로 조정.

> Phase B는 Phase A 이후 선택적. A만으로도 우려 1·2의 핵심(중복·폴백)은 해소된다. B는 "파일 1개"라는 미관·정합성 개선.

---

### Phase C — 인메모리 캐시 / connection 재사용 (우려 3, 추후)

**목표**: turn당 connect 8회 → 0회(재사용). I/O를 페이지 캐시로 흡수.

1. **Connection 재사용**
   - storage별 connection 1개 lazy open + 보유. `configure_workspace`(워크스페이스 전환) 시 close→reopen.
   - 부팅 시 1회: `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;`
   - 효과: 매 연산의 open/close 비용 제거 + SQLite 페이지 캐시 인메모리 유지.

2. **Thread-safety (필수 — bg flush thread 존재)**
   - `_bg_flush_worker`가 별도 thread에서 `self.queue`(FIFO) + `self.store`(summary)를 건드린다.
   - 옵션 (a): `check_same_thread=False` + storage별 `threading.Lock`으로 직렬화.
   - 옵션 (b): thread별 connection(`threading.local`). WAL이면 reader/writer 동시성 확보.
   - 권장: 단일 사용자·저빈도 flush이므로 **(a) lock 직렬화**가 단순·안전.

3. **선택적 in-memory 캐시 레이어**
   - 이미 있는 `_fifo_token_total`(토큰 합 캐시) 패턴을 확장.
   - working/latest-summary 같은 매 turn 읽히는 소량 데이터는 connection 재사용만으로 페이지 캐시 히트 → 별도 캐시 불필요할 수 있음. **먼저 측정 후 결정.**

**검증**:
- I/O 측정 재실행: prepare/commit connect 횟수 0 확인.
- bg flush와 메인 turn 동시 실행 시 race/lock 정상.
- 워크스페이스 전환 시 connection 누수 없음(이전 핸들 close).

**규모**: 중간. 주로 storage 베이스 + runtime의 lifecycle.

> 우려 3 본인이 "추후로 미뤄도 됨"으로 명시. Phase A/B 정리 후 측정값을 보고 착수. 4B~9B 단일 사용자에서 connect 0.1~1ms × 8 = ~수ms/turn이라 당장 치명적이진 않으나, 구조 부채는 분명.

---

## 4. 단계별 PR 요약

| Phase | 핵심 | 위험도 | 선행 | 순감 LOC(추정) |
|---|---|---|---|---|
| A | fallback 제거 + JSONL 미러 폐기 + 공통 베이스 | 중 | 없음 | ~−400 |
| B | 단일 `memory.sqlite3` 통합 | 중 | A | ~−150 |
| C | connection 재사용 + thread-safety + (선택)캐시 | 중 | A | +α(lifecycle) |

**최소 실행선**: Phase A. 우려 1·2의 핵심 해소.
**완결**: A→B→C 순차.

---

## 5. 리스크 및 트레이드오프

| 항목 | 내용 | 완화 |
|---|---|---|
| JSONL grep 편의 상실 | 디버그 시 텍스트 파일 직접 grep 불가 | `sqlite3 .dump` 또는 디버그 CLI 한 줄. 감사용 `invocations.jsonl`은 유지. |
| 마이그레이션 1회 필요 | 레거시 JSONL → SQLite | marker 테이블 방식(이미 검증)으로 partial 안전. JSONL은 즉시 삭제 말고 `.migrated` rename. |
| 기존 폴백 테스트 폐기 | 폴백이 사라지므로 관련 테스트 무의미 | 삭제/재작성 — round-trip·마이그레이션 테스트로 대체. |
| Connection 생명주기 | bg thread와 공유 | Phase C에서 lock 직렬화로 해결. A/B는 기존 connect-per-op 유지하므로 무관. |
| 실데이터 마이그레이션 부담 | — | **현재 `runs/`에 memory 디렉토리 0개**. 마이그레이션 코드는 안전망일 뿐, 실데이터 부담 없음. 지금이 정리 최적 시점. |

---

## 6. 결론

현재 구조의 3가지 문제(과도한 fallback / 분산된 저장 / 과도한 I/O)는 **"JSONL을 호환 로그로 영구 유지"** 라는 단일 결정에서 파생된다. 그 결정을 철회하고 **SQLite 단일 소스**로 가면:

- Phase A: fallback·중복 제거 (우려 1·2)
- Phase B: 파일 1개로 통합 (우려 2 마무리)
- Phase C: connection 재사용으로 I/O 제거 (우려 3)

가 순차적으로 해소된다. 실데이터가 아직 없으므로 마이그레이션 위험이 가장 낮은 지금이 착수 적기다.

다음 단계로 **Phase A의 구체 변경안(파일별 diff 설계 + 테스트 목록)** 을 작성할 수 있다. 진행 범위를 정해주면 그 PR 설계를 이어가겠다.
