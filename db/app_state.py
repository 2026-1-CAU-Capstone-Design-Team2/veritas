from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .db import get_connection, init_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(key: str, default: Any = None) -> Any:
    init_db()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = ?",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return default
    try:
        return json.loads(str(row["value"]))
    except json.JSONDecodeError:
        return default


def write_json(key: str, value: Any) -> None:
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO app_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, json.dumps(value, ensure_ascii=False), _now()),
        )
        conn.commit()
    finally:
        conn.close()
