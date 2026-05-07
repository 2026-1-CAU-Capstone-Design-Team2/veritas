from __future__ import annotations

from typing import Any

from services.screen_tool_funcs import ScreenContextService
from tools.tool import BaseTool, ToolResult


class ScreenContextTool(BaseTool):
    """LLM이 screen context service의 결과를 조회/제어하는 tool입니다."""

    def __init__(self, schema: dict[str, Any], screen_context_service: ScreenContextService) -> None:
        super().__init__(schema=schema)
        self._screen_context_service = screen_context_service

    @property
    def name(self) -> str:
        return "screen_context"

    def run(self, action: str, limit: int = 10) -> ToolResult:
        try:
            action = str(action or "").strip()
            try:
                safe_limit = max(0, min(int(limit), 50))
            except Exception:
                safe_limit = 10

            if action == "capture_once":
                event = self._screen_context_service.capture_once()
                return ToolResult(
                    success=True,
                    content=event.filtered.active_editor_text,
                    data=event.to_dict(),
                )

            if action == "latest":
                latest = self._screen_context_service.store.load_latest()
                return ToolResult(success=True, data=latest or {})

            if action == "recent":
                recent = self._screen_context_service.store.load_recent(limit=safe_limit)
                return ToolResult(success=True, data={"events": recent})

            if action == "pending_interventions":
                pending = self._screen_context_service.store.load_pending_interventions(limit=safe_limit)
                return ToolResult(success=True, data={"interventions": pending})

            if action == "consume_interventions":
                consumed = self._screen_context_service.store.consume_pending_interventions(limit=safe_limit)
                return ToolResult(success=True, data={"interventions": consumed})

            if action == "start_polling":
                self._screen_context_service.start_polling()
                return ToolResult(success=True, data={"polling": True})

            if action == "stop_polling":
                self._screen_context_service.stop_polling()
                return ToolResult(success=True, data={"polling": False})

            if action == "status":
                return ToolResult(
                    success=True,
                    data={
                        "polling": self._screen_context_service.is_polling(),
                        "last_poll_error": self._screen_context_service.last_poll_error(),
                    },
                )

            return ToolResult(success=False, error=f"Unknown action: {action}")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
