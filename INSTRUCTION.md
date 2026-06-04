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
