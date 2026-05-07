from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import ScreenContextEvent


class ScreenContextStore:
    """screen context event를 latest JSON과 append-only JSONL로 저장합니다."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.screen_dir = self.root / "screen_context"
        self.events_path = self.screen_dir / "events.jsonl"
        self.latest_path = self.screen_dir / "latest.json"
        self.intervention_log_path = self.screen_dir / "interventions.jsonl"
        self.intervention_queue_path = self.screen_dir / "intervention_queue.json"
        self.screen_dir.mkdir(parents=True, exist_ok=True)

    def save_event(self, event: ScreenContextEvent) -> None:
        payload = event.to_dict()
        self.latest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def load_latest(self) -> dict[str, Any] | None:
        if not self.latest_path.exists():
            return None
        return json.loads(self.latest_path.read_text(encoding="utf-8"))

    def load_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []

        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        recent_lines = lines[-max(limit, 0):]
        records: list[dict[str, Any]] = []
        for line in recent_lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def enqueue_intervention(self, payload: dict[str, Any]) -> None:
        queue = self.load_pending_interventions()
        if not any(item.get("event_id") == payload.get("event_id") for item in queue):
            queue.append(payload)
            self._write_pending_interventions(queue)

        with self.intervention_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def load_pending_interventions(self, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.intervention_queue_path.exists():
            return []
        try:
            queue = json.loads(self.intervention_queue_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if not isinstance(queue, list):
            return []
        if limit is None:
            return queue
        return queue[: max(limit, 0)]

    def consume_pending_interventions(self, limit: int = 1) -> list[dict[str, Any]]:
        queue = self.load_pending_interventions()
        count = max(limit, 0)
        consumed = queue[:count]
        remaining = queue[count:]
        self._write_pending_interventions(remaining)
        return consumed

    def _write_pending_interventions(self, queue: list[dict[str, Any]]) -> None:
        self.intervention_queue_path.write_text(
            json.dumps(queue, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
