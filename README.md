# Veritas

**자동화된 웹 리서치 및 RAG(Retrieval-Augmented Generation) 시스템**

LLM과 Tool-Use 패턴을 활용하여 웹 검색, 문서 수집, 요약, 최종 보고서 생성까지 자동화하는 AI Agent 시스템입니다.

---

## 📋 프로젝트 개요

Veritas는 사용자의 리서치 요청을 받아 다음 파이프라인을 자동으로 실행합니다:

1. **계획 수립 (Plan)**: 리서치 주제를 분석하여 검색 쿼리 생성
2. **문서 수집 (Collect)**: 웹 검색 및 페이지 크롤링, 중복 제거
3. **요약 생성 (Summarize)**: 개별 문서 요약 및 배치 요약
4. **최종 보고서 (Final)**: 모든 정보를 종합한 마크다운 보고서
5. **RAG 채팅 (Chat)**: 수집된 문서 기반 대화형 Q&A

---

## 🏗️ 아키텍처 개요

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Application Layer                             │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │                    workflows/                                   │ │
│  │  AutoSurveyWorkflow (plan → collect → summarize → final)       │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                │                                     │
│                                ▼                                     │
├─────────────────────────────────────────────────────────────────────┤
│                           Core Layer                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────────┐ │
│  │    core/     │  │    tools/    │  │       services/            │ │
│  │ Models       │  │ ToolRegistry │  │ RunStoreService            │ │
│  │ Prompts      │  │ BaseTool     │  │ RAGService                 │ │
│  └──────────────┘  └──────────────┘  └────────────────────────────┘ │
│                           │                     │                    │
│           ┌───────────────┼─────────────────────┘                    │
│           ▼               ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │                    Concrete Tools                                ││
│  │  • web_search_tool      • fetch_webpage_tool                    ││
│  │  • query_plan_tool      • document_summarize_tool               ││
│  │  • final_report_tool                                             ││
│  └─────────────────────────────────────────────────────────────────┘│
├─────────────────────────────────────────────────────────────────────┤
│                      Infrastructure Layer                            │
│  ┌─────────────────────────────┐  ┌────────────────────────────────┐│
│  │           llm/              │  │          storage/              ││
│  │  LLMClient (OpenAI-compat)  │  │  VectorStore (ChromaDB)        ││
│  │  • ask() / ask_json()       │  │  • add_documents()             ││
│  │  • embed() / embed_batch()  │  │  • query()                     ││
│  └─────────────────────────────┘  └────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
```

---

## 📁 디렉토리 구조

```
veritas/
├── main.py             # CLI 엔트리포인트
├── core/               # 공유 데이터 모델 및 프롬프트 템플릿
├── llm/                # LLM 클라이언트 (llama-server 연동)
├── services/           # 비즈니스 로직 서비스 (RunStore, RAG)
├── storage/            # 영속성 계층 (Vector Store)
├── tools/              # Tool-Use 패턴 구현 (핵심 모듈)
└── workflows/          # 워크플로우 오케스트레이션
```

각 디렉토리별 상세 설명은 해당 폴더 내 `README.md`를 참조하세요.

---

## 🎯 핵심 설계 패턴

### 1. Tool-Use Pattern (도구 사용 패턴)

모든 도구는 `BaseTool`을 상속하고 `ToolRegistry`에 등록되어 일관된 인터페이스로 호출됩니다.

```python
class MyTool(BaseTool):
    @property
    def name(self) -> str:
        return "my_tool"
    
    def run(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="결과")

# Registry를 통한 호출
result = registry.call("my_tool", param1="value1")
```

### 2. Workflow Pattern (워크플로우 패턴)

`AutoSurveyWorkflow`가 여러 도구를 조합하여 전체 파이프라인을 오케스트레이션합니다.

```python
workflow = AutoSurveyWorkflow(registry, run_store_service, max_docs=15)
result = workflow.run_all(user_request="AI 윤리에 대한 최신 동향")
```

### 3. Service Layer Pattern (서비스 레이어 패턴)

`RunStoreService`가 파일 I/O, 레코드 관리, 중복 검사 등 복잡한 상태 관리를 캡슐화합니다.

```python
run_store = RunStoreService(root="./output")
run_store.save_plan(plan_data)
records = run_store.load_records()
```

### 4. Dependency Injection (의존성 주입)

`build_registry()` 팩토리에서 LLM, RunStoreService 등을 주입하여 도구들을 구성합니다.

```python
registry, run_store_service = build_registry(
    llm=llm,
    run_root=output_dir,
    batch_size=5,
    max_context=16384,
)
```

---

## 🔧 새로운 모듈 추가 가이드

### 새 Tool 추가하기

1. **디렉토리 생성**: `tools/my_new_tool/`
2. **스키마 정의**: `tool_schema.json`
3. **Tool 클래스 구현**: `my_new_tool.py`
4. **Loader에 등록**: `tools/loader.py`에서 `build_registry()`에 추가

상세 가이드: [`tools/README.md`](tools/README.md)

### 새 Service 추가하기

도구 간 공유되는 비즈니스 로직은 `services/`에 추가:

```
services/
└── my_service/
    ├── __init__.py
    └── my_service.py
```

상세 가이드: [`services/README.md`](services/README.md)

### 새 Workflow 추가하기

여러 도구를 조합한 파이프라인은 `workflows/`에 추가:

```python
class MyWorkflow:
    def __init__(self, registry, run_store_service):
        self.registry = registry
        self.run_store_service = run_store_service
    
    def run_all(self, **kwargs) -> dict:
        # 단계별 도구 호출
        pass
```

상세 가이드: [`workflows/README.md`](workflows/README.md)

---

## 📐 코드 컨벤션

| 항목 | 규칙 |
|------|------|
| **클래스명** | PascalCase (`WebSearchTool`, `RunStoreService`) |
| **함수/변수명** | snake_case (`build_registry`, `load_records`) |
| **내부 메서드** | `_` 접두사 (`_normalize_results`) |
| **타입 힌트** | 필수 사용 |
| **Docstring** | 클래스 및 공개 메서드에 필수 |

---

## 🔗 의존성 흐름

```
main.py
    │
    ├──▶ workflows/AutoSurveyWorkflow
    │         │
    │         └──▶ tools/ (Registry + 각 Tool)
    │                   │
    │                   ├──▶ services/ (RunStoreService)
    │                   ├──▶ core/ (Models, Prompts)
    │                   └──▶ llm/ (LLMClient)
    │
    └──▶ services/RAGService
              │
              └──▶ storage/VectorStore
```

**의존성 규칙**:
- 상위 → 하위 방향으로만 의존 (순환 참조 금지)
- `core/`는 순수 데이터 클래스/상수만 포함 (의존성 없음)
- `services/`는 `core/`만 의존 가능

---

## 🚀 Quick Start

### CLI 실행

```bash
# 전체 파이프라인 실행
python main.py "AI 윤리에 대한 최신 연구 동향" --output-dir ./output

# 단계별 실행
python main.py --phase plan --output-dir ./output "리서치 주제"
python main.py --phase collect --output-dir ./output
python main.py --phase summarize --output-dir ./output
python main.py --phase final --output-dir ./output

# RAG 채팅 모드
python main.py --phase rag --output-dir ./output
```

### 주요 CLI 옵션

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `--output-dir` | 출력 디렉토리 (필수) | - |
| `--phase` | 실행 단계 (all/plan/collect/summarize/final/rag) | all |
| `--host` | LLM 서버 호스트 | 127.0.0.1 |
| `--port` | LLM 서버 포트 | 8080 |
| `--embed-host/port` | 임베딩 서버 (별도 운영 시) | - |
| `--max-docs` | 최대 수집 문서 수 | 15 |
| `--batch-size` | 배치 요약 크기 | 5 |
| `--no-rag` | 서베이 후 RAG 채팅 건너뛰기 | false |

---

## 📚 참고 문서

- 각 폴더별 상세 가이드: `{폴더명}/README.md`
- Tool 스키마 정의: `tools/{tool_name}/tool_schema.json`
- 프롬프트 템플릿: `core/prompts.py`
