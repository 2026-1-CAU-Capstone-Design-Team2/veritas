# Memory SQLite 단일화 — Phase A / Phase B 구현 목록

**대상 브랜치**: `feat/59-memory`
**기준일**: 2026-05-30
**환경**: 로컬 Qwen3.5 4B~9B + llama-server, 단일 사용자
**역할**: 본 문서는 설계/작업 목록. 코드 수정은 codex 전담.
**선행 문서**: `docs/memory_sqlite_unification_proposal.md`

---

## 범위 요약

| Phase | 목표 | 결과물 |
|---|---|---|
| **A** | 이중 저장(JSONL 미러) 폐기 + fallback 전면 제거 + recall/archival 공통 베이스화 | SQLite가 recall/archival/fifo의 단일 소스 |
| **B** | 잔여 JSONL/JSON(summaries, working) 흡수 → 워크스페이스당 `memory.sqlite3` 1개 + `invocations.jsonl` 1개 | 저장 파일 10종 → 2종 |

각 작업은 `[A-n]` / `[B-n]` 식별자로 표기. 체크박스는 진행 추적용.

---

# Phase A — Fallback 제거 + JSONL 미러 폐기

## A-0. 전제 / 불변식

- **단일 소스 원칙**: recall/archival/fifo 데이터는 SQLite에만 존재. JSONL은 1회 마이그레이션 입력으로만 사용 후 폐기.
- **Fallback 금지**: `_sqlite_disabled` / `_search_jsonl` 류의 "조용한 폴백" 제거. SQLite 오류는 상위로 전파(혹은 단일 지점 로깅 후 raise).
- **마이그레이션 안전성**: FIFO에서 검증된 `*_meta.legacy_migrated` marker 방식 사용(부분 마이그레이션 내성).
- **현재 실데이터 0** (`runs/`에 memory 디렉토리 없음) → 마이그레이션은 안전망일 뿐.

## A-1. 공통 베이스 클래스 `FtsMemoryStore` 신설

**파일(신규)**: `services/memory_tools_funcs/external_context/fts_memory_store.py`

- [ ] recall/archival의 복붙 메서드를 베이스로 흡수:
  - `_connect()` — connection 생성(여기서는 기존 connect-per-op 유지, 재사용은 Phase C)
  - `_ensure_schema(conn)` — `{prefix}_items` 테이블 + `{prefix}_fts` 가상테이블
  - `_upsert_row(conn, row)` — items upsert + fts delete/insert
  - `_tail_sqlite(limit)`, `_search_sqlite(query, limit)`
  - `_fts_query(query)` (정적), `_sqlite_row_to_dict(row)` (정적)
  - `_append_sqlite(row)`
- [ ] 서브클래스가 주입하는 설정값을 추상 속성/생성자 인자로:
  - `table_name` (예: `recall_items` / `archival_items`)
  - `fts_name` (예: `recall_fts` / `archival_fts`)
  - `db_path` (store에서 해석: `recall_db_path` / `archival_db_path`)
  - `default_tier` (`"recall"` / `"archival"`)
- [ ] 공개 API:
  - `append(item: MemoryItem) -> None` (recall 명명) / `insert(item) -> None` (archival 명명) — 베이스는 `add(item)`로 통일하고 서브클래스가 alias 제공
  - `tail(limit=50) -> list[dict]`
  - `search(query, *, limit=5) -> list[dict]`
- [ ] **제거 대상(베이스에 포함하지 않음)**: `_sqlite_disabled`, `_search_jsonl`, `_ensure_sqlite_current`, `_rebuild_sqlite_from_rows`, `_jsonl_id_count`, `_synced_signature`, `_jsonl_signature`.

**근거**: 현재 `recall_storage.py`(279줄)와 `archival_storage.py`(278줄)는 테이블명만 다른 동일 코드. 각 27건의 fallback 식별자 참조.

## A-2. `RecallStorage` 재작성

**파일**: `services/memory_tools_funcs/external_context/recall_storage.py`

- [ ] `FtsMemoryStore` 상속, `table_name="recall_items"`, `fts_name="recall_fts"`, `db_path=store.recall_db_path`, `default_tier="recall"`.
- [ ] `append(item)`: JSONL 미러(`store.append_jsonl(recall_path, ...)`) 호출 **삭제**. SQLite만 기록.
- [ ] `search` / `tail`: 베이스 위임. JSONL 폴백 분기 삭제.
- [ ] 1회 마이그레이션 트리거: A-4 참조(첫 접근 시 `recall_storage.jsonl` → DB import).
- [ ] 최종 파일 크기 목표: 279줄 → ~30줄.

## A-3. `ArchivalStorage` 재작성

**파일**: `services/memory_tools_funcs/external_context/archival_storage.py`

- [ ] `FtsMemoryStore` 상속, `table_name="archival_items"`, `fts_name="archival_fts"`, `db_path=store.archival_db_path`, `default_tier="archival"`.
- [ ] `insert(item)`: JSONL 미러(`store.append_jsonl(archival_path, ...)`) 호출 **삭제**.
- [ ] `search` / `tail`: 베이스 위임. JSONL 폴백 삭제.
- [ ] 1회 마이그레이션 트리거: A-4.
- [ ] 최종 파일 크기 목표: 278줄 → ~30줄.

## A-4. 1회 마이그레이션 (marker 기반)

- [ ] 베이스 `FtsMemoryStore`에 `_meta` 테이블 추가:
  ```sql
  CREATE TABLE IF NOT EXISTS {prefix}_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)
  ```
- [ ] `_ensure_migrated()` — FIFO `_ensure_sqlite_from_legacy_if_needed` 패턴 재사용:
  1. `legacy_migrated == "1"` 이면 즉시 반환.
  2. 레거시 JSONL 없으면 marker만 set 후 반환.
  3. 있으면 같은 트랜잭션에서: 각 row `_upsert_row` + `token_count` **recount**(새 TokenCounter) + marker set + commit.
- [ ] import 완료 후 레거시 파일을 `recall_storage.jsonl.migrated` / `archival/items.jsonl.migrated`로 **rename**(즉시 삭제 금지 — 안전망).
- [ ] `token_count` recount는 `MemoryItem.token_count`를 재계산해 옛 `chars//3` 값을 폐기(이전 리뷰 잔여 항목 동시 해소).

> **주의**: 현재 recall/archival의 마이그레이션은 "row count 비교" 방식이라 부분 마이그레이션·외부 변경 시 재빌드. Phase A에서는 이를 **marker 방식으로 교체**한다. count 비교는 compaction이 없는 recall엔 동작하지만, 일관성·단순성을 위해 fifo와 동일 패턴으로 통일.

## A-5. `FifoStorage` 정리

**파일**: `services/memory_tools_funcs/main_context/fifo_storage.py`

- [ ] 이미 SQLite 단일 소스 + marker 마이그레이션 보유(이전 수정). 변경 최소.
- [ ] 베이스 `FtsMemoryStore`와 schema 패턴 정렬(선택): fifo는 FTS가 없고 `seq AUTOINCREMENT` 순서가 핵심이라 **베이스 상속 대신 독립 유지** 권장. 단 `_meta` 마이그레이션 패턴은 이미 동일하므로 명명만 정렬.
- [ ] import 후 `fifo_queue.jsonl` → `.migrated` rename 추가(현재는 rename 없이 무시만 함).

## A-6. `store.py` 정리 (Phase A 한정)

**파일**: `services/memory_tools_funcs/store.py`

- [ ] `memory_state.json` / `self.state_path` **제거** (dead, 참조 0 확인).
- [ ] recall/archival JSONL 경로(`recall_path`, `archival_path`)는 마이그레이션·rename 동안 필요하므로 **Phase A에서는 유지**, Phase B에서 제거.
- [ ] `append_jsonl`는 `invocations.jsonl` + summaries(아직 JSONL)에서 쓰이므로 유지.
- [ ] `read_jsonl` / `read_jsonl_tail` / `write_jsonl_atomic` / `truncate`: 마이그레이션 입력에만 남기고, recall/archival 소비 제거 후 사용처 재확인.

## A-7. 호출처 영향 점검

- [ ] `context_builder.build_messages` — `queue.recall.search()` / `archival.search()` 시그니처 유지(반환 dict 동일) → 변경 없음 확인.
- [ ] `llm_tools.py` — `recall_search` / `archival_insert` / `archival_search` 러너가 동일 메서드명 사용 → 변경 없음 확인.
- [ ] `runtime.py` — `self.recall` / `self.archival` 생성부 시그니처 유지.

## A-8. 테스트

- [ ] **삭제/재작성** (폴백 사라짐):
  - `test_recall_storage.py::test_*_falls_back_to_jsonl_search`
  - `test_recall_storage.py::test_search_rebuilds_*`
  - `test_archival_storage.py::test_insert_sqlite_failure_falls_back_to_jsonl_search`
  - `test_archival_storage.py::test_search_rebuilds_*`
  - `test_memory_followup_fixes.py::SearchScanTests::test_*_external_jsonl_change_still_triggers_rebuild` (signature 캐시 제거로 무의미)
- [ ] **유지/보강**:
  - round-trip: `add → search → tail` SQLite-only 동작.
  - 한국어 FTS 검색(`unicode61`) 동작.
  - `test_memory_followup_fixes.py::SearchScanTests::test_*_does_not_full_scan` → connect-per-op 유지 시 의미 재정의(스캔 0 → 폴백 제거로 자동 보장). Phase C에서 connect 0회로 강화.
- [ ] **신규**:
  - 마이그레이션: 레거시 JSONL → DB import + recount + marker + `.migrated` rename.
  - 부분 마이그레이션 내성: 빈 DB(스키마만) + 레거시 JSONL → 재import.
  - marker 존재 시 레거시 무시(재import 안 함).

## A-9. Phase A 산출물 / 수치 목표

- 저장 파일: `memory_state.json` 제거(−1). recall/archival JSONL은 `.migrated`로 비활성(런타임 미사용).
- 코드: recall 279 + archival 278 = 557줄 → 베이스 ~150 + 서브 각 ~30 = ~210줄 (**약 −350줄**).
- fallback 식별자 참조 54건 → 0.

---

# Phase B — 단일 `memory.sqlite3` 통합

## B-0. 전제

- Phase A 완료(recall/archival/fifo가 각자 SQLite 사용 중).
- 목표: 워크스페이스당 memory 파일을 **`memory.sqlite3` 1개** + **`invocations.jsonl` 1개**(감사 로그)로.
- 워크스페이스 격리는 파일 경로 유지(`runs/<ws>/memory/memory.sqlite3`).

## B-1. 단일 DB 파일로 테이블 통합

**파일**: `services/memory_tools_funcs/store.py`

- [ ] `MemoryStore`에 `db_path = memory_dir / "memory.sqlite3"` 단일 경로 추가.
- [ ] 기존 분리 경로 제거: `fifo_db_path`, `recall_db_path`, `archival_db_path`.
- [ ] 한 DB에 테이블 공존:
  - `fifo_items` (+ `idx_fifo_items_created_at`)
  - `recall_items` + `recall_fts`
  - `archival_items` + `archival_fts`
  - `summaries` (B-2)
  - `working` (B-3)
  - `migration_meta` (통합 marker)
- [ ] FIFO/recall/archival storage가 **같은 db_path**를 바라보도록 생성자 조정.

## B-2. `summaries.jsonl` → `summaries` 테이블

**파일**: `store.py` + `queue_manage.py`

- [ ] 스키마:
  ```sql
  CREATE TABLE IF NOT EXISTS summaries (
      id TEXT PRIMARY KEY,
      summary TEXT NOT NULL,
      created_at TEXT NOT NULL
  )
  ```
- [ ] `reset_fifo_with_summary` (queue_manage.py:164): `append_jsonl(summaries_path, ...)` → `INSERT INTO summaries`.
- [ ] `load_latest_summary` (store.py:237): `read_jsonl_tail` → `SELECT summary FROM summaries ORDER BY created_at DESC LIMIT 1`.
- [ ] `summaries.jsonl` → `.migrated` rename + 1회 import.

## B-3. `working_context.json` → `working` 테이블

**파일**: `store.py` + `working_context.py`

- [ ] working은 이미 record 구조(`{id, text, source, confidence, tags, updated_at}`)라 매핑 자연스러움.
- [ ] 스키마:
  ```sql
  CREATE TABLE IF NOT EXISTS working (
      id TEXT PRIMARY KEY,
      text TEXT NOT NULL,
      source TEXT NOT NULL,
      confidence REAL NOT NULL DEFAULT 1.0,
      tags_json TEXT NOT NULL DEFAULT '[]',
      updated_at TEXT NOT NULL,
      seq INTEGER  -- 순서 보존용(AUTOINCREMENT 또는 삽입 순서)
  )
  ```
- [ ] `store.py`의 working 헬퍼 교체:
  - `load_working_records()` → `SELECT * FROM working ORDER BY seq`
  - `save_working_records(records)` → 트랜잭션 내 전체 교체(delete-all + insert) 또는 upsert + 잔여 delete
  - `format_working_records()` (정적) 유지
  - `working_records_from_text()` 유지(마이그레이션/legacy 텍스트 변환)
  - `load_working_context()` 위임 유지
- [ ] `working_context.json` → `.migrated` rename + 1회 import (record / legacy content 양형 모두 처리, 기존 `load_working_records`의 호환 로직 재사용).
- [ ] `WorkingContextManager`는 `store` API만 호출하므로 내부 로직 변경 최소(저장 백엔드만 교체).

## B-4. `store.py` 슬림화

- [ ] JSONL 헬퍼 정리:
  - `read_jsonl`, `read_jsonl_tail`, `_read_tail_lines`, `write_jsonl_atomic`, `truncate` — recall/archival/summaries/working 소비 제거 후 **마이그레이션 입력 + invocations 전용**만 남김.
  - `invocations.jsonl`은 append-only 감사 로그로 유지 → `append_jsonl`만 존속.
- [ ] 경로 속성 제거: `fifo_path`/`recall_path`/`archival_path`/`summaries_path`/`working_path`는 **마이그레이션 단계에서만** 참조 → 마이그레이션 모듈로 이동 또는 `legacy_*` 접두어로 격리.
- [ ] 목표: store.py 249줄 → ~100줄.

## B-5. 마이그레이션 통합

- [ ] `migration_meta` 단일 테이블에 각 소스 마이그레이션 완료 플래그:
  `fifo_migrated`, `recall_migrated`, `archival_migrated`, `summaries_migrated`, `working_migrated`.
- [ ] 부팅/첫 접근 시 1회 실행. 각 레거시 파일 존재 + 미완료면 import → `.migrated` rename.
- [ ] Phase A에서 분리 DB(`fifo.sqlite3` 등)에 이미 import된 데이터가 있으면 → **분리 DB → 통합 DB 복사** 경로도 처리(Phase A를 거친 환경 호환). 실데이터 0이면 사실상 no-op.

## B-6. 호출처 영향 점검

- [ ] `runtime.configure_workspace` (runtime.py:68): storage 핸들 재생성 시 단일 db_path 기준으로 동작 확인.
- [ ] `context_builder` / `llm_tools` / `queue_manage`: 메서드 시그니처 불변 → 호출부 변경 없음 확인.

## B-7. 테스트

- [ ] **신규**:
  - 단일 `memory.sqlite3`에 5개 테이블 공존 + round-trip.
  - summaries 저장/조회(latest).
  - working 저장/조회/compaction(token cap)이 테이블 백엔드에서 동일 동작.
  - 통합 마이그레이션: 레거시 5종 파일 → 단일 DB import + 각 marker.
- [ ] **갱신**:
  - 기존 working/summary 테스트가 JSON/JSONL 경로 가정을 제거하고 테이블 경유로.
  - 워크스페이스 전환 시 단일 파일 생성 확인.

## B-8. Phase B 산출물 / 수치 목표

- 저장 파일: (Phase A 후) → **`memory.sqlite3` + `invocations.jsonl`** 2종.
  - 제거/통합: `fifo.sqlite3`, `recall.sqlite3`, `archival.sqlite3`, `summaries.jsonl`, `working_context.json` (+ Phase A의 `.migrated` 잔재 정리).
- 코드: store.py 249 → ~100줄 (**약 −150줄**).

---

# 공통 — 리스크 / 검증 체크리스트

- [ ] **회귀**: 기존 memory 테스트 전체 통과(폴백 테스트는 A-8 기준 정리 후).
- [ ] **마이그레이션 안전**: 부분 마이그레이션(중단/빈 DB) 재시도 안전, 레거시 즉시 삭제 금지(`.migrated`).
- [ ] **워크스페이스 격리**: 전환/생성 시 파일·핸들 정상, 누수 없음.
- [ ] **thread-safety**: Phase A/B는 connect-per-op 유지 → bg flush thread와 충돌 없음(각 연산 독립 connection). connection 재사용은 **Phase C**에서 lock과 함께 도입.
- [ ] **디버그성**: JSONL grep 상실 → `sqlite3 .dump` 또는 디버그 CLI 안내. `invocations.jsonl` 유지로 호출 감사 가능.
- [ ] **실데이터 0**: 현재 `runs/`에 memory 디렉토리 없음 → 마이그레이션 실패 시 데이터 손실 위험 없음. 착수 적기.

---

# 작업 순서 권장

```
A-1 (베이스) → A-2/A-3 (recall/archival 재작성) → A-4 (마이그레이션)
            → A-5 (fifo 정렬) → A-6 (store 1차 정리) → A-7 (호출처) → A-8 (테스트)
B-1 (단일 DB) → B-2 (summaries) → B-3 (working) → B-5 (통합 마이그레이션)
            → B-4 (store 슬림화) → B-6 (호출처) → B-7 (테스트)
```

각 묶음은 독립 PR로 분리 가능. 최소 가치 전달선은 **A 완료**(이중 저장·폴백 종료). B는 파일 단일화 마무리.
