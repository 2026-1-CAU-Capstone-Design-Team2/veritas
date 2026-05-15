# document_summarize_tool

각 문서의 **정제 Markdown(`clean_md/<doc_id>.md`)** 을 읽어 **문서별 요약**(`summary/doc_*.md`)과 **batch 요약**(`summary/batch_*.md`)을 만드는 tool입니다. AutoSurvey workflow가 호출합니다.

---

## per-doc 요약과 batch 요약 — clean_md의 두 독립 소비자

per-doc 요약과 batch 요약은 **체인이 아니라, 같은 `clean_md`를 각각 읽는 독립 소비자**입니다.

| | 입력 | 출력 | 호출 시점 | 용도 |
| --- | --- | --- | --- | --- |
| per-doc 요약 | `clean_md/<id>.md` | `summary/doc_*.md` | 조사 **종료 시 1회** (`summarize_docs=True`) | 출처 카드 / 인용 / 검증 UI |
| batch 요약 | `clean_md/<id>.md` ×N | `summary/batch_*.md` | 수집 **루프 안** (`rebuild_batches=True`) | gap 분석 → replan, 최종 보고서 입력 |

- **batch 요약은 per-doc 요약이 아니라 clean_md를 직접 읽습니다.** 요약-of-요약으로 인한 이중 손실을 피해, gap 분석이 본문 깊이를 그대로 보게 합니다.
- **per-doc 요약은 수집 루프의 임계 경로에서 빠집니다.** replan에 관여하지 않는 UX 디스크립터이므로, 루프 안에서 매 사이클 돌리면 수렴만 느려집니다. 그래서 루프가 끝난 뒤 전체 문서를 한 단계로 요약하되, 진행 상황은 문서별로 실시간 보고합니다(아래 `progress_callback`).
- `_read_batch_documents()`는 각 clean_md를 `single_pass_budget // batch_size`로 캡해 batch 한 건이 모델 context를 넘지 않게 합니다.

---

## 무엇이 바뀌었나 (per-doc 요약 경로)

기존에는 모든 문서를 길이와 무관하게 `text[:max_context]`로 **하드 절단**한 뒤 한 번에 요약했습니다. `max_context`(기본 16384자)를 넘는 문서는 뒷부분이 통째로 버려졌습니다.

이제 문서 길이에 따라 두 경로로 분기하되, **단일 패스 임계치를 모델의 실제 context window에서 산출**합니다.

```text
len(text) <= single_pass_budget   → 단일 패스 (fetch한 markdown을 그대로 1콜 요약)
len(text) >  single_pass_budget   → map-reduce (안전망)
     map:    문서를 overlap 청크로 분할 → 청크별 "노트" 추출 (free-form text)
     reduce: 노트 전체를 DOC_SUMMARY_PROMPT와 동일한 JSON 스키마로 합성
```

핵심: **절단 없이, 그리고 대부분의 문서는 청킹 없이 단일 패스**로 처리됩니다.

---

## single_pass_budget — 왜 더 이상 16384자가 아닌가

기존 `max_context=16384`는 **글자 수** 기준으로, 웬만한 길이의 일반 기사도 이 값을 넘겨 map-reduce가 불필요하게 자주 발동하고 그만큼 느렸습니다. 16384자는 토큰으로 환산하면 5천 토큰 안팎인데, 실제 로컬 llama-server의 context window는 보통 그보다 훨씬 큽니다.

이제 `LLMClient`가 시작 시 llama-server `/props`에서 `n_ctx`(context window, 토큰)를 읽어 옵니다. `document_summarize_tool`은 이 값으로 단일 패스 예산을 산출합니다.

```text
single_pass_budget = clamp(
    max( max_context, int(n_ctx * _CHARS_PER_TOKEN * _INPUT_CONTEXT_FRACTION) ),
    하한 2000자, 상한 _MAX_SINGLE_PASS_CHARS(200000자)
)

_CHARS_PER_TOKEN        = 2.5   # 한/영 혼합 기준 보수적 환산 비율
_INPUT_CONTEXT_FRACTION = 0.5   # window의 절반만 입력 본문에 사용
                                # (나머지는 system prompt + 생성 요약 몫)
```

예: `n_ctx = 143872` → `single_pass_budget ≈ 179840자`. `fetch_webpage_tool`이 텍스트를 25000자로 캡하므로, 수집된 문서는 **사실상 항상 단일 패스**로 처리됩니다 — map-reduce는 거의 발동하지 않습니다.

`n_ctx`를 읽지 못하면(예: llama-server가 아닌 백엔드) `max_context`로 폴백하여 기존 동작을 그대로 유지합니다.

요약 시작 시 CLI에 다음이 한 번 출력되어 현재 예산을 확인할 수 있습니다.

```text
[summarize] single-pass budget=179840 chars (n_ctx=143872); documents above this size use chunked map-reduce
```

---

## map-reduce — 이제는 "안전망"

단일 패스 예산을 context window에서 산출하므로, map-reduce는 **문서가 모델 context에 정말로 안 들어갈 때만** 발동하는 안전망 역할입니다. 발동 시 CLI 로그:

```text
[summarize][map-reduce] doc_id=003 chunks=N chars=...
```

### 설계 의도

- **청크 노트는 JSON이 아니라 free-form text**: 로컬 4-9B 모델은 긴 입력에서 strict JSON을 안정적으로 못 지킵니다. map 단계(청크별)는 자유 형식 텍스트 노트로 받고, **strict JSON은 reduce 단계에서 단 한 번만** 요구합니다. reduce 입력은 원문이 아니라 압축된 노트라 작은 모델도 스키마를 지키기 쉽습니다.
- **청크 분할**: `_chunk_text()`는 `single_pass_budget` 크기, `budget // 10`(최소 200자) overlap으로 분할하되, 각 청크 끝을 윈도우 마지막 1/5 구간의 문단(`\n\n`) → 줄(`\n`) → 문장(`. `) 경계에서 끊어 문장 중간 절단을 피합니다.
- **`_MAX_DOC_CHUNKS`(16)**: 비정상적으로 큰 입력에 대한 안전 상한. 도달하더라도 남은 꼬리를 마지막 청크로 접어 넣어 손실 없이 처리합니다.

---

## "loss 없이" 보장 장치

- **절단 제거**: 긴 문서도 잘리지 않고 단일 패스(context에 들어가면) 또는 청크 전체 커버(안 들어가면)로 처리됩니다.
- **청크 캡 초과 시 꼬리 보존**: `_MAX_DOC_CHUNKS` 도달 시에도 남은 텍스트를 마지막 청크로 추가합니다.
- **reduce 실패 fallback**: reduce JSON 파싱이 끝내 실패하면 문서를 통째로 잃지 않고, `_payload_from_notes()`가 청크 노트에서 직접 요약 payload를 조립합니다(`reliability_notes`에 자동 조립 사실 표기).
- **노트 과대 시 안전 절단**: 합쳐진 노트가 예산을 넘으면 노트 단계에서만 절단합니다 — 원문이 아니라 이미 압축된 노트라서 손실 영향이 작습니다.

---

## 인터페이스

### 입력 (`tool_schema.json`)

```text
overwrite       (default false) 기존 문서/batch 요약을 덮어쓸지 여부
doc_ids         (optional)      cycle 범위 문서 ID 목록. batch 요약을 이 문서들로 한정
rebuild_batches (default true)  batch 요약을 (재)생성할지 — clean_md를 직접 읽음
summarize_docs  (default true)  per-document 요약(summary/doc_*.md) 생성 여부
```

workflow의 `run_summarize(phase=...)`가 이 플래그들을 설정합니다:
`phase="batch"` → `summarize_docs=False, rebuild_batches=True` (수집 루프),
`phase="per_doc"` → `summarize_docs=True, rebuild_batches=False` (조사 종료 시),
`phase="all"` → 둘 다 (standalone `--phase summarize`).

### per-doc 진행 이벤트 (`progress_callback`)

`run()`은 `tool_schema.json`에 없는 **런타임 kwarg** `progress_callback`도 받습니다(기본 `None`). per-doc 요약 루프는 문서당 LLM 1콜로 도는 조사의 긴 꼬리 구간이라, 콜백이 주어지면 루프가 각 문서를 **시작할 때** `"doc_start"`, **끝낼 때** `"doc_summarized"` / `"doc_failed"` 이벤트를 문서별로 즉시 흘려보냅니다 — `callback(kind, doc_id=..., title=..., [reason=...])`.

workflow(`_on_summarize_progress`)는 이를 progress 이벤트로 변환하므로, 진행률 막대가 per-doc 구간에서 50%에 멈췄다 92%로 튀지 않고 **문서마다 전진**하고, 우측 `doc_*.md` 출처 카드도 모든 요약이 끝날 때까지 기다리지 않고 **요약이 끝나는 즉시 하나씩 활성화**됩니다. 콜백은 UX 사이드 채널이므로 호출이 실패해도 요약은 중단되지 않습니다(`_notify_progress`가 예외를 삼킴). `progress_callback=None`이면 동작은 종전과 동일합니다.

### 출력 (`ToolResult.data`)

```text
summarized_doc_ids / skipped_existing_doc_ids / skipped_invalid_doc_ids /
skipped_duplicate_doc_ids / skipped_not_in_cycle_doc_ids / failed_doc_ids
failed_documents = [{"docId", "title", "reason"}, ...]
batch_result = {"batch_files": [...], "count": N}
```

per-document 요약은 동일한 스키마(`DOC_SUMMARY_PROMPT`의 JSON)를 따르므로 `_render_doc_summary_from_record()`는 그대로 동작합니다.

---

## 생성자 파라미터

```python
DocumentSummarizeTool(
    schema,
    llm,
    run_store_service,
    batch_size=5,       # batch 요약 1건에 묶는 문서 수
    max_context=16384,  # single_pass_budget의 하한(floor) 겸 n_ctx 미검출 시 폴백값
    json_retries=2,     # ask_json 재시도 횟수 (단일 패스/reduce 공통)
)
```

`max_context`는 이제 **하드 임계치가 아니라 하한**입니다. 실제 단일 패스 예산은 `llm.n_ctx` 기반으로 산출되며, `max_context`보다 작아지지 않습니다. `--max-context`를 크게 주면 그 값이 하한으로 작동하고, 주지 않아도 모델 capability에 맞춰 자동으로 커집니다.

---

## 관련 프롬프트 (`core/prompts.py`)

```text
DOC_SUMMARY_PROMPT          단일 패스 요약 (JSON)
DOC_CHUNK_NOTES_PROMPT      map 단계: 청크별 노트 추출 (free-form text)
DOC_SUMMARY_REDUCE_PROMPT   reduce 단계: 노트 → 문서 요약 (DOC_SUMMARY_PROMPT와 동일 스키마)
BATCH_SUMMARY_PROMPT        batch 요약 (markdown)
```

---

## 관련 파일

```text
tools/document_summarize_tool/document_summarize_tool.py  이 tool 본체
llm/llama_server_llm.py                                   n_ctx 검출 (_detect_n_ctx)
core/prompts.py                                           위 4개 프롬프트
workflows/autosurvey_workflow.py                          run_summarize()에서 호출
services/run_store_tool_funcs/run_store_service.py        문서/batch 요약 read/write
```
