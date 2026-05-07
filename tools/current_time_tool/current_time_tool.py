from __future__ import annotations

from datetime import datetime
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from tools.tool import BaseTool, ToolResult


class CurrentTimeTool(BaseTool):
    def __init__(self, schema: dict[str, Any]) -> None:
        super().__init__(schema=schema)

    @property
    def name(self) -> str:
        return "current_time"

    def run(self, timezone: str | None = None) -> ToolResult:
        timezone_text = str(timezone or "").strip()

        try:
            if timezone_text:
                if ZoneInfo is None:
                    return ToolResult(
                        success=False,
                        error="Timezone support is unavailable on this Python runtime.",
                    )
                now = datetime.now(ZoneInfo(timezone_text))
            else:
                now = datetime.now().astimezone()
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to resolve current time: {e}")

        payload = {
            "current_datetime_iso": now.isoformat(timespec="seconds"),
            "current_date": now.date().isoformat(),
            "current_year": now.year,
            "current_month": now.month,
            "current_day": now.day,
            "timezone": str(now.tzinfo or ""),
        }

        return ToolResult(
            success=True,
            content=payload["current_datetime_iso"],
            data=payload,
        )
