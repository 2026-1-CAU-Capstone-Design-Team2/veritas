"""runs/<workspace>/memory/ 디렉토리의 파일 IO."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from core.memory.models import MemoryItem


class MemoryStore:
    """memory 디렉토리의 경로 + 공통 IO."""

    def __init__(self, workspace_root: Path) -> None:
        """memory/ 와 archival/ 디렉토리를 생성하고 경로를 셋업한다."""
        self.workspace_root = Path(workspace_root)
        self.memory_dir = self.workspace_root / "memory"
        self.archival_dir = self.memory_dir / "archival"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.archival_dir.mkdir(parents=True, exist_ok=True)

        self.working_path = self.memory_dir / "working_context.json"
        self.fifo_path = self.memory_dir / "fifo_queue.jsonl"
        self.recall_path = self.memory_dir / "recall_storage.jsonl"
        self.summaries_path = self.memory_dir / "summaries.jsonl"
        self.invocations_path = self.memory_dir / "invocations.jsonl"
        self.state_path = self.memory_dir / "memory_state.json"
        self.archival_path = self.archival_dir / "items.jsonl"

    def append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        """JSONL 한 줄을 append한다."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read_jsonl_tail(self, path: Path, limit: int = 50) -> list[dict[str, Any]]:
        """JSONL 마지막 limit줄을 dict 리스트로 읽는다."""
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        rows: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows

    def truncate(self, path: Path) -> None:
        """파일을 빈 상태로 만든다."""
        path.write_text("", encoding="utf-8")

    def load_working_context(self) -> str:
        """working_context.json의 content를 반환한다."""
        if not self.working_path.exists():
            return ""
        try:
            data = json.loads(self.working_path.read_text(encoding="utf-8"))
            return str(data.get("content") or "")
        except Exception:
            return ""

    def save_working_context(self, content: str) -> None:
        """working_context.json을 덮어쓴다."""
        self.working_path.write_text(
            json.dumps({"content": content}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_latest_summary(self) -> str:
        """summaries.jsonl 마지막 줄의 summary를 반환한다."""
        rows = self.read_jsonl_tail(self.summaries_path, limit=1)
        if not rows:
            return ""
        return str(rows[-1].get("summary") or "")

    def item_to_dict(self, item: MemoryItem) -> dict[str, Any]:
        """MemoryItem을 JSONL 직렬화용 dict로 변환한다."""
        data = asdict(item)
        data["tier"] = item.tier.value
        data["role"] = item.role.value
        return data
