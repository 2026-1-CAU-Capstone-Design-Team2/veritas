# fetch_webpage_tool

웹페이지를 가져와 **LLM 친화적인 Markdown 본문**으로 추출하는 tool입니다. AutoSurvey workflow의 `collect` 단계에서 검색 결과 URL마다 호출됩니다.

---

## 수집 전략: Crawl4AI 단일 경로

문서 수집은 **Crawl4AI의 HTTP 전용 크롤러 하나로만** 수행합니다. 별도의 fallback 추출기는 없습니다.

```text
Crawl4AI AsyncHTTPCrawlerStrategy (aiohttp 기반, 브라우저 없음)
   └ DefaultMarkdownGenerator + PruningContentFilter
   └ HTML → 구조 보존 Markdown (헤딩/리스트/표 유지, boilerplate 제거)
```

핵심 설계: **Crawl4AI로 수집 가능한 문서만 저장한다.** 어떤 URL을 Crawl4AI가 가져오지 못하면 그 URL은 `fetch_error`로 처리되고, AutoSurvey의 `collect` 루프는 그냥 다음 검색 결과로 넘어갑니다. 따라서 최종적으로 워크스페이스에 저장되는 모든 문서는 Crawl4AI가 수집한 것이고, 전부 깨끗한 Markdown으로 바로 저장됩니다 — `requests`+`BeautifulSoup` 휴리스틱 추출이나 raw_html→raw_text 변환 단계가 필요 없습니다.

> 이전 버전은 "Crawl4AI 1차 → BS4 2차 fallback" 이중 경로였습니다. fallback이 사라지면서 `_fetch_with_requests`, `html_document_preprocessing.py`(BS4 휴리스틱), `hints.py`는 제거되었습니다.

---

## 왜 HTTP 전용 전략인가

Crawl4AI는 기본적으로 Playwright 브라우저를 구동합니다. 과거 브라우저 기반 통합은 Windows에서 Playwright의 asyncio transport 정리가 끝나지 않아 별도 subprocess로 격리해야 했고, fetch마다 인터프리터 기동 비용이 들었습니다.

`AsyncHTTPCrawlerStrategy`는 브라우저를 띄우지 않고 `aiohttp`만 사용합니다. 따라서

- subprocess 불필요 → fetch당 기동 오버헤드 없음
- asyncio 정리 문제 없음 → `asyncio.run()`으로 **in-process** 실행
- crawl4ai import 비용은 프로세스당 1회만 발생

in-process 모듈: [`services/fetch_webpage_tool_funcs/crawl4ai_fetch.py`](../../services/fetch_webpage_tool_funcs/crawl4ai_fetch.py)

> HTTP 전용 전략은 JavaScript를 실행하지 않습니다. SPA처럼 본문을 JS로 그리는 페이지는 본문이 비어 나올 수 있고, 그러면 해당 URL은 `fetch_error`로 처리되어 수집 대상에서 제외됩니다.

---

## text / html 저장 방식

| 필드 | 내용 | 캡 |
| --- | --- | --- |
| `text` (요약 입력) | Crawl4AI가 변환한 clean Markdown (`fit_markdown` 우선, 과도하게 깎이면 `raw_markdown`) | `max_chars` |
| `html` | 원본 raw HTTP 응답 본문 — **보관(provenance)용, 절단 없음** | 없음 |

`html`은 lossy 변환의 입력이 아니라 단순 아카이브입니다. 나중에 더 나은 추출기로 재처리할 때 재-fetch 없이 쓸 수 있도록 원본을 온전히 보존합니다. `DocRecord.html_path`와 `is_invalid_document_record()`의 zero-byte 검사 호환성도 그대로 유지됩니다.

> `html`은 "HTTP 응답으로 받은 원본"일 뿐 **JS 렌더링 후의 DOM은 아닙니다**.

### `fit_markdown` vs `raw_markdown` 자동 선택

`PruningContentFilter`의 `fit_markdown`이 `raw_markdown` 대비 일정 비율(`_FIT_MIN_RATIO`, **25%**) 미만으로 줄어들거나 절대 길이가 `_FIT_MIN_CHARS`(500자) 미만이면, 필터가 본문까지 과하게 깎은 것으로 보고 `raw_markdown`을 사용합니다 — 그 외에는 de-chrome된 `fit_markdown`을 신뢰합니다. (초기값 45%는 chrome이 많은 뉴스/블로그에서 필터가 60~75%를 정상 제거한 깨끗한 본문까지 노이즈 raw로 되돌려, `clean_md`에 chrome이 남는 원인이었음. 실측상 복구 가능한 본문은 fit/raw≈0.27~0.40, 과깎임 페이지는 ≈0.04~0.05라 0.25가 그 사이 간격.)

`max_chars`(workflow 기본 25000)는 `text`에만 적용되는 상한입니다. 상한을 넘는 초장문은 `document_summarize_tool`이 손실 없이 처리합니다.

### 하드 타임아웃

Crawl4AI 경로에는 `timeout_sec + 15s`의 외곽 타임아웃이 걸려 단일 fetch가 workflow를 멈추지 않습니다.

---

## 동작 확인 (CLI 로그)

fetch 결과는 매번 CLI 로그에 출력됩니다.

```text
[fetch][crawl4ai][ok]     url=... content_type='text/markdown; extraction=crawl4ai:fit_markdown' text_chars=8123 html_chars=98412
[fetch][crawl4ai][failed] url=... (에러 사유)        # 수집 실패 → collect 루프가 다음 URL로 진행
```

- `content_type`의 `extraction=` 값으로 `fit_markdown` / `raw_markdown` 중 무엇이 쓰였는지 알 수 있습니다.
- `clean_md/<doc_id>.md`는 Crawl4AI 정제 Markdown(`#` 헤딩, `-` 리스트, `|` 표 — RAG 답변 근거이자 요약 입력), `corpus/raw_html/<doc_id>.html`은 원본 HTML 전체입니다.

---

## 인터페이스

### 입력 (`tool_schema.json`)

```text
url         (required)     가져올 페이지 URL
timeout_sec (default 15)   HTTP 타임아웃(초). Crawl4AI 경로에 timeout_sec + 15s 외곽 타임아웃 적용
max_chars   (default 25000) 추출 Markdown 텍스트(`text`)의 최대 글자 수
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
    html: str           # 원본 raw HTML (보관용, 절단 없음)
    content_type: str   # 예: "text/markdown; extraction=crawl4ai:fit_markdown"
```

수집 실패 시 `ToolResult(success=False, error=...)`를 반환하고, workflow의 `_fetch_one`이 이를 `fetch_error`로 처리합니다.

---

## 의존성 / 설치

```bash
pip install crawl4ai
```

- HTTP 전용 전략만 사용하므로 **`crawl4ai-setup`(브라우저 바이너리 다운로드)은 불필요**합니다.
- `crawl4ai`는 이제 **하드 의존성**입니다 (fallback 없음). 미설치 시 `fetch_with_crawl4ai()`가 명확한 에러를 반환하고 모든 fetch가 실패합니다.

---

## 관련 파일

```text
tools/fetch_webpage_tool/fetch_webpage_tool.py        이 tool 본체 (Crawl4AI 단일 경로)
services/fetch_webpage_tool_funcs/crawl4ai_fetch.py   Crawl4AI HTTP 전용 in-process fetch
workflows/autosurvey_workflow.py                      _fetch_one()에서 이 tool을 호출, 실패 시 다음 URL로 진행
```
