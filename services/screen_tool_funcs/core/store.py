from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ScreenContextEvent


class ScreenContextStore:
    """screen context event를 latest JSON과 append-only JSONL로 저장합니다."""

    EVENTS_ROTATE_MAX_BYTES = 5 * 1024 * 1024
    EVENTS_ROTATE_KEEP = 5
    INTERVENTION_LOG_ROTATE_MAX_BYTES = 5 * 1024 * 1024
    INTERVENTION_LOG_ROTATE_KEEP = 5
    INTERVENTION_FEEDBACK_ROTATE_MAX_BYTES = 5 * 1024 * 1024
    INTERVENTION_FEEDBACK_ROTATE_KEEP = 5
    CAPTURE_LOG_ROTATE_MAX_BYTES = 10 * 1024 * 1024
    CAPTURE_LOG_ROTATE_KEEP = 3
    CAPTURE_LOG_SESSION_RETENTION = 20

    def __init__(self, root: str | Path, *, debug: bool = False) -> None:
        self.root = Path(root)
        self.screen_dir = self.root / "screen_context"
        self.events_path = self.screen_dir / "events.jsonl"
        self.latest_path = self.screen_dir / "latest.json"
        self.intervention_log_path = self.screen_dir / "interventions.jsonl"
        # Append-only reward log: one line per user reaction to a shown
        # intervention. Joins to interventions.jsonl on event_id (and carries a
        # denormalized intervention_type) to form the (context, scenario, reward)
        # dataset a future contextual bandit learns the selection policy from.
        self.intervention_feedback_path = self.screen_dir / "intervention_feedback.jsonl"
        self.intervention_queue_path = self.screen_dir / "intervention_queue.json"
        self.latest_intervention_path = self.screen_dir / "latest_intervention.json"
        # debug 모드면 capture log를 capture_logs/ 대신 debug/에 기록
        self.capture_log_dir = self.screen_dir / ("debug" if debug else "capture_logs")
        self.scheduler_dir = self.screen_dir / "scheduler_state"
        self.capture_session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.capture_log_path = self.capture_log_dir / f"capture_{self.capture_session_id}.jsonl"
        self._lock = threading.RLock()
        self.screen_dir.mkdir(parents=True, exist_ok=True)
        self.capture_log_dir.mkdir(parents=True, exist_ok=True)
        self.scheduler_dir.mkdir(parents=True, exist_ok=True)
        self._prune_capture_log_sessions()

    def save_event(self, event: ScreenContextEvent) -> None:
        payload = event.to_dict()
        with self._lock:
            self._write_json_atomic(self.latest_path, payload, indent=2)
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._maybe_rotate(
                self.events_path,
                max_bytes=self.EVENTS_ROTATE_MAX_BYTES,
                keep=self.EVENTS_ROTATE_KEEP,
            )

    def append_capture_log(self, payload: dict[str, Any]) -> None:
        with self._lock:
            with self.capture_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._maybe_rotate(
                self.capture_log_path,
                max_bytes=self.CAPTURE_LOG_ROTATE_MAX_BYTES,
                keep=self.CAPTURE_LOG_ROTATE_KEEP,
            )

    def append_intervention_feedback(self, payload: dict[str, Any]) -> None:
        with self._lock:
            with self.intervention_feedback_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._maybe_rotate(
                self.intervention_feedback_path,
                max_bytes=self.INTERVENTION_FEEDBACK_ROTATE_MAX_BYTES,
                keep=self.INTERVENTION_FEEDBACK_ROTATE_KEEP,
            )

    def load_latest(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.latest_path.exists():
                return None
            try:
                return json.loads(self.latest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None

    def load_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most recent `limit` events, scanning rotated files if needed.

        Reads tail-first to avoid pulling the entire JSONL into memory on every
        capture. Falls back into events.jsonl.1 when the live file does not yet
        hold enough lines (e.g. immediately after rotation).
        """
        if limit <= 0:
            return []
        with self._lock:
            lines = self._tail_lines(self.events_path, limit)
            if len(lines) < limit:
                rotated = self.events_path.with_name(f"{self.events_path.name}.1")
                if rotated.exists():
                    deficit = limit - len(lines)
                    lines = self._tail_lines(rotated, deficit) + lines
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records[-limit:]

    def enqueue_intervention(self, payload: dict[str, Any]) -> None:
        # 단일 슬롯 큐로 구현.
        with self._lock:
            queue = [payload]

            self._write_json_atomic(self.latest_intervention_path, payload, indent=2)
            self._write_pending_interventions_unlocked(queue)

            with self.intervention_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._maybe_rotate(
                self.intervention_log_path,
                max_bytes=self.INTERVENTION_LOG_ROTATE_MAX_BYTES,
                keep=self.INTERVENTION_LOG_ROTATE_KEEP,
            )

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

    def scheduler_state_path(self, document_key: str) -> Path:
        safe = self._safe_document_filename(document_key)
        return self.scheduler_dir / f"{safe}.json"

    def load_scheduler_state(self, document_key: str) -> dict[str, Any] | None:
        path = self.scheduler_state_path(document_key)
        with self._lock:
            if not path.exists():
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        return data if isinstance(data, dict) else None

    def save_scheduler_state(self, document_key: str, payload: dict[str, Any]) -> None:
        path = self.scheduler_state_path(document_key)
        with self._lock:
            self._write_json_atomic(path, payload, indent=2)

    def _safe_document_filename(self, document_key: str) -> str:
        normalized = (document_key or "unknown").strip() or "unknown"
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", normalized)[:48].strip("_") or "doc"
        return f"{slug}_{digest}"

    def _tail_lines(self, path: Path, n: int, *, block_size: int = 8192) -> list[str]:
        """Return the last `n` lines of `path` without reading the whole file.

        Reads backwards from EOF in `block_size` chunks until we've collected
        enough newlines, then decodes the joined bytes once. Bytes that span a
        UTF-8 boundary are tolerated via errors="replace" on the final decode.
        """
        if n <= 0:
            return []
        try:
            with path.open("rb") as f:
                f.seek(0, 2)
                pos = f.tell()
                if pos == 0:
                    return []
                chunks: list[bytes] = []
                newline_count = 0
                while pos > 0 and newline_count <= n:
                    read_size = min(block_size, pos)
                    pos -= read_size
                    f.seek(pos)
                    chunk = f.read(read_size)
                    chunks.append(chunk)
                    newline_count += chunk.count(b"\n")
                data = b"".join(reversed(chunks))
        except OSError:
            return []
        text = data.decode("utf-8", errors="replace")
        lines = [line for line in text.splitlines() if line]
        return lines[-n:]

    def _maybe_rotate(self, path: Path, *, max_bytes: int, keep: int) -> None:
        """Rotate `path` to `path.1` … `path.keep` when it grows past `max_bytes`.

        Caller must hold self._lock. Best-effort: any individual rename/unlink
        failure is swallowed so that the active append path stays unblocked.
        """
        if max_bytes <= 0 or keep <= 0:
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < max_bytes:
            return

        oldest = path.with_name(f"{path.name}.{keep}")
        try:
            oldest.unlink(missing_ok=True)
        except OSError:
            pass
        for index in range(keep - 1, 0, -1):
            src = path.with_name(f"{path.name}.{index}")
            dst = path.with_name(f"{path.name}.{index + 1}")
            if not src.exists():
                continue
            try:
                src.replace(dst)
            except OSError:
                pass
        try:
            path.replace(path.with_name(f"{path.name}.1"))
        except OSError:
            pass

    def _prune_capture_log_sessions(self) -> None:
        """capture_log_dir의 capture 세션 파일을 mtime 기준 최근 RETENTION개만 남김."""
        retention = max(self.CAPTURE_LOG_SESSION_RETENTION, 1)
        try:
            files = list(self.capture_log_dir.glob("capture_*.jsonl*"))
        except OSError:
            return
        if len(files) <= retention:
            return
        try:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for path in files[retention:]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

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
