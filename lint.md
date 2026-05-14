# OpenKB `lint.py` 분석 보고서

> 대상 파일: [`openkb/lint.py`](https://github.com/VectifyAI/OpenKB/blob/main/openkb/lint.py) (264줄)
> 분석 목적: Veritas의 AutoSurvey 조사 워크플로우 종료 후, 산출된 문서 묶음에 대한 **정합성·최신화·유효성 검사** 단계로 통합하기 위한 사전 기능 분석

---

## 1. 개요

`lint.py`는 OpenKB가 구축한 위키 형태 지식 베이스(KB)에 대해 **구조적 정합성(structural lint)** 만을 검사하는 모듈이다. LLM을 호출하거나 의미적 일관성을 판단하지 않으며, 순수하게 **파일 시스템 + 마크다운 위키링크 그래프**를 기반으로 정적 검사만 수행한다.

핵심 전제는 다음 디렉터리 구조다.

```
<kb_dir>/
├── raw/         # 원본 입력 문서(예: PDF, 웹페이지 등의 원자료)
└── wiki/        # 생성된 위키
    ├── index.md
    ├── sources/    # raw 문서 1:1 대응 페이지 (자동 생성)
    ├── summaries/  # 문서 요약 페이지
    ├── concepts/   # 추출된 개념 페이지
    └── reports/    # 보고서(자동 생성)
```

검사 단위는 마크다운 파일 내부의 **`[[wikilink]]`** 구문(별칭 `[[target|display]]` 지원)이며, 정규식 `\[\[([^\]]+)\]\]` 로 추출한다.

검사에서 제외되는 파일/디렉터리:
- 파일: `AGENTS.md`, `SCHEMA.md`, `log.md` (메타/로그 성격)
- 디렉터리: `reports/`, `sources/` (자동 생성 산출물이므로 위키 콘텐츠로 취급하지 않음)

---

## 2. 제공 함수별 기능

### 2.1 `_all_wiki_pages(wiki)` — 페이지 인덱스 구축
- `wiki/` 하위 모든 `*.md`를 재귀 수집해 두 가지 키로 색인하는 dict 반환.
  - 상대경로(확장자 제거, 슬래시 정규화): 예) `concepts/attention`
  - 파일 stem 단독: 예) `attention`
- 즉, 위키링크가 **풀 경로**로 적혀 있어도 **이름만** 적혀 있어도 매칭 가능하도록 양쪽 키를 모두 등록.

### 2.2 `find_broken_links(wiki)` — 깨진 링크 탐지
- 모든 `.md`를 순회하면서 `[[...]]` 타깃이 페이지 인덱스에 없는 경우를 수집.
- 결과 예: `Broken link [[foo/bar]] in summaries/x.md`
- 제외: `_EXCLUDED_FILES`, `reports/*`, `sources/*`.
- **용도**: 본문에서 참조하는 다른 문서가 실제로 존재하는지 검증.

### 2.3 `find_orphans(wiki)` — 고아 페이지 탐지
- "**들어오는 링크도 없고 나가는 링크도 없는**" 페이지를 고아로 정의.
- 제외: `index.md`, `_EXCLUDED_FILES`, `sources/` 하위.
- 알고리즘
  1. 각 페이지별로 outgoing(나가는 위키링크 집합)을 만든다.
  2. 전 페이지의 outgoing을 합집합하여 incoming(어디든 한 번이라도 링크된 대상의 집합)을 만든다. 이때 풀 경로와 stem 모두를 incoming에 추가.
  3. `not has_incoming and not has_outgoing` 페이지만 고아로 보고.
- **주의(중요한 설계 특성)**: outgoing이 단 1개라도 있으면 incoming이 없어도 고아가 아니다. 즉 "역참조가 없는 막다른 페이지"는 잡아내지 못한다. 양방향 모두 비어있는 완전 고립 페이지만 잡는다.

### 2.4 `find_missing_entries(raw, wiki)` — 위키 미생성 원본 탐지
- `raw/` 디렉터리의 각 파일 stem이 `wiki/sources/*.md` 또는 `wiki/summaries/*.md` 중 어디에도 대응되는 stem이 없으면 "누락"으로 보고.
- **용도**: 새로 들어온 원본 자료가 워크플로우상 위키 페이지로 변환되지 않았는지(파이프라인 누락) 검증.

### 2.5 `check_index_sync(wiki)` — 인덱스 동기화 검사
- `wiki/index.md`가 존재하지 않으면 즉시 보고.
- 두 가지 비대칭 검사를 수행한다.
  1. **index → 페이지**: `index.md` 안의 `[[...]]` 타깃이 실제 페이지로 해석되지 않으면 보고 ("index.md links to missing page").
  2. **페이지 → index**: `summaries/`, `concepts/` 하위의 각 `.md` 파일 stem이 index의 위키링크 stem 집합에도 없고, index 본문 텍스트(소문자)에도 등장하지 않으면 보고 ("…/x.md not mentioned in index.md").
- 즉, **index.md를 KB의 목차(TOC)로 강제**하는 검사다.

### 2.6 `run_structural_lint(kb_dir)` — 통합 실행 + 마크다운 리포트 생성
- `kb_dir/wiki`, `kb_dir/raw` 경로를 자동 추론.
- 위 4개 검사(`find_broken_links`, `find_orphans`, `find_missing_entries`, `check_index_sync`)를 순차 실행하고, 각 섹션을 다음 형식의 마크다운 문자열로 반환.

```
## Structural Lint Report

### Broken Links (N)
- ...

### Orphaned Pages (N)
- ...

### Raw Files Without Wiki Entry (N)
- ...

### Index Sync Issues (N)
- ...
```

- 반환 타입은 **문자열**이며, 파일에 저장하지는 않는다. 저장은 호출자(예: CLI `openkb lint` 명령) 책임.

---

## 3. 핵심 설계 특성과 한계

### 3.1 기능 범위
| 카테고리 | 검사 여부 |
| --- | --- |
| 링크 무결성 (broken `[[ ]]`) | ✅ |
| 완전 고립 페이지(orphan) | ✅ (단, 양방향 모두 빈 경우만) |
| 파이프라인 누락(raw → wiki 미생성) | ✅ |
| 목차(index.md) 동기화 | ✅ |
| **문서 내용의 사실성·최신성** | ❌ |
| **문서 간 의미적 모순(contradiction)** | ❌ |
| **출처(citation) 유효성·URL 살아있음 여부** | ❌ |
| **stale content(오래된 정보) 탐지** | ❌ |
| **마크다운 문법 자체의 유효성** | ❌ |

> README/문서 레벨에서는 "contradictions, gaps, orphans, stale content"를 잡는 것으로 홍보되지만, **`lint.py` 자체는 구조적 검사만** 구현되어 있다. 의미적 검사(모순/최신성)는 별도의 LLM 기반 경로(예: agent 모듈 또는 chat `/lint`)에서 처리되는 것으로 보인다.

### 3.2 위키링크 의존성
- 모든 검사가 `[[wikilink]]` 구문에 의존. 일반 마크다운 링크 `[text](path.md)`나 상대경로 링크는 검사 대상이 아니다.
- 즉, Veritas의 AutoSurvey 산출물이 `[[ ]]` 형식을 쓰지 않는다면 **링크 추출기를 교체**하거나 산출물에 위키링크를 부착하는 후처리가 선행되어야 한다.

### 3.3 디렉터리 컨벤션 강제
- `wiki/`, `raw/`, `wiki/index.md`, `wiki/sources/`, `wiki/summaries/`, `wiki/concepts/`, `wiki/reports/`라는 고정 폴더 명칭에 강하게 결합되어 있다.
- Veritas 통합 시 (a) 같은 폴더 구조로 정규화하거나 (b) 이 상수들을 설정값화하는 리팩터링이 필요.

### 3.4 외부 의존성
- 표준 라이브러리만 사용 (`re`, `pathlib`). LLM/네트워크/DB 의존성 없음 → **그대로 떼어와 임베드 가능**. 라이선스(OpenKB의 LICENSE)만 확인하면 된다.

---

## 4. Veritas AutoSurvey 통합 관점의 시사점

1. **재사용 가능한 부분**: 4개 검사 함수는 순수 함수에 가깝고, 입력으로 디렉터리 경로만 받는다. AutoSurvey 종료 직후 산출 디렉터리를 가리키도록 호출하면 즉시 동작 가능.
2. **선행 작업 필요 항목**
   - Veritas 산출 문서를 `wiki/{index.md, summaries/, concepts/, sources/}` 컨벤션에 맞춰 배치하거나, 컨벤션을 설정 가능하도록 수정.
   - 문서 내 상호 참조를 `[[ ]]` 형태로 표준화(또는 `_WIKILINK_RE` 및 추출기 교체).
   - `raw/`에 해당하는 "원본 자료" 묶음(예: 수집된 웹 페이지/PDF) 경로 매핑.
3. **추가로 구현해야 할 검사(현 `lint.py`가 다루지 않는 부분)**
   - **최신화 검사**: 원본 자료의 수정시각·해시와 위키 페이지의 갱신 시각 비교 → "stale" 보고.
   - **모순/사실성 검사**: 동일 개념을 다루는 두 페이지 간 LLM 기반 모순 탐지(임베딩 클러스터링 + 페어 비교).
   - **출처 유효성 검사**: 인용 URL의 HTTP 상태, 인용 텍스트가 원문에 존재하는지(quote-match).
   - **막다른 페이지 검사**: 현행 orphan 정의는 너무 보수적이므로, "incoming 0인 페이지"와 "outgoing 0인 페이지"를 분리 보고하는 변형 추가 권장.
4. **반환 형식**: `run_structural_lint`가 마크다운 문자열을 돌려주므로, Veritas의 보고서 파이프라인(예: `runs/<id>/lint.md`로 저장)과 자연스럽게 연결 가능.

---

## 5. 한 줄 요약

`openkb/lint.py`는 **LLM 없이 위키링크 그래프와 디렉터리 컨벤션만으로 KB의 구조적 정합성(깨진 링크·고아 페이지·파이프라인 누락·목차 동기화)을 검사**하는 264줄짜리 순수 파이썬 모듈이며, "정합성"의 일부분(링크/구조)은 즉시 재사용 가능하지만 "최신화·유효성·모순"은 **별도 검사기를 추가 구현해야** 한다.
