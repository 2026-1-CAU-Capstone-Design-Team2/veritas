# VERITAS Local SQLite DB

이 폴더는 설치형 데스크톱 앱 VERITAS가 사용하는 로컬 SQLite 저장소 계층입니다. FastAPI 서버, 외부 DB 서버, 별도 포트를 사용하지 않고 사용자 로컬 앱 데이터 폴더의 SQLite 파일 하나만 사용합니다.

## 저장 위치

기본 DB 파일은 다음 위치에 생성됩니다.

```text
C:\Users\<사용자>\AppData\Local\VERITAS\veritas.db
```

경로 생성은 [db.py](./db.py)의 `pathlib.Path` 기반 함수가 담당합니다.

- `get_app_data_dir()`: 사용자 로컬 앱 데이터 폴더 아래 `VERITAS` 디렉터리 반환
- `get_db_path()`: `veritas.db` 전체 경로 반환
- `get_connection()`: SQLite 연결 생성, `sqlite3.Row` 적용, WAL 모드 적용
- `init_db()`: DB 파일과 테이블 자동 생성

Windows에서는 우선 `LOCALAPPDATA` 환경 변수를 사용합니다. 값이 없으면 `Path.home() / "AppData" / "Local" / "VERITAS"`를 fallback으로 사용합니다.

## 실행 흐름

1. API 프로세스 부팅 시(launcher / `db.workspace_sync`) `init_db()`가 호출됩니다. (대시보드 repository도 첫 조회 시 방어적으로 `init_db()`를 호출합니다.)
2. DB 디렉터리가 없으면 자동 생성합니다.
3. `veritas.db` 파일이 없으면 SQLite가 자동 생성합니다.
4. [schema.py](./schema.py)의 `CREATE TABLE IF NOT EXISTS` 구문으로 필요한 테이블을 생성합니다.
5. 프론트엔드 대시보드는 코어를 직접 호출하지 않고 **HTTP `GET /api/v1/dashboard/home`** 를 호출합니다. (이름 변경은 `POST /api/v1/dashboard/workspaces/{id}/rename`.)
6. 해당 라우트는 `api/services/dashboard_service.py`의 `get_home_summary()`로, 이 함수가 [dashboard_repository.py](./dashboard_repository.py)의 SELECT 결과를 UI에서 바로 쓰기 좋은 dict로 가공합니다. (repository는 `db.activity_repository`처럼 db 계층에 남아 API 서비스가 사용합니다.)
7. PySide6 `QTimer`가 4초마다 대시보드 데이터를 다시 로드합니다.

## SQLite 설정

`get_connection()`은 매 연결마다 다음 설정을 적용합니다.

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
```

WAL 모드는 데스크톱 앱에서 읽기와 쓰기가 섞일 때 기본 rollback journal보다 동시성에 유리합니다. 이 설정 때문에 DB 파일 옆에 `veritas.db-wal`, `veritas.db-shm` 파일이 같이 생길 수 있으며 정상 동작입니다.

## 테이블

현재 스키마는 대시보드 조회에 필요한 최소 테이블로 구성되어 있습니다.

### workspaces

워크스페이스 기본 정보와 최근 작업 시각을 저장합니다.

```sql
id TEXT PRIMARY KEY
name TEXT NOT NULL
path TEXT NOT NULL
status TEXT DEFAULT 'active'
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
last_worked_at TEXT
```

### documents

문서 메타데이터와 처리 상태를 저장합니다.

```sql
id TEXT PRIMARY KEY
workspace_id TEXT
title TEXT NOT NULL
file_path TEXT
document_type TEXT
status TEXT
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

### activity_logs

대시보드의 최근 작업 목록에 표시할 이벤트 로그를 저장합니다.

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT
workspace_id TEXT
document_id TEXT
action TEXT NOT NULL
description TEXT
created_at TEXT NOT NULL
```

사용 중인 주요 `action` 예시는 다음과 같습니다.

- `document_uploaded`
- `draft_created`
- `validation_completed`
- `feedback_completed`
- `workspace_opened`

### feedbacks

피드백 결과 메타데이터를 저장합니다. 긴 피드백 본문은 추후 Markdown 파일로 분리 저장할 수 있도록 `content_path`를 둡니다.

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT
document_id TEXT NOT NULL
status TEXT DEFAULT 'completed'
content_path TEXT
created_at TEXT NOT NULL
```

## 대시보드 조회 기준

[dashboard_repository.py](./dashboard_repository.py)는 다음 값을 SELECT합니다.

- 처리 문서 수: `documents.status IN ('validated', 'feedback_completed', 'completed')`
- 검증 완료 워크스페이스 수: `workspaces.status = 'validated'`
- 피드백 완료 문서 수: `documents.status IN ('feedback_completed', 'completed')`
- 최근 작업 워크스페이스: `workspaces.last_worked_at` 최신순 5개
- 최근 문서/피드백: `activity_logs.created_at` 최신순 5개

`api/services/dashboard_service.py`의 `get_home_summary()`는 피드백 완료율을 계산합니다.

```text
feedback_rate = feedback_completed_docs / total_docs * 100
```

문서가 0개이면 `0`으로 반환합니다.

## 반환 데이터 형태

UI는 service 결과만 사용합니다.

```python
{
    "processed_docs": 48,
    "validated_workspaces": 6,
    "feedback_rate": 98,
    "recent_workspaces": [
        {
            "id": "ws_demo_001",
            "name": "AI 안전성 브리프 워크스페이스",
            "last_worked_at": "2026-05-11 15:30:00",
        }
    ],
    "recent_activities": [
        {
            "action": "feedback_completed",
            "description": "2026_Q2_리스크_브리프.docx 피드백 완료",
            "created_at": "2026-05-11 15:40:00",
        }
    ],
}
```

## UI 연동

대시보드 연동은 [../frontend/ui/pages/dashboard_page.py](../frontend/ui/pages/dashboard_page.py)에 있습니다. 프론트엔드는 DB를 직접 import하지 않고 `AgentController`(HTTP)만 사용합니다.

- `load_dashboard_data()`: `AgentController.get_dashboard_home()`(HTTP) 결과를 카드와 최근 목록에 반영
- `_rename_workspace()`: `AgentController.rename_workspace()`(HTTP)로 이름 변경
- `refresh()`: 문서 추가, 피드백 완료, 작업 완료 같은 이벤트에서 호출할 수 있는 공개 갱신 함수
- `QTimer`: 4초마다 자동 갱신

## 개발용 시드 데이터

[db.py](./db.py)의 `seed_demo_data()`는 개발 중 대시보드 확인을 위한 선택 함수입니다. 실제 앱 시작 시 자동 호출하지 않습니다.

중복 삽입 방지를 위해 `documents` 테이블에 데이터가 하나라도 있으면 아무 것도 삽입하지 않습니다.

사용 예:

```powershell
@'
from db.db import init_db, seed_demo_data
from api.services.dashboard_service import get_home_summary

init_db()
seed_demo_data()
print(get_home_summary())
'@ | python -B -
```

seed 데이터는 처리 문서 48개, 검증 완료 워크스페이스 6개, 피드백 완료율 약 98%가 나오도록 구성되어 있습니다.

## 주의사항

- 대시보드 핵심 저장소는 SQLite 파일입니다.
- AI가 생성한 긴 초안/피드백 본문은 추후 별도 파일로 저장할 수 있지만, DB에는 해당 파일 경로를 저장합니다.
- DB 파일은 앱 설치 폴더가 아니라 사용자 로컬 앱 데이터 폴더에 생성됩니다.
- 서버 실행이나 포트 점유가 필요한 코드는 이 계층에 추가하지 않습니다.
