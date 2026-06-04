# Codex-Claude Collaboration Instructions

## Purpose
이 문서는 동일 코드베이스에서 Codex가 설계/검토자 역할을 맡고, Claude Code가 구현자 역할을 맡는 협업 절차를 정의한다. Codex는 사용자의 요구사항을 구현 가능한 체크리스트와 Claude용 지시문으로 분해하고, 구현 완료 후 `git diff`를 기준으로 아키텍처 정합성, 불필요한 의존성, 구현 선택의 적절성, 과도한 추상화를 검토한다.

## Current Task: final.md Citation Link Popup

### Goal
`DocumentPage`의 "요약" 섹션에서 `runs/<workspace>/final.md`를 Markdown preview로 보여줄 때 `[doc_000]` 같은 인용 marker를 클릭 가능한 링크로 렌더링한다. 사용자가 인용을 클릭하면 해당 `clean_md/<id>.md` 원문에서 관련 문장/단락을 찾아 작은 popup으로 보여주고, 근거 문장을 하이라이트한다. popup은 main UI를 다시 클릭하면 자동으로 닫혀야 한다. 추가 LLM call은 만들지 않는다.

### Key Design Decision
`final.md` 생성 단계에서 sentence span을 새로 저장하지 않는다. 이미 batch summary와 final report에 `[doc_<id>]` marker가 있으므로, UI 클릭 시점에 citation 주변 claim text를 API로 보내고, API가 해당 문서의 `clean_md/<id>.md`에서 결정론적 lexical matching으로 가장 가까운 문장/단락을 찾는다. 이는 OpenAI/local LLM 양쪽에서 추가 비용 없이 동작하며, 기존 AutoSurvey pipeline을 건드리지 않는다.

### Implementation Checklist for Claude Code
- [ ] `api/services/documents_service.py` 또는 새 `api/services/document_citation_service.py`에 citation 조회 로직을 추가한다.
- [ ] `workspaceId`와 `docId`를 안전하게 정규화한다. `doc_000`/`000` 입력을 허용하되 path traversal은 금지하고, 실제 파일은 `runs/<workspace>/clean_md/<id>.md`만 읽는다.
- [ ] `summary/index.json`에서 title, url, final_url, domain metadata를 찾아 popup에 반환한다.
- [ ] `clean_md/<id>.md`를 paragraph/sentence 단위로 나누고 Markdown noise를 가볍게 제거한다.
- [ ] 클릭된 citation 주변 claim text와 각 source sentence를 LLM 없이 scoring한다. 우선 exact substring, 그 다음 숫자/영문/한글 token overlap을 사용한다.
- [ ] API 응답에 `docId`, `title`, `url`, `domain`, `claim`, `match.text`, `match.paragraphText`, `match.paragraphIndex`, `match.sentenceIndex`, `match.score`, `match.confidence`를 포함한다.
- [ ] `api/api_routes/documents.py`에 `GET /api/v1/documents/{workspaceId}/citations/{docId}` endpoint를 추가한다. query param은 `claim`을 사용한다.
- [ ] `frontend/controllers/agent_controller.py`에 `get_document_citation(workspace_id, doc_id, claim)` wrapper를 추가한다.
- [ ] `frontend/ui/pages/document_page.py`에서 요약 viewer를 링크 클릭 처리가 가능한 widget으로 바꾼다. `QTextBrowser`를 우선 사용하고 `setOpenExternalLinks(False)` 및 `anchorClicked`를 연결한다.
- [ ] `final.md` 원문은 수정하지 말고, 렌더링 직전에 `[doc_000]` marker를 custom link로 변환한다. 예: `[doc_000](veritas-citation://doc_000?claim=<encoded>)`.
- [ ] citation 주변 claim text는 marker가 있는 같은 문장 또는 같은 markdown line에서 추출한다. 너무 길면 500자 안팎으로 자른다.
- [ ] popup은 `Qt.Popup` flag를 가진 작은 `QFrame`/`QDialog`로 구현한다. 최대 크기는 대략 520x360 이하로 제한하고, focus out 또는 main UI 클릭 시 자동 닫히게 한다.
- [ ] popup 본문은 source metadata, claim, highlighted source paragraph를 표시한다. `<span style="background-color: #fff3a3">...</span>` 같은 안전한 HTML escape 기반 하이라이트를 사용한다.
- [ ] Qt rich text가 pill-style anchor background/border-radius를 안정적으로 지원하면 연회색 pill 형태를 적용하고, 불안정하면 파란색 hyperlink text로 유지한다.
- [ ] matching 실패 또는 낮은 score일 때도 crash하지 말고 "가장 가까운 원문 후보" 또는 "원문 위치를 확정하지 못했습니다"를 표시한다.

### Architecture Constraints
- `frontend/`는 파일 시스템에서 `runs/`를 직접 읽지 않는다. 반드시 `AgentController -> HTTP API -> api/services` 경로를 사용한다.
- `api/api_routes/documents.py`는 얇은 route wrapper로 유지한다. 파일 읽기, matching, metadata 구성은 service 함수에 둔다.
- AutoSurvey의 `final_report_tool`, `document_cleanup_tool`, `document_summarize_tool`에는 필요 없는 변경을 하지 않는다. 특히 추가 LLM call을 만들지 않는다.
- citation matching은 `services/proactive/`나 verification pipeline에 섞지 않는다. 이 기능은 문서 요약 UI의 source-preview capability이다.
- `final.md`에 저장된 markdown 자체는 그대로 둔다. 링크화는 presentation concern으로 처리한다.

### Suggested Matching Algorithm
1. Normalize claim: citation markers, Markdown bullets, heading markers, links, punctuation noise를 제거하고 lowercase/tokenize한다.
2. Split source:
   - paragraph: blank-line boundary
   - sentence: `.`, `?`, `!`, `다.`, `요.`, newline/list item boundary 기반의 lightweight split
3. Score:
   - exact normalized substring이면 high confidence
   - shared numeric tokens에 가중치
   - shared named/technical tokens 및 Korean token overlap에 가중치
   - keyword list가 아니라 길이, 숫자, token 밀도 같은 일반 신호만 사용한다.
4. Return top candidate. threshold 미만이면 confidence=`low`로 반환한다.

### Tests Required
- [ ] `tests/test_document_citations.py`: service가 `summary/index.json` + `clean_md/<id>.md`에서 metadata와 best match를 반환하는지 검증한다.
- [ ] 같은 테스트에서 `doc_000`과 `000` 입력 모두를 허용하고, `../` 같은 path traversal은 실패/빈 결과로 처리하는지 검증한다.
- [ ] exact quote match, paraphrased overlap match, no-match fallback을 각각 검증한다.
- [ ] `frontend` 쪽 linkification helper를 순수 함수로 분리했다면 `[doc_000]`이 custom citation href로 바뀌고 code fence 내부 marker는 바꾸지 않는지 검증한다.
- [ ] 최소 실행: `python -m unittest tests.test_document_citations -v`
- [ ] 가능하면 전체 실행: `python -m unittest discover tests`

### Report Back From Claude
- 변경한 파일 목록
- endpoint shape와 popup 동작 요약
- 추가 LLM call이 없다는 확인
- 실행한 테스트와 결과
- matching이 낮은 confidence일 때의 UX 처리

### Codex Post-Implementation Review Focus
- `git diff`에서 `frontend`가 직접 `runs/`나 `clean_md`를 읽지 않는지 확인한다.
- API route가 비대해지지 않고 service에 책임이 모였는지 확인한다.
- `final_report_tool` prompt/call이 불필요하게 바뀌지 않았는지 확인한다.
- citation matching이 새 class/factory/interface 과잉 없이 간단한 pure helper 중심인지 확인한다.
- popup 구현이 main UI 클릭 시 닫히며, UI thread를 blocking HTTP call로 멈추지 않는지 확인한다.
- raw source 전체를 불필요하게 frontend로 보내지 않고 필요한 paragraph/snippet만 반환하는지 확인한다.

## Follow-Up Task: Citation UX and Prompt-Only Citation Format Fixes

### User-Tested Problems
1. 기존 `[doc_000]` 인용이 클릭 링크로 바뀌면서 화면에는 `doc_000`만 보여 대괄호가 사라졌다.
2. `final.md`에는 `[doc_000]`뿐 아니라 bare `doc_000` 형태도 섞여 있어 일부 인용이 클릭 불가능하다.
3. 외부 API 조사 경로에서 source noise가 final report로 올라와 과도한/쓸모없는 citation이 생길 수 있다. 단, 이를 keyword 기반 deterministic filter로 해결하지 않는다.

### Required Fixes for Claude Code
- [ ] `frontend/citation_links.py`의 링크 label은 항상 화면에 `[doc_000]`처럼 대괄호를 포함해 보이게 한다. Markdown 링크는 `[\[doc_000\]](href)` 또는 `[[doc_000]](href)` 형태를 사용한다.
- [ ] bracketed marker와 bare marker를 모두 링크화한다. 예: `[doc_000]`, `doc_000`, `doc-000`, `doc000` 중 기존 service가 허용하는 형태는 모두 클릭 가능해야 한다.
- [ ] bare marker를 링크화할 때도 표시 label은 `[doc_000]`로 정규화한다. 사용자가 원문이 bare였는지 bracketed였는지 신경 쓰지 않게 만든다.
- [ ] 축약형 `doc7`, `doc_7`, `doc-7`도 허용한다면 표시 label과 href는 반드시 zero-padded canonical form인 `[doc_007]` / `doc_007`로 정규화한다. `FINAL_PROMPT`의 `[doc_NNN]` 규칙과 UI presentation이 충돌하면 안 된다.
- [ ] code fence, inline code span, 기존 Markdown link target, URL/path 내부의 `doc_000`은 링크화하지 않는다.
- [ ] `extract_claim_from_line()`은 bracketed와 bare marker를 모두 제거하고 claim을 만든다.
- [ ] `tests/test_document_citations.py`에 bracket 보존, bare marker linkify, bare marker claim stripping, inline/code/link-target 제외 케이스를 추가한다.
- [ ] `tests/test_document_citations.py`에 `doc7`/`doc_7`/`doc-7`가 모두 `[doc_007]`로 렌더되고 `doc_007` endpoint로 향하는지 검증하는 케이스를 추가한다.
- [ ] `ARCHITECTURE.md`의 "bare doc_003 제외" 결정 기록은 현재 요구사항과 충돌하므로 제거하거나 "bare marker도 presentation layer에서 bracket label로 정규화"로 고친다.
- [ ] `api/api_routes/documents.py`의 citation endpoint는 `async def`가 아니라 plain `def`로 둔다. citation lookup은 파일 I/O와 source sentence scan을 수행하므로 event loop에서 실행하면 안 된다.

### Boilerplate / Over-Citation Non-Goal
- [ ] 외부 API batch cleanup에 keyword 기반 deterministic stripping을 추가하지 않는다.
- [ ] 금지 예: 특정 단어, 라벨, 해시태그, "share", "footer", "related", "tags"류 문자열을 보고 paragraph를 삭제하는 rule list.
- [ ] 이유: 이런 필터는 언어/도메인/사이트별 표현에 의존하므로 일반화되지 않고 본문 손실 위험이 크다.
- [ ] source quality 개선이 필요하면 keyword list가 아니라 upstream extractor 개선, 구조적 파싱, source ranking, 또는 LLM prompt 지시처럼 일반화 가능한 접근만 검토한다.
- [ ] 기존 `raw_md -> clean_md` pass-through 동작을 바꾸려면, keyword-free 설계 근거와 회귀 테스트를 먼저 제시해야 한다.

### Prompt Guardrails
- [ ] `FINAL_PROMPT`에 citation format은 항상 `[doc_000]` bracketed form으로 쓰고 bare `doc_000`은 쓰지 말라는 규칙을 추가한다.
- [ ] `FINAL_PROMPT`에는 "실질적인 source claim을 뒷받침할 때만 citation을 붙인다"는 일반 원칙을 둘 수 있다. 특정 keyword 목록을 나열해 제거/무시 대상으로 삼지 않는다.
- [ ] prompt 변경은 기존 LLM call 수를 늘리지 않아야 한다.

### Verification
- [ ] `python -m unittest tests.test_document_citations -v`
- [ ] 가능하면 `python -m unittest discover tests`
- [ ] 수동 확인: bracketed `[doc_000]`와 bare `doc_000`가 모두 화면에서 `[doc_000]` 링크로 보이는지, 클릭 popup이 뜨는지 확인한다.

### Codex Review Notes from Current Diff
- `frontend/citation_links.py:31-74`는 bracketed marker만 처리하고 `return f"[{inner}]({href})"` 때문에 렌더링 label에서 대괄호가 사라진다.
- `ARCHITECTURE.md`의 "bare doc_003 제외" 구현 결정은 사용자 테스트 결과와 충돌한다.
- `tools/document_cleanup_tool/document_cleanup_tool.py:236-277`의 외부 API batch mode를 keyword 기반 deterministic cleanup으로 고치라는 이전 제안은 철회한다. 이 방향은 일반화되지 않으므로 앞으로 금지한다.
- 최신 리뷰 추가: `api/api_routes/documents.py`의 citation endpoint가 `async def`이면 동기 파일 읽기/문장 스캔이 FastAPI event loop를 막을 수 있으므로 plain `def`로 바꿔야 한다.
- 최신 리뷰 추가: `frontend/citation_links.py`가 `doc7`/`doc_7`/`doc-7`를 `[doc_7]`처럼 표시하면 `[doc_NNN]` canonical 규칙과 충돌한다. 허용한다면 `int(digits):03d`로 `[doc_007]`까지 정규화해야 한다.

## Pre-Implementation Checklist
Codex는 구현을 시작시키기 전에 다음 항목을 작성한다.

- 요구사항을 사용자 가치와 완료 조건 기준으로 재정의한다.
- 변경 범위를 파일/모듈 단위로 나눈다.
- `ARCHITECTURE.md` 기준으로 호출 경로와 책임 경계를 명시한다.
- Claude Code가 바로 구현할 수 있도록 단계별 체크리스트를 제공한다.
- 필요한 테스트 파일과 실행 명령을 지정한다.
- 금지 사항과 회귀 위험을 명확히 적는다.

## Claude Code Instruction Template
Claude Code에게 전달하는 지시문은 아래 구조를 따른다.

```markdown
## Goal
<사용자 요구사항을 한 문단으로 요약>

## Implementation Checklist
- [ ] <작업 1>
- [ ] <작업 2>
- [ ] <테스트 추가/수정>

## Architecture Constraints
- `frontend/`는 직접 코어를 호출하지 않고 `api/`를 HTTP로 호출한다.
- `api/api_routes/`는 얇게 유지하고 실제 로직은 `api/services/` 또는 `services/`에 둔다.
- 도메인 알고리즘은 `services/`, 호출 가능한 tool은 `tools/<name>_tool/`에 둔다.
- 공유 모델/프롬프트는 `core/`에 둔다.

## Verification
- `python -m unittest discover tests`
- 필요한 경우 특정 테스트: `python -m unittest tests.<module> -v`

## Report Back
- 변경한 파일 목록
- 실행한 테스트와 결과
- 남은 리스크 또는 의도적으로 변경하지 않은 범위
```

## Architecture Review Rubric
Codex는 Claude 구현 후 다음 기준으로 검토한다.

1. **호출 경로 정합성**: UI, API, 서비스, tool, DB 계층이 `ARCHITECTURE.md`의 책임 분리를 따른다. `frontend/`가 `services/`나 `core/`를 직접 호출하는 우회 경로를 만들지 않았는지 확인한다.
2. **유지보수성**: 라우터가 비대해지지 않았는지, 상태 소유자가 분산되지 않았는지, 싱글톤 런타임(`AgentRuntime`) 접근이 기존 패턴과 일치하는지 확인한다.
3. **Dead Code 및 Deprecated 의존성**: 사용되지 않는 함수, 중복 어댑터, 레거시 경로, production 코드의 `services/proactive/legacy_bandit/` import 여부를 확인한다.
4. **구현 선택의 적절성**: 더 단순한 기존 helper, repository, service, parser로 해결 가능한데 새 추상화를 만든 것은 아닌지 검토한다.
5. **과도한 추상화**: 단일 호출부를 위한 불필요한 class/factory/interface, 의미 없는 wrapper, 테스트만을 위한 production surface 확장을 지적한다.
6. **테스트 적합성**: `unittest` 규칙을 따르고, 파일명은 `test_<topic>.py`, 클래스명은 `<Topic>Tests(unittest.TestCase)`인지 확인한다.

## Diff Review Procedure
구현 완료 후 Codex는 다음 순서로 검토한다.

```powershell
git status --short
git diff --stat
git diff
python -m unittest discover tests
```

변경량이 크면 관련 파일별 diff를 나누어 읽는다. 리뷰 결과는 아래 형식으로 남긴다.

```markdown
## Required Fixes
- [severity] file:line - 문제와 수정 방향

## Design Feedback
- 아키텍처, 책임 분리, 단순화 가능성

## Dead Code / Dependency Notes
- 제거 또는 유지 판단

## Verification
- 실행한 테스트와 실패/통과 결과
```

## Repository-Specific Guardrails
- 긴 FastAPI 작업은 이벤트 루프를 막지 않도록 가능하면 `plain def`로 둔다.
- Proactive production 경로는 hard-coded keyword features와 `legacy_bandit` import를 금지한다.
- JSONL/JSON 영속화에는 raw document text를 저장하지 않는다.
- 파일 산출물 변경은 `services/run_store_tool_funcs/`, SQLite 스키마 변경은 `db/schema.py`를 우선 확인한다.
- 프롬프트 문자열은 관련 `core/prompts/` 모듈에 둔다.

## Final Review: Citation Link Popup

### Required Fixes
- 없음. 이전 리뷰에서 지적한 citation endpoint `plain def` 전환과 short doc id zero-padding은 구현되어 있다.

### Design Feedback
- 호출 경로는 `DocumentPage -> AgentController -> HTTP API -> api/services/document_citation_service.py`로 정리되어 있으며, `frontend/`가 `runs/` 또는 `clean_md/`를 직접 읽지 않는다.
- API 라우터는 얇은 wrapper로 유지되고, 파일 I/O, metadata lookup, lexical matching은 service에 모여 있다.
- `frontend/citation_links.py`는 Qt-free pure helper로 분리되어 테스트 가능성이 좋고, `final.md` 원문을 수정하지 않는 presentation-only 방식이다.
- `FINAL_PROMPT`는 `[doc_NNN]` canonical marker와 `## Source Notes` Markdown table을 강제한다. keyword-list 기반 boilerplate 제거는 추가되지 않았다.

### Dead Code / Dependency Notes
- 새 경로에서 불필요한 legacy import, deprecated dependency, extra LLM call, persisted span artifact는 발견되지 않았다.
- citation popup 구현은 `QTextBrowser`, `Qt.Popup`, `JobManager.run_detached`를 사용해 UI thread blocking과 과도한 abstraction을 피하고 있다.

### Verification
- `C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_document_citations -v` 통과: 21 tests.
- `C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_document_cleanup_modes -v` 통과: 12 tests.
- `C:\Users\asdf\.conda\envs\agent\python.exe -m unittest discover tests` 실패: 251 tests 중 13 errors, 1 skipped. 실패는 OpenAI factory SSL 초기화와 기존 RAG/chat memory 누락(`ChatAgent._append_history`, `collected`, `RAGService._format_recent_history`)으로, 이번 citation diff와 직접 관련 없는 기존 실패로 분류한다.

## Implementation Plan: External API Structural Cleanup

### Problem
외부 API cleanup batch mode는 현재 `raw_md -> clean_md`를 pass-through로 복사한다. 따라서 `document_summarize`의 batch summary와 최종 `final.md`가 boilerplate가 섞인 `clean_md`를 읽고, citation popup도 같은 noisy source에서 문장 후보를 찾는다. Local LLM per-doc mode는 LLM cleanup으로 boilerplate paragraph를 제거하므로 두 경로의 source quality가 달라진다.

### Recommended Direction
- 핵심 수정은 `tools/document_cleanup_tool/document_cleanup_tool.py`의 batch mode에서 `clean_md`를 raw pass-through가 아니라 `corpus/raw_html/<doc_id>.html` 기반 구조적 extraction 결과로 쓰는 것이다.
- `FINAL_PROMPT`만 수정하는 것은 늦다. final report tool은 원문이 아니라 `summary/batch_*.md`만 보기 때문에, boilerplate citation은 이미 batch summary 단계에서 들어간다.
- Prompt 보강은 보조 수단으로 `BATCH_DOC_METADATA_PROMPT`와 `BATCH_SUMMARY_PROMPT`에 적용한다. 단, 추가 LLM call은 만들지 않는다.

### Claude Implementation Checklist
- [ ] `services/document_cleanup_tool_funcs/html_body_extractor.py`를 추가한다.
- [ ] BeautifulSoup 기반으로 HTML에서 semantic non-content tags만 제거한다: `script`, `style`, `noscript`, `template`, `svg`, `nav`, `footer`, `aside`, `form` 등 HTML tag/role 구조를 기준으로 한다.
- [ ] 구현에서 특정 텍스트 키워드, class/id substring, 사이트별 selector list를 사용하지 않는다. 예: `tags`, `share`, `related`, `footer`, `광고` 같은 문자열 매칭으로 paragraph를 제거하지 않는다.
- [ ] body 후보는 `article`, `main`, `[role=main]`, 이후 `body` fallback 순서로 고르고, 필요하면 link-density/text-length 같은 language/domain-agnostic structural score만 사용한다.
- [ ] HTML extraction 결과를 markdown-ish text로 변환하되 headings, paragraphs, list items, code/pre, table rows 정도는 보존한다.
- [ ] extraction 결과가 너무 짧거나 비어 있으면 기존 `raw_md` fallback을 사용한다. 실패는 hard failure가 아니라 degraded path로 처리한다.
- [ ] `DocumentCleanupTool._run_batch_mode()`에서 `raw_text`를 그대로 `write_clean_md()`하지 말고, raw_html이 있으면 structural extraction 결과를 `clean_md`와 batch metadata input의 `bodies[doc_id]`에 사용한다.
- [ ] local per-doc cleanup path(`_process_record`, `_cleanup_with_retry`)는 변경하지 않는다.
- [ ] `core/prompts/cleanup.py`의 `BATCH_DOC_METADATA_PROMPT`와 `core/prompts/autosurvey.py`의 `BATCH_SUMMARY_PROMPT`에 "body evidence only; page chrome/social/footer/navigation is not evidence" 취지의 지시를 추가한다. 이것은 prompt guardrail이며 deterministic keyword filter가 아니다.
- [ ] `ARCHITECTURE.md`의 implementation log에 외부 API batch cleanup이 raw pass-through에서 structural HTML cleanup으로 바뀌었음을 기록한다.

### Tests Required
- [ ] `tests/test_document_cleanup_modes.py`의 기존 `test_clean_md_is_raw_passthrough` 기대값을 수정한다. raw_html이 없거나 extraction 실패 시 raw fallback을 검증하는 테스트로 바꾼다.
- [ ] raw_html에 `<nav>`, `<footer>`, `<aside>`와 `<article>` 본문이 있을 때 batch mode `clean_md`가 본문을 보존하고 semantic chrome tag 내용을 제거하는지 검증한다.
- [ ] batch metadata `ask_json` 입력에도 sanitized body가 들어가고 removed semantic tag text가 들어가지 않는지 검증한다.
- [ ] local per-doc mode의 LLM cleanup behavior가 그대로인지 기존 regression test를 유지한다.
- [ ] 구현 후 실행: `C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_document_cleanup_modes -v`
- [ ] citation 흐름 회귀 확인: `C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_document_citations -v`

### Review Focus
- deterministic cleanup이 text keyword list나 site-specific selector로 변질되지 않았는지 확인한다.
- 외부 API batch mode의 LLM call count가 늘지 않았는지 확인한다.
- `clean_md`가 downstream single source로 계속 쓰이는지 확인한다: batch summary, RAG, verify, citation popup.
- HTML extraction 실패 시 raw fallback이 보수적으로 동작해 body 손실보다 leftover noise를 선택하는지 확인한다.

## Implementation Plan: Citation Anchor Reliability + Source Notes Table

### Problem
현재 citation popup은 `final.md`의 같은 줄 claim을 `clean_md/<doc_id>.md`에 즉석 lexical matching한다. `final.md` 문장은 batch summary를 다시 종합한 paraphrase인 경우가 많고, 한 문장에 여러 `[doc_NNN]`가 붙으면 각 문서에 같은 claim을 던진다. 그 결과 낮은 confidence가 대량 발생하고, service가 "가장 가까운 후보"를 반환하면서 관련 없는 원문 문장을 highlight하는 문제가 생긴다.

또한 `## Source Notes`는 prompt상 table을 요구해도 실제 출력에서 separator row가 빠지거나 각 row가 `- doc_001 | ...`처럼 bullet-prefixed pipe row로 생성되어 Markdown table rendering이 깨질 수 있다.

### Recommended Direction
- Citation popup은 low-confidence direct match를 원문 위치로 가장하지 않는다. 직접 match가 약하면 `summary/batch_*.md`의 source-proximal cited finding을 중간 anchor로 사용하고, 그래도 anchor가 없으면 문서 수준 fallback만 보여준다.
- Source Notes는 prompt 보강에 더해 `final_report_tool` 저장 직전 deterministic markdown normalizer로 table 형태를 보정한다. 이 보정은 `## Source Notes` section에만 적용한다.

### Claude Implementation Checklist: Citation Anchors
- [ ] `api/services/document_citation_service.py` 또는 별도 `api/services/document_citation_anchor_service.py`에 batch-summary anchor lookup을 추가한다.
- [ ] `summary/batch_*.md`에서 citation-bearing bullet/line을 파싱한다. 각 line의 `[doc_NNN]` marker를 추출하고, marker를 제거한 line text를 `batch_claim`으로 둔다.
- [ ] 각 `(doc_id, batch_claim)`에 대해 기존 `match_claim_in_source()` 또는 동일 scoring helper로 `clean_md/<id>.md`에서 source sentence anchor를 찾는다.
- [ ] low-confidence anchor는 "정확한 source sentence"로 채택하지 않는다. threshold는 direct source match와 anchor match에 명확히 둔다.
- [ ] `get_citation(workspace_id, doc_id, final_claim)` 처리 순서:
  1. final claim을 clean_md에 직접 match한다. `high` 또는 충분한 `medium`이면 반환한다.
  2. direct match가 약하면 같은 doc_id의 batch anchors 중 final claim과 lexical/numeric overlap이 가장 높은 anchor를 찾는다.
  3. batch anchor가 충분하면 그 anchor의 source sentence/paragraph를 반환하고 `matchSource: "batch_anchor"`, `anchorClaim`을 포함한다.
  4. 둘 다 부족하면 임의의 low-confidence "closest sentence"를 반환하지 말고 `match=None`, `resolution: "document_only"`로 반환한다.
- [ ] frontend popup은 `match=None`이면 unrelated sentence highlight를 보여주지 않는다. 대신 "이 인용은 문서 수준 근거로 연결되었지만 정확한 문장 위치는 확정되지 않았습니다."처럼 문서 수준 fallback으로 표시한다.
- [ ] `BATCH_SUMMARY_PROMPT`와 `FINAL_PROMPT`를 보강한다: 각 `[doc_NNN]`는 그 문서가 같은 문장의 구체 claim을 직접 뒷받침할 때만 붙이고, 관련 문서라는 이유만으로 citation을 남발하지 말 것. 여러 문서를 한 문장에 붙일 때는 모든 문서가 같은 claim을 독립적으로 support해야 한다.

### Claude Implementation Checklist: Source Notes Table
- [ ] `core/report_markdown_normalizer.py` 또는 유사한 pure helper를 추가해 `normalize_final_report_markdown()`을 만든다.
- [ ] `tools/final_report_tool/final_report_tool.py`에서 `clean_latex_in_markdown()` 이후, `save_final_report()` 이전에 final markdown normalizer를 호출한다.
- [ ] normalizer는 `## Source Notes` section에만 적용한다.
- [ ] Source Notes table header를 canonical form으로 보정한다:
  `| Doc ID | Title / Type | Year | What it contributes | Reliability / Caveat |`
  다음 줄에 `|---|---|---|---|---|`를 보장한다.
- [ ] `- doc_001 | ...`, `- [doc_001] | ...`, `doc_001 | ...` 같은 row를 `| [doc_001] | ... |`로 보정한다.
- [ ] table cell 안의 leading bullet marker는 제거하되, unknown value `-` 자체는 유지한다.
- [ ] cell 내부 줄바꿈은 space 또는 semicolon으로 접고, literal pipe는 table delimiter와 혼동되지 않도록 escape 또는 안전하게 치환한다.
- [ ] `FINAL_PROMPT`에도 Source Notes table은 leading bullet 없이 single-line pipe rows만 사용하라고 명시한다. 하지만 prompt만 믿지 말고 normalizer를 유지한다.

### Tests Required
- [ ] `tests/test_document_citations.py`: direct low-confidence final claim이 unrelated closest sentence를 반환하지 않고 `match=None`/`document_only`가 되는지 검증한다.
- [ ] 같은 테스트에서 final claim은 paraphrase지만 같은 doc_id의 `batch_*.md` cited line이 source sentence에 더 잘 맞을 때 `batch_anchor`가 사용되는지 검증한다.
- [ ] multi-doc final claim에서 각 doc_id가 자기 batch anchor로 resolve되는지 검증한다.
- [ ] `tests/test_final_report_normalizer.py` 또는 관련 테스트 추가: bullet-prefixed Source Notes rows가 canonical table로 보정되는지 검증한다.
- [ ] Source Notes에서 separator row가 누락된 경우 자동 삽입되는지 검증한다.
- [ ] `[doc_1]`, `doc_1`, `doc-1` row id가 `[doc_001]`로 보정되는지 검증한다.
- [ ] 실행: `C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_document_citations -v`
- [ ] 실행: `C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_final_report_normalizer -v`

### Review Focus
- 낮은 confidence 문장을 highlight하지 않는지 확인한다. "틀린 highlight"보다 "문서 수준 fallback"이 낫다.
- batch anchor index가 추가 LLM call 없이 기존 `batch_*.md`와 `clean_md`만 사용해 생성되는지 확인한다.
- final report normalizer가 `## Source Notes` 외의 본문/수식/일반 표를 건드리지 않는지 확인한다.
- citation 남발을 줄이는 prompt 변경이 keyword 기반 boilerplate filter로 변질되지 않았는지 확인한다.

## Implementation Plan: Cleanup Quality After `runs/삼성전자-3` Review

### Observed Quality
`runs/삼성전자-3/raw_md`와 `clean_md` 23건을 비교한 결과, structural extraction이 채택된 문서는 10건(`000`, `002`, `008`, `009`, `010`, `012`, `017`, `019`, `020`, `021`)이고, 13건은 raw와 clean이 완전히 동일했다(`001`, `003`, `004`, `005`, `006`, `007`, `011`, `013`, `014`, `015`, `016`, `018`, `022`).

품질은 부분 개선 수준이다. `009`는 본문 표 구조가 살아났지만 tail에 `Tags`, `Follow`, `Recent Posts`, unrelated post list가 여전히 남았다. `004`는 extractor 결과가 기사 본문 중심으로 2,227자까지 줄었지만 raw 대비 14%라서 `_MIN_STRUCTURAL_RETENTION=0.5` gate에 막혀 noisy raw가 그대로 clean으로 쓰였다. `001`은 extractor 자체가 삼성닷컴 프로모션/IR navigation 쪽을 고르는 실패 케이스라 retention만 낮추면 오히려 악화된다. `011`은 extractor 결과가 Counterpoint 표와 본문 중심으로 좋아 보이지만 raw 대비 30%라 fallback되었다.

### Diagnosis
- 현재 fallback gate는 noisy `raw_md` 길이를 기준으로 삼기 때문에, chrome이 많은 문서일수록 좋은 extraction을 버리는 역설이 생긴다.
- `html_body_extractor._pick_body()`는 후보가 `article/main/[role=main]/body`에 한정되어 있고, 실제 뉴스 사이트처럼 main article이 non-semantic `div` 안에 있거나 잘못된 `<article>`이 관련기사 박스인 경우를 잘 못 다룬다.
- 변환 후 문자열만 보면 footer link cluster가 일반 텍스트처럼 보인다. link-density 같은 구조 정보는 DOM block 단계에서 보존하고, 그 정보를 이용해 leading/trailing chrome을 잘라야 한다.
- cleanup provenance가 기록되지 않아, 어느 문서가 structural extraction 채택/거절인지 UI나 리뷰에서 바로 확인하기 어렵다.

### Recommended Direction
- keyword/class/id/site selector는 계속 금지한다. `tags`, `related`, `share`, `footer`, `광고` 같은 문자열로 제거하지 않는다.
- extractor를 단순 container 선택에서 "block-run extraction"으로 확장한다. DOM에서 heading/paragraph/list/table/pre 등 terminal block을 순서대로 수집하고, 각 block에 text length, link text length, href count, form/button/control count, table 여부, heading 여부를 함께 저장한다.
- 후보 window를 연속 block 단위로 scoring한다. score는 language/domain-agnostic 구조 지표만 사용한다: prose length, paragraph/table count, heading continuity, low link density, low control density, non-empty text diversity.
- leading/trailing block trimming을 추가한다. 본문 candidate 내부에서도 앞뒤의 link-heavy/list-heavy/control-heavy block run은 제거하고, middle body는 보존한다. 이 단계에서도 text keyword를 쓰지 않는다.
- fallback gate를 retention 고정값에서 quality-based acceptance로 바꾼다. extraction이 충분한 본문 구조(예: 최소 길이, 여러 prose/table block, 낮은 link density)를 갖추면 raw 대비 50% 미만이어도 채택한다. 반대로 `001`처럼 추출물이 promo/navigation 성격이고 link/control density가 높으면 낮은 retention이어도 채택하지 않는다.
- `_batch_clean_body()`는 `extract_main_text_with_stats()` 같은 새 helper를 사용해 `{text, accepted, reason, score, raw_len, extracted_len, link_density}` 형태의 provenance를 받을 수 있게 한다. raw text는 JSONL/metadata에 저장하지 말고 수치/원인만 이벤트나 result data에 둔다.
- `raw_md`가 이미 좋은 `fit_markdown`일 수 있으므로 fallback 자체는 유지하되, fallback 사유가 `too_short`, `low_quality`, `no_html`, `extractor_error`처럼 구분되게 한다.
- `summary/batch_*.md`, RAG, verification, citation popup은 계속 `clean_md`만 downstream source로 사용한다.

### Claude Implementation Checklist
- [ ] `services/document_cleanup_tool_funcs/html_body_extractor.py`에 block model과 stats-bearing extraction helper를 추가한다. 기존 `extract_main_text(html) -> str` API는 compatibility wrapper로 유지한다.
- [ ] block 수집 시 DOM 구조에서 link/control/table/heading/prose 통계를 같이 보존한다.
- [ ] candidate scoring과 leading/trailing trimming을 구조 통계 기반으로 구현한다.
- [ ] `tools/document_cleanup_tool/document_cleanup_tool.py:_batch_clean_body()`를 fixed retention gate에서 quality-based gate로 변경한다.
- [ ] `_run_batch_mode()` progress/result data에 cleanup provenance summary를 추가하되 raw text는 저장하지 않는다.
- [ ] `ARCHITECTURE.md`에 삼성전자-3 재조사 진단과 새 gate 설계를 기록한다.
- [ ] prompt 변경은 최소화한다. cleanup 개선은 prompt가 아니라 upstream source quality에서 해결한다.

### Tests Required
- [ ] `tests/test_document_cleanup_modes.py`에 "article body is accepted even when extraction/raw retention is below 0.5" 회귀 테스트를 추가한다.
- [ ] 잘못된 promo/navigation extraction은 채택하지 않고 raw fallback하는 테스트를 추가한다.
- [ ] 관련기사 `<article>`이 진짜 본문보다 먼저 나와도 block-run scoring이 본문 window를 고르는 테스트를 추가한다.
- [ ] tail의 unrelated link cluster가 구조 통계로 잘리는 테스트를 추가한다.
- [ ] table-heavy source(`Counterpoint` 유형)가 낮은 retention 때문에 버려지지 않는 테스트를 추가한다.
- [ ] cleanup provenance에 reason/score/length fields가 있고 raw body text가 포함되지 않는지 검증한다.
- [ ] 실행: `C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_document_cleanup_modes -v`
- [ ] 실행: `C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_crawl4ai_fetch -v`

### Review Focus
- keyword/class/id/site-specific selector가 들어오지 않았는지 확인한다.
- retention threshold를 단순히 낮추는 패치로 끝내지 않았는지 확인한다.
- `001` 같은 bad extraction을 채택하지 않고, `004`/`011` 같은 good low-retention extraction은 채택하는지 fixture로 확인한다.
- 추가 LLM call이 생기지 않았는지 확인한다.
- 기존 local per-doc LLM cleanup path는 변경하지 않았는지 확인한다.

## Codex Review Checkpoint: Current Diff Verification

### Review Checklist
- [ ] Review citation link presentation, API lookup, and document cleanup source-quality changes against repository guardrails.
- [ ] Confirm `frontend/` does not read `runs/` or `clean_md/` directly and uses the HTTP API/controller boundary.
- [ ] Confirm FastAPI handlers that perform file I/O remain plain `def` where applicable.
- [ ] Check prompt changes keep canonical `[doc_000]` citation markers without adding deterministic boilerplate keyword stripping.
- [ ] Inspect new tests for focused `unittest` regressions and meaningful coverage of fixed behavior.

### Architecture Constraints
- Keep UI responsibilities in `frontend/`, API routing thin, and matching/extraction logic in services/helpers.
- Do not mutate stored `final.md` sources for presentation-only citation links.
- Do not introduce extra LLM calls for citation preview or cleanup fallback.
- Avoid language-, keyword-, or site-specific boilerplate deletion rules.

### Verification Commands
- `python -m unittest tests.test_document_citations -v`
- `python -m unittest tests.test_document_cleanup_modes -v`
- `python -m unittest tests.test_crawl4ai_fetch -v`
- `python -m unittest tests.test_final_report_normalizer -v`
- `python -m unittest discover tests`

### Review Focus
- Citation label normalization for bracketed and bare markers, including zero-padded `doc_007` variants.
- Citation endpoint safety for workspace/doc IDs and source snippet size.
- Cleanup extraction gate behavior for low-retention-but-good article bodies versus navigation/promo noise.
- Final report normalization preserving canonical citation markers without altering source documents.

## Implementation Plan: Final Report JSON Leakage Guard

### Problem
`runs/Multi_Armed_Bandit-2/final.md`에서 `## User Request` 아래에 `user_request`, `plan`, `batch_summaries`를 포함한 pretty-printed JSON payload가 그대로 출력되었다. 이는 `tools/final_report_tool/final_report_tool.py`가 final synthesis 입력을 하나의 JSON blob으로 만들고, `FINAL_PROMPT`가 `## User Request` 섹션에 무엇을 써야 하는지 명확히 제한하지 않아 모델이 입력 payload를 보고서 내용으로 오인한 결과다.

이 문제는 특정 모델만의 결함으로 처리하지 않는다. `gpt-5-nano`가 이 취약한 prompt/input contract에 더 민감하게 반응했을 수는 있지만, 파이프라인은 어떤 LLM에서도 raw payload leakage가 구조적으로 불가능하도록 설계해야 한다.

### Recommended Direction
- `final_report_tool`은 raw JSON blob을 user prompt로 보내지 않는다.
- final report input은 사람이 읽는 sectioned text로 렌더링한다:
  - `Original User Request:`에는 `request.md` 또는 `user_request` 문자열만 넣는다.
  - `Research Plan Summary:`에는 `topic`, `goal`, 핵심 `must_cover`만 짧게 넣는다.
  - `Batch Summaries:`에는 `summary/batch_*.md` 내용만 넣는다.
  - `kept_doc_count`, `duplicate_count`는 필요하면 `Run Stats:` 같은 짧은 metadata section으로 넣는다.
- `FINAL_PROMPT`에 명시한다: 입력 payload, JSON keys, plan object, search queries, `batch_summaries` 배열을 그대로 재현하지 말 것. `## User Request` 섹션에는 원문 사용자 요청만 쓰고, plan/search_queries/batch summaries는 절대 포함하지 않는다.
- 생성 후 deterministic leakage guard를 둔다. `final.md` 저장 전에 `## User Request` 섹션이 `{`, `"user_request"`, `"plan"`, `"batch_summaries"` 등 payload key로 시작하거나 포함하면 실패로 간주하고 repair 또는 retry한다.
- repair는 추가 LLM call 없이 수행 가능해야 한다. 가장 안전한 fallback은 `## User Request` 섹션을 `request.md`의 원문 요청으로 교체하고, 누출된 JSON block만 제거하는 것이다. retry를 선택하더라도 최대 1회로 제한하고, prompt에 "previous output leaked internal JSON"을 명시한다.

### Claude Implementation Checklist
- [ ] `tools/final_report_tool/final_report_tool.py`에서 `json.dumps({...})` prompt 조립을 sectioned text renderer로 교체한다.
- [ ] renderer helper를 pure function으로 분리한다. 예: `_render_final_report_input(user_request, plan, records, batch_summaries) -> str`.
- [ ] renderer는 raw `plan` 전체를 dump하지 않고 allowlist fields만 출력한다: `topic`, `goal`, `must_cover` 일부, `keywords` 일부 정도.
- [ ] `batch_summaries`는 Markdown 본문으로 구분자와 함께 넣되 JSON string escaping 형태로 넣지 않는다.
- [ ] `core/prompts/autosurvey.py`의 `FINAL_PROMPT`에 internal payload/JSON key leakage 금지 규칙을 추가한다.
- [ ] 저장 전 guard helper를 추가한다. 예: `_repair_user_request_section_if_leaked(final_markdown, user_request)`.
- [ ] guard는 `## User Request` section에만 적용하고, 다른 본문/수식/Source Notes table은 건드리지 않는다.
- [ ] leakage가 감지되면 raw JSON body를 저장하지 않는다. repair 결과를 저장하거나, 1회 retry 후에도 실패하면 repair fallback을 저장한다.
- [ ] 이 변경으로 추가 LLM call이 기본 경로에 생기면 안 된다. retry는 leakage 감지 시에만 발생해야 한다.
- [ ] `ARCHITECTURE.md`에 `Multi_Armed_Bandit-2` 사례와 final report 입력 계약 변경을 기록한다.

### Tests Required
- [ ] `tests/test_final_report_tool.py` 또는 관련 테스트를 추가해 final report prompt input에 literal `"batch_summaries": [` / `"plan": {` 같은 raw JSON structure가 포함되지 않는지 검증한다.
- [ ] `## User Request` section에 JSON payload가 누출된 synthetic output을 repair하면 원문 request만 남고 `plan`, `search_queries`, `batch_summaries`가 제거되는지 검증한다.
- [ ] 정상적인 `## User Request` section은 guard가 변경하지 않는지 검증한다.
- [ ] Source Notes table normalizer와 guard가 서로 간섭하지 않는지 검증한다.
- [ ] 실행: `C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_final_report_tool -v`
- [ ] 실행: `C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_final_report_normalizer -v`

### Review Focus
- raw JSON leakage를 모델 품질 문제로만 치부하지 않고 prompt/input contract에서 차단했는지 확인한다.
- `plan.json` 전체, `plan_history.json`, `query_state.json`, `batch_summaries` 배열이 final prompt에 JSON 형태로 노출되지 않는지 확인한다.
- guard가 `## User Request` 외의 수식, citations, Source Notes table을 훼손하지 않는지 확인한다.
- 기본 final report 생성 경로의 LLM call count가 늘지 않았는지 확인한다.

## Implementation Plan: AutoSurvey Quality, Speed, and UX Review

### PM Diagnosis
AutoSurvey 구조는 합리적이다: `term_grounding -> initial plan -> scout collect -> cleanup -> batch summary -> gap/replan loop -> final_report -> RAG index` 흐름은 유지보수 가능한 파이프라인이다. 다만 현재 품질 병목은 모델 크기보다 수집 전 후보 선별과 수집 후 coverage 판단에 있다.

최근 run 기준 15~23개 문서 수집에 약 475~711초가 걸렸고, `runs/국내`에서는 대체육 시장 조사에 AI video market, bath bomb market 등 오프토픽 시장 리포트가 섞였다. cleanup과 final prompt를 보강해도 오프토픽 문서가 이미 `clean_md`, batch summary, final synthesis로 들어오면 사용자 신뢰가 떨어진다.

### Priority Direction
- 문서를 더 많이 모으는 방향보다 같은 `maxDocs` 안에서 후보 품질을 높인다.
- 추가 LLM call은 기본 경로에 넣지 않는다. 검색 결과 title/snippet/url/domain, query terms, `plan.must_cover`, `plan.keywords`, user request에서 나온 lexical/structural signal만 사용한다.
- deterministic boilerplate keyword filtering은 금지한다. 기존 원칙대로 cleanup은 HTML/ARIA/구조 통계 기반으로 유지한다.
- `maxDocs`는 목표치가 아니라 상한으로 취급한다. coverage가 충분하고 core gap이 없으면 조기 종료할 수 있어야 한다.

### Claude Implementation Checklist
- [ ] `workflows/autosurvey_workflow.py:209`의 collect 루프 앞단에 source-candidate scoring 단계를 추가한다. 검색 결과를 받은 즉시 title/snippet/url/domain/query와 plan/user-request 토큰으로 relevance score를 계산하고, 낮은 후보는 fetch 전에 제외한다.
- [ ] scoring helper는 별도 pure module로 분리한다. 예: `services/autosurvey_source_quality.py`. topic-specific keyword list, site-specific selector, boilerplate keyword blocklist는 넣지 않는다.
- [ ] domain diversity cap을 둔다. 한 도메인이 결과를 과점하지 않도록 하되, 사용자가 명시한 reference URL/site constraint는 예외로 우선 처리한다.
- [ ] post-fetch acceptance metadata를 추가한다. fetched body가 user request / plan terms와 거의 겹치지 않으면 kept document로 소비하지 말고 rejected note/metadata로 남긴다. rejection은 `maxDocs`를 소모하지 않게 설계한다.
- [ ] coverage ledger를 추가한다. batch summary의 Core Gap/Supporting Gap과 collected source metadata를 사용해 `must_cover` 항목별 상태를 `summary/autosurvey_metrics.json`에 저장한다.
- [ ] early stop 조건을 넣는다. 최소 문서 수를 넘었고 core gap이 없거나 최근 cycle의 accepted-source marginal gain이 낮으면 final로 넘어간다.
- [ ] speed 개선은 먼저 fetch 전 filtering으로 한다. 이후 필요하면 낮은 concurrency(예: 3~4) fetch를 도입하되, RunStore write는 순차화해서 doc id와 index 안정성을 유지한다.
- [ ] `api/services/document_citation_service.py:384`의 batch anchor fallback을 수정한다. 상위 3개 anchor 후보 중 source score만으로 고르지 말고, 각 후보의 final-claim overlap을 다시 계산해 threshold 미만은 제외하고 combined score로 고른다.
- [ ] Research UI에는 단순 문서 수뿐 아니라 accepted/rejected/fetch-error/duplicate count, 주요 remaining core gaps, source quality warning을 표시한다. 이미 있는 progress event/detail 경로를 우선 활용한다.

### Tests Required
- [ ] source scoring pure tests: query와 무관한 시장 리포트(title/snippet/domain)가 relevant 후보보다 뒤로 밀리는지 확인한다.
- [ ] domain diversity tests: 동일 도메인 결과가 과점하지 않는지, 명시 reference URL은 예외 처리되는지 확인한다.
- [ ] post-fetch rejection tests: 오프토픽 fetched body가 kept doc id를 소비하지 않고 rejected metadata로 남는지 확인한다.
- [ ] coverage/early-stop tests: core gap이 없고 최소 문서 수를 넘으면 `maxDocs`까지 억지로 수집하지 않는지 확인한다.
- [ ] citation anchor tests: paraphrased final claim과 관련 없는 exact source sentence가 있는 multi-finding 문서에서 unrelated anchor가 선택되지 않는지 확인한다.
- [ ] UX/API tests: research job response나 progress detail에 rejected/fetch-error/duplicate/source-quality counts가 누락되지 않는지 확인한다.

### Review Focus
- 개선이 더 큰 LLM이나 추가 API call에 의존하지 않는지 확인한다.
- source quality logic이 특정 키워드, 특정 사이트, 특정 언어에 과적합하지 않는지 확인한다.
- cleanup과 candidate scoring 책임이 섞이지 않는지 확인한다. cleanup은 body 정제, source scoring은 조사 적합성 판단으로 분리한다.
- fetch 병렬화를 도입했다면 shared RunStore/index/doc id write 경합이 없는지 확인한다.
- UX가 사용자에게 “왜 이 문서가 선택/제외됐는지”를 짧게 설명하되, final.md 본문 품질을 UI 배지로 덮어 숨기지 않는지 확인한다.

## Implementation Plan: AutoSurvey Demo Surprise and Performance Upgrades

### Product Goal
Improve demo impact without defaulting to bigger models or more API calls. The highest-value demo point is not “more documents,” but “the system visibly knows which evidence is useful, which evidence is rejected, and which gaps remain.”

### High-Impact Features
- [ ] Add a live Evidence Coverage panel: rows are `must_cover` items, columns show accepted source count, unresolved gap status, and confidence. This can be driven from `summary/autosurvey_metrics.json`.
- [ ] Show rejected-source replacement live: when a result is anti-bot, off-topic, thin, or low-quality, mark it as rejected with a short reason and continue searching until the usable-document budget or early-stop condition is reached.
- [ ] Add citation confidence indicators in the final report preview: exact source match, batch-anchor match, and document-level fallback should render differently so users know how strong each citation anchor is.
- [ ] Add “Run follow-up for this gap” actions on Remaining Gaps / Core Gap items. This should generate targeted follow-up queries from the stored gap text and reuse the same workspace.
- [ ] Add source clustering by claim/domain: repeated claims should show which documents independently support them, making the repeated-finding section visually persuasive during demos.
- [ ] Add a Fast Demo mode that uses stricter pre-fetch scoring, lower `maxDocs`, early stop, and cached/resumable workspace artifacts. It must be an explicit mode, not the default research path.

### Performance Constraints
- Prefer fetch-before-LLM savings: candidate scoring, rejected-source replacement, and early stop should reduce downstream cleanup/batch/final work.
- Keep new UI metrics deterministic and derived from existing artifacts where possible.
- Do not add another LLM review pass for every document. If a stronger model is used, gate it behind an explicit Quality/Deep mode or failed coverage threshold.

### Review Focus
- Demo UI must not overstate evidence confidence; document-level fallback citations should be visibly weaker than exact anchors.
- Follow-up gap actions must not mutate prior evidence or hide prior rejected-source reasons.
- Fast Demo mode must be labeled as a speed-optimized mode and should not silently reduce quality in the normal AutoSurvey workflow.

## Implementation Plan: AutoSurvey Review Fixes After Source-Quality Increment

### Context
The latest implementation added pre-fetch source scoring, post-fetch rejection,
early stop, and citation anchor combined scoring. Targeted tests pass in the
`agent` conda environment, but the review found three engineering risks that
should be fixed before treating this increment as production-ready.

### Claude Implementation Checklist
- [ ] Tighten early-stop semantics in `workflows/autosurvey_workflow.py`. If
  `gap_directions` is non-empty, do not stop only because
  `accepted_this_cycle <= _EARLY_STOP_MIN_GAIN`. Replan/retry should be tried
  first, and low-gain stop should require stronger evidence such as exhausted
  remaining queries, repeated empty/low-gain cycles, or an explicit no-progress
  state recorded in query state.
- [ ] Keep the existing `no_core_gap` early stop: `kept >= min_docs` and no core
  gap may still stop before `max_docs`.
- [ ] Update `_early_stop_decision` tests in `tests/test_autosurvey_collect.py`
  so "gap remains + low gain" does not stop on the first low-gain cycle. Add a
  separate test for the new allowed low-gain terminal condition.
- [ ] Pass the original user request into source scoring. `services/
  autosurvey_source_quality.build_topic_terms()` already accepts
  `user_request`, but `workflows/autosurvey_workflow.py` currently calls it with
  only `plan` and `query`. Extend `run_collect(..., user_request: str = "")`
  and pass the request from `run_all()` for scout and main cycles.
- [ ] Preserve backwards compatibility for any direct `run_collect(plan=...)`
  tests/callers by defaulting `user_request` to an empty string.
- [ ] Add or update tests proving request-only constraints influence
  `TopicTerms` and candidate ranking, while `plan.search_queries` remains
  excluded from topic signal.
- [ ] Make reference-domain matching include subdomains. A pinned domain such as
  `samsung.com` should exempt `news.samsung.com` and `www.samsung.com` from the
  relevance gate and domain cap. Use structural domain suffix matching:
  `domain == ref or domain.endswith("." + ref)`.
- [ ] Add tests in `tests/test_autosurvey_source_quality.py` for parent-domain
  reference exemption and subdomain domain-cap exemption.

### Required Tests
- [ ] `C:/Users/asdf/.conda/envs/agent/python.exe -m unittest tests.test_autosurvey_source_quality -v`
- [ ] `C:/Users/asdf/.conda/envs/agent/python.exe -m unittest tests.test_autosurvey_collect -v`
- [ ] `C:/Users/asdf/.conda/envs/agent/python.exe -m unittest tests.test_document_citations -v`

### Review Focus
- Early stop must mean "enough evidence and no unresolved core gap" or
  "recoverable collection paths are exhausted", not simply "collection was slow".
- Source scoring must use user intent, plan fields, and the live query, but must
  not use `plan.search_queries` as topic vocabulary.
- Reference-site handling must honor user-pinned domains without introducing a
  site-specific allowlist.
- Keep the implementation deterministic and avoid adding LLM calls.

## Implementation Plan: Diffusion_LM-2 Quality and Citation Reliability Fixes

### Context From Run Review
Workspace `runs/Diffusion_LM-2` produced a readable final report, but it did not
meet the desired verification UX level. The report covers speed, quality, and
structural differences well, and the Source Notes table renders correctly, but
source verification is weak:

- `final.md` contains 82 citation markers.
- Citation preview resolution: `document_only=68`, `direct=8`,
  `batch_anchor=6` (about 83% unresolved).
- Executive Summary and Conflicts/Uncertainties citations all resolved as
  `document_only`.
- 30 kept documents were summarized; only 20 were cited in `final.md`.
- 14 fetch errors were recorded and 0 rejected notes were produced.
- Some accepted documents were peripheral or off-topic for text DLM comparison
  (for example video Diffusion/DiT optimization and generic LLM benchmark pages).
- Several `clean_md` files became much larger than `raw_md` because HTML
  extraction pulled large embedded listing/JSON-like bodies; these pages wasted
  cleanup/summarization budget.

Important diagnosis: this is not primarily a RAG chunking problem. The citation
popup does not use Chroma/RAG chunks; it reads `clean_md/<doc>.md` and performs
deterministic lexical matching against the clicked final-report claim. The main
failure modes are:

1. Cross-language mismatch: final claims are Korean syntheses while many source
   documents are English, so lexical sentence matching cannot bridge them.
2. Claim granularity: `frontend/citation_links.py` attaches the whole Markdown
   line as the claim. Lines with multiple citations or synthesized multi-part
   claims are too broad for a single source sentence.
3. Batch-anchor weakness: batch summaries are often Korean while source text is
   English, so batch claims also fail to anchor back to source sentences.
4. Source Notes rows are being linkified as evidence claims, but table rows are
   metadata/document descriptions, not source-backed final-report claims.
5. Query drift: `build_topic_terms()` currently treats the live query as topic
   signal. If a replan query drifts toward generic DiT/video diffusion
   optimization, the off-topic result can satisfy the gate using query-only
   terms.

### Claude Implementation Checklist
- [ ] Add citation evidence atoms during document summarization without adding a
  new LLM call. Extend the existing document-summary output contract so each key
  point can include: `evidence_id`, `doc_id`, `localized_claim`, `source_quote`
  or `source_sentence`, and optional `paragraph_index` / `sentence_index`.
- [ ] After a document summary is produced, deterministically verify each
  `source_quote` against `clean_md/<doc>.md`. Store only anchors that can be
  found exactly or with a high lexical/fuzzy score. Unverified key points may be
  kept as summary text but must not become clickable exact citations.
- [ ] Persist verified anchors in a sidecar such as
  `summary/citation_evidence.json` or `summary/citation_evidence/<doc>.json`.
  Do not store long raw bodies; store bounded source snippets and stable
  paragraph/sentence offsets or hashes.
- [ ] Make batch summaries and final report generation cite evidence atoms, not
  bare documents where possible. The final report can still render visible
  labels as `[doc_004]`, but the linkifier/API must be able to resolve the
  clicked occurrence to a specific evidence atom.
- [ ] Add a final-report postprocessor that builds
  `summary/final_citations.json`: for each marker occurrence, store
  `doc_id`, final local claim text, matched `evidence_id`, resolution
  (`evidence_anchor` / `document_only`), and confidence. Matching should compare
  final Korean claim to `localized_claim`, not directly to English source text.
- [ ] Update `frontend/citation_links.py` and
  `api/services/document_citation_service.py` so citation clicks first use
  `final_citations.json` / evidence ids. Fall back to current direct/batch
  matching only when no evidence map exists.
- [ ] Treat `## Source Notes` Doc IDs differently from claim citations. A Doc ID
  in the Source Notes table should open a document-level source preview or
  metadata card, not try to prove the whole table row as a source sentence.
- [ ] Improve source-quality gating against query drift. Separate core topic
  terms (user request + plan topic/goal/must_cover/keywords) from live query
  terms. Query terms may help ranking but must not be sufficient for post-fetch
  body acceptance.
- [ ] Add a post-cleanup quality gate. If structural extraction expands
  massively beyond `raw_md`, has high JSON/listing/repeated-record density, low
  prose density, or is dominated by framework payload text, mark the document as
  `rejected_clean` or exclude it from summarization/final synthesis. This must
  be structural/statistical, not a hard-coded keyword/site blocklist.
- [ ] Make rejected post-cleanup documents not count as usable evidence. If
  practical, collect replacement candidates until the usable-document cap or a
  safe early-stop condition is reached.
- [ ] Tighten final-report prompt style: avoid assistant-chat closings such as
  "If you want..." / "원하시면...". Use a report-native `Recommended Next Steps`
  section instead.

### Required Tests
- [ ] Citation evidence tests: a Korean final claim citing an English source
  resolves through `localized_claim -> source_quote` and returns a highlighted
  source sentence without another LLM call.
- [ ] Multi-citation line tests: two markers on the same line can resolve to
  different evidence ids instead of sharing one oversized line claim.
- [ ] Source Notes tests: Doc IDs inside `## Source Notes` route to
  document-level preview and do not produce "exact location not determined" as
  if they were unsupported claims.
- [ ] Query drift tests: a live query containing generic/video diffusion terms
  cannot make an otherwise non-text-DLM page pass post-fetch acceptance unless
  the body also overlaps core topic terms.
- [ ] Cleanup quality tests: HTML pages with embedded JSON/listing payloads or
  huge structural expansion are rejected or trimmed without using site-specific
  keywords.
- [ ] Regression test on `runs/Diffusion_LM-2`-style fixture: unresolved citation
  ratio should drop substantially for body sections, especially Executive
  Summary and Conflicts/Uncertainties.

### Review Focus
- Do not add per-document extra LLM calls. Use the existing document-summary
  call to emit evidence atoms, then verify anchors deterministically.
- Do not rely on RAG chunking to fix citation preview. Citation preview must be
  grounded by explicit source anchors or an honest document-level fallback.
- Do not overstate weak evidence. If no verified evidence atom exists, show
  document-level fallback with lower visual confidence.
- Keep final report readability: visible citation labels should remain simple
  (`[doc_NNN]`) even if the underlying link carries an evidence id.

## Review Note: 2026-06-04 Source Quality / Citation Increment

### Checklist
- [ ] Ensure Korean topic matching does not require exact whitespace-token
  equality. Normal Korean particles and compounds such as `대체육은` or
  `시장규모는` must still count toward the post-fetch topic gate when the topic
  contains `대체육` / `시장`.
- [ ] Pass the original `user_request` through every `run_collect()` entrypoint,
  including CLI `--phase collect`, so request-only constraints are available to
  source scoring and post-fetch acceptance.
- [ ] Keep Source Notes citations document-level only; body sections after
  `## Source Notes` must return to claim-level citation links at the next
  heading.
- [ ] Keep structured-payload fallback statistical/structural only. Do not add
  fixed boilerplate, framework, site, or language keyword blocklists.

### Architecture Constraints
- Source-quality gates must remain deterministic and must not add LLM/API calls.
- Candidate scoring can use user request, plan topic/goal/must_cover/keywords,
  current query, title/snippet/url/domain, and structural token overlap only.
- Post-fetch acceptance should use core intent terms, not query-only drift
  terms, and rejected sources must not allocate `doc_*` ids or store raw body
  text.
- Citation preview remains a presentation/read-only lookup over persisted
  `clean_md` and summary metadata; it must not mutate `final.md` or source
  documents.

### Verification Commands
- `python -m py_compile services/autosurvey_source_quality.py workflows/autosurvey_workflow.py frontend/citation_links.py api/services/document_citation_service.py`
- `python -m unittest tests.test_autosurvey_source_quality tests.test_autosurvey_collect tests.test_document_cleanup_modes`
- `python -m unittest tests.test_document_citations` after installing API/UI
  dependencies such as FastAPI in the active environment.

### Review Focus
- Watch for false rejections in Korean-heavy surveys caused by tokenization,
  particles, compounds, or cross-language title/body differences.
- Watch for entrypoints that call `run_collect(plan)` without user intent and
  therefore silently weaken source scoring.
- Confirm rejected/fetch-error/duplicate notes remain metadata-only and do not
  affect kept-document numbering or `maxDocs`.
- Confirm citation linkification changes are presentation-only and preserve the
  canonical visible marker format `[doc_NNN]`.
## Planning Note: 2026-06-05 Chat-Triggered AutoSurvey Memory Brief

### Checklist
- [x] Add an explicit AutoSurvey memory-brief builder instead of switching AutoSurvey tools from `ask/ask_json` to memory-aware `call`.
- [x] Use the brief only when AutoSurvey is launched from the chat/search mode (`ChatAgent` -> `AutoSurveyTool`), where an existing workspace and chat memory already represent the user's working context.
- [x] Keep the standalone/research-page AutoSurvey run memory-free by default. A first survey usually has no relevant KB/memory and should remain a clean request-driven research workflow.
- [x] Include only stable user preferences and task context: working-context records tagged/category `preference`, `profile`, `constraint`, and `project`.
- [x] Exclude raw FIFO history, full recall rows, assistant answers, local-private document/table contents, screen captures, and any source text from the memory brief.
- [x] Pass the brief as a separate labelled section such as `User Research Preferences` / `Non-evidence Context`; never merge it into `user_request`, `grounded_terms`, `clean_md`, `batch_summaries`, or citations.
- [x] Use the brief only in initial query planning. Do not inject it into final report generation, document cleanup/summarization, citations, source notes, or fetched document acceptance.
- [x] Persist lightweight provenance metadata (`memory_brief_used`, character count), not the raw memory text, unless a user-visible audit view is intentionally added.

### Architecture Constraints
- AutoSurvey currently runs through `AgentRuntime.run_autosurvey()`: build `autosurvey_llm`, term-ground workspace naming, reserve/publish workspace, build a per-run registry with `autosurvey_llm` for research-generation tools and local `embedding_llm` for RAG indexing, then `AutoSurveyWorkflow.run_all()`.
- Chat/search-mode AutoSurvey currently runs through `AgentRuntime.answer_chat_selection*()` -> `ChatAgent.ask_explicit_tool("autosurvey", ...)` -> `AutoSurveyTool.run()` against the active workspace's workflow and memory-bearing chat runtime.
- AutoSurvey tools (`term_grounding`, `query_plan`, `document_cleanup`, `document_summarize`, `final_report`) call `ask()` / `ask_json()`, which are raw passthrough methods on `MemoryAwareLLMClient`; memory is not injected today.
- Do not route AutoSurvey through `MemoryAwareLLMClient.call()` globally. That would inject FIFO/recall into cleanup/summarization/final prompts and can contaminate evidence, leak private context to OpenAI-backed AutoSurvey, and record survey internals as chat memory.
- OpenAI AutoSurvey remains an external research-generation role. Any memory sent to it must be treated as exportable user preference text only; local/private evidence remains local-only.
- Memory brief must be deterministic, bounded, and non-evidence. It can guide query style, language, depth, deliverable format, and project constraints, but fetched documents and their clean Markdown remain the only report evidence.

### Recommended Injection Points
- `AutoSurveyTool.run()` or the `ChatAgent.ask_explicit_tool("autosurvey", ...)` adapter: build `autosurvey_memory_brief` from the active workspace's `MemoryRuntime` and pass it into the workflow explicitly.
- `QueryPlanTool.run()`: add `memory_brief` to `planner_input` under a labelled field. Prompt rule: use it only for preference/constraint interpretation and query style, not as source evidence.
- Do not inject the brief into `FinalReportTool`. Final synthesis should remain driven by the original request, plan summary, run stats, and batch summaries only.
- Avoid `DocumentCleanupTool` and `DocumentSummarizeTool` injection. Those are source-processing stages and should remain driven only by document text plus the original request.
- `run_collect()` source ranking may use only explicit request/plan/reference URLs. Do not use memory-only topics to admit pages unless they are promoted into the plan as explicit must-cover constraints.

### Verification Commands
- `python -m py_compile services\autosurvey_memory_brief.py tools\autosurvey_tool\autosurvey_tool.py workflows\autosurvey_workflow.py tools\query_plan_tool\query_plan_tool.py core\prompts\autosurvey.py api\services\agent_runtime.py tests\test_autosurvey_memory_brief.py`
- `python -m unittest tests.test_autosurvey_memory_brief tests.test_autosurvey_collect tests.test_autosurvey_source_quality`
- Focused tests cover: memory brief category allowlist, no raw recall/FIFO leakage, chat-triggered AutoSurvey passes the brief to planning, and replan/final-adjacent paths do not receive memory.

### Review Focus
- Watch for accidental privacy regressions: memory brief must not contain local document/table text or screen OCR payloads.
- Watch for evidence contamination: final claims must still be grounded only in `[doc_NNN]` source documents.
- Watch entrypoint separation: chat/search-mode AutoSurvey can use a brief from the active workspace, but research-page AutoSurvey should remain clean unless the user explicitly asks to use memory.
- Watch prompt leakage: the memory brief must never be printed in `final.md`; it should influence query planning only.
