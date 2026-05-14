# fetch_webpage_tool

웹페이지를 가져와 **LLM 친화적인 본문 텍스트**로 추출하는 tool입니다. AutoSurvey workflow의 `collect` 단계에서 검색 결과 URL마다 호출됩니다.

---

## 무엇이 바뀌었나

기존에는 `requests` + `BeautifulSoup` 휴리스틱으로 HTML body 일부를 잘라서 텍스트로 변환했습니다. 이 방식은

- 표(`<table>`), 정의 리스트, 캡션 등을 통째로 누락하고
- 고정된 selector/heuristic으로 본문을 고르다 보니 사이트마다 추출 품질 편차가 크며
- 잘라온 텍스트를 그대로 요약에 넘겨 요약 품질이 입력 품질에 종속

되는 문제가 있었습니다.

이제 **Crawl4AI의 HTTP 전용 크롤러**를 1차 경로로 사용합니다.

```text
1차: Crawl4AI AsyncHTTPCrawlerStrategy (aiohttp 기반, 브라우저 없음)
       └ DefaultMarkdownGenerator + PruningContentFilter
       └ HTML → 구조 보존 Markdown (헤딩/리스트/표 유지, boilerplate 제거)
2차(fallback): requests + BeautifulSoup 휴리스틱 추출
       └ crawl4ai 미설치 또는 특정 URL에서 1차 실패 시에만 동작
```

---

## 왜 HTTP 전용 전략인가

Crawl4AI는 기본적으로 Playwright 브라우저를 구동합니다. 과거 브라우저 기반 통합은 Windows에서 Playwright의 asyncio transport 정리가 끝나지 않아 별도 subprocess(`crawl4ai_fetch_worker.py`)로 격리해야 했고, fetch마다 인터프리터 기동 비용이 들었습니다.

`AsyncHTTPCrawlerStrategy`는 브라우저를 띄우지 않고 `aiohttp`만 사용합니다. 따라서

- subprocess 불필요 → fetch당 기동 오버헤드 없음
- asyncio 정리 문제 없음 → `asyncio.run()`으로 **in-process** 실행
- crawl4ai import 비용은 프로세스당 1회만 발생

`crawl4ai_fetch_worker.py`(subprocess + 브라우저)는 제거되고, in-process 모듈 [`services/fetch_webpage_tool_funcs/crawl4ai_fetch.py`](../../services/fetch_webpage_tool_funcs/crawl4ai_fetch.py)로 대체되었습니다.

> JavaScript로 본문을 렌더링하는 SPA 페이지는 HTTP 전용 전략으로 본문이 비어 나올 수 있습니다. 이 경우 자동으로 2차 fallback이 시도되고, 그래도 실패하면 해당 URL은 `fetch_error`로 처리됩니다.

---

## raw_html / raw_text 처리 방식 변경

| 항목 | 이전 | 현재 |
| --- | --- | --- |
| `text` (요약 입력) | BS4 휴리스틱으로 추출한 본문 일부 | Crawl4AI가 변환한 clean Markdown (`fit_markdown` 우선) |
| `html` | 추출한 main node의 HTML, `max_chars` 캡 | 원본 raw HTML **전체 — 절단 없음**, 보관(provenance)용 |
| HTML→텍스트 변환 | 직접 구현한 lossy 휴리스틱 | Crawl4AI에 위임 |

`html` 필드는 더 이상 **lossy 변환의 입력이 아니라** 단순 아카이브입니다. 따라서 **절단하지 않고 원본 HTTP 응답 본문을 그대로 저장**합니다 — 나중에 더 나은 추출기로 재처리할 때 재-fetch 없이 쓸 수 있도록 원본을 온전히 보존합니다. (`text`만 `max_chars`로 캡됩니다.) `DocRecord.html_path`와 `is_invalid_document_record()`의 zero-byte 검사 호환성도 그대로 유지됩니다.

> 단, HTTP 전용 전략은 JS를 실행하지 않으므로, `html`은 "HTTP 응답으로 받은 원본"을 온전히 담을 뿐 **JS 렌더링 후의 DOM은 아닙니다**. SPA처럼 본문을 JS로 그리는 페이지는 원본 HTML 자체에 본문이 없을 수 있습니다.

---

## "loss 없이" 보장 장치

- **`fit_markdown` vs `raw_markdown` 자동 선택**: `PruningContentFilter`의 `fit_markdown`이 `raw_markdown` 대비 일정 비율(`_FIT_MIN_RATIO`, 45%) 미만으로 줄어들면 필터가 과하게 깎은 것으로 보고 `raw_markdown`을 사용합니다. 노이즈만 제거하고 본문은 보존합니다.
- **이중 경로**: 1차(crawl4ai)가 특정 URL에서 실패하면 2차(BS4)를 자동 시도합니다.
- **하드 타임아웃**: Crawl4AI 경로에는 `timeout_sec + 15s`의 외곽 타임아웃이 걸려 단일 fetch가 workflow를 멈추지 않습니다.
- **raw HTML 전체 보존**: `html`은 절단 없이 저장되므로, 추출 결과가 미흡해도 원본에서 언제든 재처리할 수 있습니다.

`max_chars`(workflow 기본 25000)는 `text`에만 적용되는 상한입니다. Crawl4AI Markdown은 노이즈가 제거되어 같은 글자 수에 담기는 정보 밀도가 높습니다. 상한을 넘는 초장문은 `document_summarize_tool`이 손실 없이 처리합니다(대부분 모델 context 안에서 단일 패스, 정말 큰 경우만 map-reduce 안전망).

---

## crawl4ai가 실제로 작동하는지 확인하는 법

fetch는 어떤 경로를 탔는지 CLI 로그에 매번 출력합니다.

```text
[fetch][crawl4ai] ok url=... content_type='text/markdown; extraction=crawl4ai:fit_markdown' text_chars=8123 html_chars=98412
[fetch][crawl4ai] failed -> fallback (...)        # 1차 실패 → 2차 시도
[fetch][bs4-fallback] ok url=... text_chars=...   # 2차(BS4)로 성공
[fetch][crawl4ai] unavailable (not installed) ... # crawl4ai 미설치
```

- `[fetch][crawl4ai] ok` 가 보이면 Crawl4AI 경로가 정상 동작 중입니다.
- `content_type`의 `extraction=` 값으로 `fit_markdown`/`raw_markdown` 중 무엇이 쓰였는지 알 수 있습니다.
- `corpus/raw_text/<doc_id>.txt`를 열어 보면, Crawl4AI 경로일 때는 Markdown 형식(`#` 헤딩, `-` 리스트, `|` 표)이 보이고, BS4 fallback일 때는 평문 문단만 보입니다.
- `corpus/raw_html/<doc_id>.html`은 원본 HTML 전체입니다(절단 없음).

---

## 인터페이스

### 입력 (`tool_schema.json`)

```text
url         (required)  가져올 페이지 URL
timeout_sec (default 15) HTTP 타임아웃(초)
max_chars   (default 25000) 추출 Markdown 텍스트의 최대 글자 수
```

### 출력 (`ToolResult.data` = `FetchedDocument`)

```python
@dataclass
class FetchedDocument:
    title: str          # 페이지 제목
    url: str            # 정규화된 요청 URL (arxiv /abs/ → /html/ 변환 포함)
    final_url: str      # 리다이렉트 후 최종 URL
    domain: str         # final_url의 도메인
    text: str           # 추출된 Markdown 본문 (요약 입력)
    html: str           # 원본 raw HTML (보관용)
    content_type: str   # 예: "text/markdown; extraction=crawl4ai:fit_markdown"
```

`content_type`의 `extraction=` 접미사로 어떤 경로/변형이 쓰였는지 추적할 수 있습니다
(`crawl4ai:fit_markdown`, `crawl4ai:raw_markdown`, 또는 fallback의 원본 `Content-Type`).

---

## 의존성 / 설치

```bash
pip install crawl4ai
```

- HTTP 전용 전략만 사용하므로 **`crawl4ai-setup`(브라우저 바이너리 다운로드)은 불필요**합니다.
- `crawl4ai`가 설치되어 있지 않으면 `fetch_with_crawl4ai()`가 `None`을 반환하고, tool은 자동으로 2차 BS4 경로로 동작합니다. 즉 설치 전에도 파이프라인은 깨지지 않습니다.

---

## 관련 파일

```text
tools/fetch_webpage_tool/fetch_webpage_tool.py     이 tool 본체 (1차/2차 경로 분기)
services/fetch_webpage_tool_funcs/crawl4ai_fetch.py  Crawl4AI HTTP 전용 in-process fetch
services/fetch_webpage_tool_funcs/html_document_preprocessing.py  2차 fallback용 BS4 휴리스틱
workflows/autosurvey_workflow.py                   _fetch_one()에서 이 tool을 호출
```
