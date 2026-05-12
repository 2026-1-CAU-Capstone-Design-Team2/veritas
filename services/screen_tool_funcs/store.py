from __future__ import annotations

import json
import threading
import time
from datetime import datetime
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
        self.latest_intervention_path = self.screen_dir / "latest_intervention.json"
        self.capture_log_dir = self.screen_dir / "capture_logs"
        self.capture_session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.capture_log_path = self.capture_log_dir / f"capture_{self.capture_session_id}.jsonl"
        self._lock = threading.RLock()
        self.screen_dir.mkdir(parents=True, exist_ok=True)
        self.capture_log_dir.mkdir(parents=True, exist_ok=True)

    def save_event(self, event: ScreenContextEvent) -> None:
        payload = event.to_dict()
        with self._lock:
            self._write_json_atomic(self.latest_path, payload, indent=2)
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def append_capture_log(self, payload: dict[str, Any]) -> None:
        with self._lock:
            with self.capture_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def load_latest(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.latest_path.exists():
                return None
            try:
                return json.loads(self.latest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None

    def load_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
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
        """Append one approved intervention to the durable FIFO queue.

        Earlier versions used latest_intervention.json as the effective queue and
        rewrote intervention_queue.json with only the newest payload. That made
        screen assists fragile: a newly approved event could overwrite an older
        pending event, and consume_interventions could clear the whole queue even
        when only one item was requested. The chat-side consumer now gets a real
        FIFO queue while latest_intervention.json remains a diagnostic snapshot.
        """
        with self._lock:
            queue = self._load_pending_interventions_unlocked()
            event_id = str(payload.get("event_id") or "").strip()
            if event_id:
                queue = [
                    item
                    for item in queue
                    if str(item.get("event_id") or "").strip() != event_id
                ]
            queue.append(payload)
            queue = queue[-50:]

            self._write_json_atomic(self.latest_intervention_path, payload, indent=2)
            self._write_pending_interventions_unlocked(queue)

            with self.intervention_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def load_pending_interventions(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            queue = self._load_pending_interventions_unlocked()
        if limit is None:
            return queue
        return queue[: max(limit, 0)]

    def _load_pending_interventions_unlocked(self) -> list[dict[str, Any]]:
        if not self.intervention_queue_path.exists():
            return []
        try:
            queue = json.loads(self.intervention_queue_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(queue, list):
            return []
        return [item for item in queue if isinstance(item, dict)]

    def consume_pending_interventions(self, limit: int = 1) -> list[dict[str, Any]]:
        with self._lock:
            queue = self._load_pending_interventions_unlocked()
            count = max(limit, 0)
            consumed = queue[:count]
            remaining = queue[count:]
            if consumed:
                self._write_pending_interventions_unlocked(remaining)
                if remaining:
                    self._write_json_atomic(self.latest_intervention_path, remaining[-1], indent=2)
                else:
                    self._unlink_latest_intervention_unlocked()
            return consumed

    def clear_latest_intervention(self) -> None:
        with self._lock:
            self._unlink_latest_intervention_unlocked()
            self._write_pending_interventions_unlocked([])

    def _unlink_latest_intervention_unlocked(self) -> None:
        try:
            self.latest_intervention_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _write_pending_interventions(self, queue: list[dict[str, Any]]) -> None:
        with self._lock:
            self._write_pending_interventions_unlocked(queue)

    def _write_pending_interventions_unlocked(self, queue: list[dict[str, Any]]) -> None:
        self._write_json_atomic(self.intervention_queue_path, queue, indent=2)

    def _write_json_atomic(self, path: Path, payload: Any, *, indent: int | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=indent),
            encoding="utf-8",
        )
        last_error: OSError | None = None
        for _ in range(5):
            try:
                tmp_path.replace(path)
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.05)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        if last_error is not None:
            raise last_error
