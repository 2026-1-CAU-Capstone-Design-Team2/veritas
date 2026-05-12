# VERITAS Frontend Standalone UI Preview

## 최신 통합 동작

- `python -m frontend.main` 실행 시 프론트엔드는 API bootstrap을 먼저 로드하고, API가 없으면 로컬 FastAPI 서버를 백그라운드로 시작합니다.
- 조사 화면의 `조사 실행` 버튼은 `/api/v1/research/jobs`를 백그라운드 `QThread`에서 호출합니다. AutoSurvey가 오래 걸려도 Qt UI는 응답 없음 상태로 멈추지 않습니다.
- 조사 결과 영역에는 상태, jobId, finalPath, indexedChunks, 전체 문서 수, 총 소요 시간, `summary/index.json` 기반 문서 제목/링크 목록, final report 요약이 표시됩니다.
- 문서 화면의 요약본 영역은 `/api/v1/documents/{workspaceId}/summary`를 호출해 최신 AutoSurvey `final.md`를 markdown으로 렌더링합니다.
- 문서 화면의 수집 문서 영역은 `/api/v1/documents/{workspaceId}/merged`를 호출해 수집된 문서 제목과 링크를 표시합니다.
- 사이드바의 현재 워크스페이스 전환 dropdown은 API bootstrap/workspace 목록을 다시 읽어 `runs/` 하위 각 조사 폴더를 workspace로 표시합니다.
- 새 조사를 시작할 때 API가 먼저 term-grounding을 수행해 첫 `grounded_terms` 문자열로 run folder 이름을 정하고, 이후 해당 폴더가 workspace로 선택 가능합니다.

## 1. 목적

이 README는 `veritas-core/frontend` UI만 단독으로 실행하기 위한 문서입니다.

현재 실행 대상은 PySide6 기반 데스크톱 UI 미리보기입니다. 백엔드, AI 서버, Windows client와의 실제 연동은 포함하지 않으며, 필요한 상태값은 `frontend/api_common.py`의 mock/dummy 데이터로 대체합니다.

## 2. 실행 전 준비

권장 Python 버전:

- Python 3.11 이상
- Python 3.10에서도 동작할 수 있지만, 이 문서는 3.11 이상을 기준으로 합니다.

필요 패키지는 레포 루트의 `requirements.txt`에 정리되어 있습니다. frontend 단독 실행도 루트 의존성 파일을 기준으로 설치합니다.

Windows PowerShell:

```powershell
cd C:\Users\chosw\VERITAS_CAPSTONE\veritas-core\frontend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r ..\requirements.txt
```

macOS/Linux:

```bash
cd /path/to/veritas-core/frontend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r ../requirements.txt
```

## 3. 실행 방법

레포 루트에서 실행:

```powershell
cd C:\Users\chosw\VERITAS_CAPSTONE\veritas-core
python -m frontend.main
```

`frontend` 폴더 안에서 실행:

```powershell
cd C:\Users\chosw\VERITAS_CAPSTONE\veritas-core\frontend
python main.py
```

macOS/Linux에서 레포 루트 기준:

```bash
cd /path/to/veritas-core
python -m frontend.main
```

macOS/Linux에서 `frontend` 폴더 기준:

```bash
cd /path/to/veritas-core/frontend
python main.py
```

## 4. 현재 동작 범위

- `frontend` UI만 실행합니다.
- `MainWindow`, sidebar, dashboard, research, verify, draft, document assist, chat, document, feedback, settings 화면을 표시합니다.
- 실제 백엔드 API, AI 서버, Windows client 연동은 수행하지 않습니다.
- workspace/settings 상태는 `frontend/api_common.py`의 mock/dummy 데이터입니다.
- 채팅 응답과 문서/검증 화면 내용은 UI 확인용 임시 데이터입니다.
- feedback 화면의 파일 업로드는 로컬 파일 텍스트 추출 미리보기 용도이며, 서버 분석을 호출하지 않습니다.

## 5. 문제 해결

### `ModuleNotFoundError`

레포 루트에서 실행할 때는 아래 명령을 사용하세요.

```powershell
python -m frontend.main
```

`frontend` 폴더 안에서 실행할 때는 아래 명령을 사용하세요.

```powershell
python main.py
```

다른 위치에서 실행하면 패키지 경로를 찾지 못할 수 있습니다.

### `No module named 'PySide6'`

PySide6가 설치되지 않은 상태입니다. `frontend` 폴더에서 다음 명령을 실행하세요.

```powershell
pip install -r ..\requirements.txt
```

가상환경을 쓰는 경우, 먼저 가상환경이 활성화되어 있는지 확인하세요.

```powershell
.\.venv\Scripts\Activate.ps1
```

### 실행 위치 문제

지원하는 실행 위치는 두 곳입니다.

- 레포 루트: `python -m frontend.main`
- `frontend` 폴더: `python main.py`

상위 폴더나 다른 서비스 폴더에서 직접 실행하는 방식은 지원하지 않습니다.

### GUI 창이 뜨지 않는 경우

- 데스크톱 GUI 세션에서 실행 중인지 확인하세요.
- 원격 터미널, SSH, headless 환경에서는 PySide6 창이 표시되지 않을 수 있습니다.
- Windows에서 실행했는데 창이 보이지 않으면 작업 표시줄에 `VERITAS` 창이 생성되었는지 확인하세요.
- 설치 확인:

```powershell
python -c "import PySide6; print(PySide6.__version__)"
```

### PowerShell에서 venv 활성화가 막히는 경우

현재 PowerShell 세션에만 실행 정책을 완화한 뒤 다시 활성화하세요.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```
