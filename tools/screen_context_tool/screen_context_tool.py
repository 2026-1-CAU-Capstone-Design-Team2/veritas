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

    def run(
        self,
        action: str,
        limit: int = 10,
        *,
        event_id: str = "",
        intervention_type: str = "",
        feedback_action: str = "",
        reward: float = 0.0,
        intervention: dict[str, Any] | None = None,
        answer_text: str = "",
    ) -> ToolResult:
        try:
            action = str(action or "").strip()
            try:
                safe_limit = max(0, min(int(limit), 50))
            except Exception:
                safe_limit = 10

            if action == "capture_once":
                event = self._screen_context_service.capture_once()
                payload = event.to_dict()
                diagnostics = payload.get("diagnostics") or {}
                if not diagnostics.get("has_foreground_window"):
                    error = self._diagnostic_error(
                        "Screen capture failed",
                        diagnostics,
                        fallback="No usable foreground window.",
                    )
                    print(f"[screen_context][tool][warn] action=capture_once {error}")
                    return ToolResult(
                        success=False,
                        error=error,
                        data=payload,
                    )
                if not diagnostics.get("has_text"):
                    error = self._diagnostic_error(
                        "Screen capture succeeded, but no readable text was extracted",
                        diagnostics,
                        fallback="No readable text.",
                    )
                    print(f"[screen_context][tool][warn] action=capture_once {error}")
                    return ToolResult(
                        success=False,
                        error=error,
                        data=payload,
                    )
                return ToolResult(
                    success=True,
                    content=event.filtered.active_editor_text,
                    data=payload,
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

            if action == "mark_card_shown":
                # 카드가 화면에 렌더되기 시작했음을 게이트에 알림 (runtime의
                # on_answer 콜백 경유). intervention dict가 문서/단락 식별자를 가짐.
                if not isinstance(intervention, dict):
                    return ToolResult(success=False, error="mark_card_shown requires intervention dict")
                self._screen_context_service.mark_card_shown(
                    intervention, answer=str(answer_text or "")
                )
                return ToolResult(
                    success=True,
                    data={"unresolved_card": self._screen_context_service.unresolved_card_gate.snapshot()},
                )

            if action == "resolve_card":
                event_id = str(event_id or "").strip()
                if not event_id:
                    return ToolResult(success=False, error="resolve_card requires event_id")
                resolved = self._screen_context_service.resolve_card(
                    event_id, feedback_action=str(feedback_action or "")
                )
                return ToolResult(success=True, data={"resolved": resolved})

            if action == "record_feedback":
                event_id = str(event_id or "").strip()
                if not event_id:
                    return ToolResult(success=False, error="record_feedback requires event_id")
                from datetime import datetime, timezone
                record = {
                    "event_id": event_id,
                    "intervention_type": str(intervention_type or "").strip() or "none",
                    "action": str(feedback_action or "").strip(),
                    "reward": float(reward),
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }
                self._screen_context_service.store.append_intervention_feedback(record)
                return ToolResult(success=True, data=record)

            if action == "start_polling":
                self._screen_context_service.start_polling()
                return ToolResult(success=True, data={"polling": True})

            if action == "stop_polling":
                self._screen_context_service.stop_polling()
                return ToolResult(success=True, data={"polling": False})

            if action == "status":
                latest = self._screen_context_service.store.load_latest() or {}
                pending = self._screen_context_service.store.load_pending_interventions()
                latest_intervention = pending[0] if pending else {}
                return ToolResult(
                    success=True,
                    data={
                        "polling": self._screen_context_service.is_polling(),
                        "last_poll_error": self._screen_context_service.last_poll_error(),
                        "latest_event_id": latest.get("event_id"),
                        "latest_captured_at": latest.get("captured_at"),
                        "latest_diagnostics": latest.get("diagnostics") or {},
                        "pending_intervention_count": len(pending),
                        "latest_intervention_event_id": latest_intervention.get("event_id"),
                        "latest_intervention_captured_at": latest_intervention.get("captured_at"),
                        "capture_log_path": str(self._screen_context_service.store.capture_log_path),
                        "unresolved_card": self._screen_context_service.unresolved_card_gate.snapshot(),
                    },
                )

            return ToolResult(success=False, error=f"Unknown action: {action}")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

    def _diagnostic_error(
        self,
        message: str,
        diagnostics: dict[str, Any],
        *,
        fallback: str,
    ) -> str:
        errors = diagnostics.get("errors") if isinstance(diagnostics, dict) else {}
        if not isinstance(errors, dict):
            errors = {}
        details = [
            f"text_source={diagnostics.get('text_source', 'unknown')}",
            f"confidence={diagnostics.get('confidence', 0.0)}",
            f"active_text_chars={diagnostics.get('active_text_chars', 0)}",
        ]
        for key in ("window", "app_text", "ui_automation", "ocr"):
            value = errors.get(key)
            if value:
                details.append(f"{key}_error={value}")
        if len(details) <= 3:
            details.append(f"reason={fallback}")
        return f"{message}. " + "; ".join(details)
