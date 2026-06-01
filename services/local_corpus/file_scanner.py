from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from core.local_corpus_models import LocalFileManifestEntry


DEFAULT_ALLOWED_EXTENSIONS = {".md", ".txt", ".pdf", ".docx", ".xlsx", ".csv"}
DEFAULT_MAX_FILE_SIZE_MB = 50
DEFAULT_MAX_FILES = 300
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    "chromadb",
    ".venv",
    "venv",
}
_TEMP_PREFIXES = ("~$", ".~", ".#")


class FileScanner:
    def __init__(
        self,
        *,
        allowed_extensions: set[str] | None = None,
        max_file_size_mb: int = DEFAULT_MAX_FILE_SIZE_MB,
        max_files: int = DEFAULT_MAX_FILES,
    ) -> None:
        self.allowed_extensions = {
            ext.lower() if ext.startswith(".") else f".{ext.lower()}"
            for ext in (allowed_extensions or DEFAULT_ALLOWED_EXTENSIONS)
        }
        self.max_file_size = max(1, int(max_file_size_mb)) * 1024 * 1024
        self.max_files = max(1, int(max_files))

    def scan(self, roots: list[str]) -> list[LocalFileManifestEntry]:
        entries: list[LocalFileManifestEntry] = []
        for root_index, root_value in enumerate(roots):
            root = Path(root_value).expanduser()
            if not root.exists():
                continue
            root = root.resolve()
            root_id = f"root_{root_index:03d}"
            candidates = [root] if root.is_file() else self._walk(root)
            for path in candidates:
                if len(entries) >= self.max_files:
                    return entries
                entry = self._entry_for(path, root=root, root_id=root_id)
                if entry is not None:
                    entries.append(entry)
        return entries

    def _walk(self, root: Path) -> Iterable[Path]:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part.startswith(".") or part in _SKIP_DIRS for part in path.parts):
                continue
            yield path

    def _entry_for(
        self,
        path: Path,
        *,
        root: Path,
        root_id: str,
    ) -> LocalFileManifestEntry | None:
        name = path.name
        if name.startswith(_TEMP_PREFIXES):
            return None
        suffix = path.suffix.lower()
        if suffix not in self.allowed_extensions:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        if stat.st_size > self.max_file_size:
            return LocalFileManifestEntry(
                source_id=self._source_id(path, root=root),
                root_id=root_id,
                absolute_path=str(path),
                relative_path=self._relative(path, root),
                file_name=name,
                extension=suffix,
                size_bytes=int(stat.st_size),
                modified_at=self._mtime(stat.st_mtime),
                content_hash="",
                parser_status="skipped_too_large",
                parser_error=f"file exceeds {self.max_file_size} bytes",
            )
        content_hash = self._sha256(path)
        return LocalFileManifestEntry(
            source_id=self._source_id(path, root=root),
            root_id=root_id,
            absolute_path=str(path),
            relative_path=self._relative(path, root),
            file_name=name,
            extension=suffix,
            size_bytes=int(stat.st_size),
            modified_at=self._mtime(stat.st_mtime),
            content_hash=content_hash,
            parser_status="pending",
        )

    def _source_id(self, path: Path, *, root: Path) -> str:
        try:
            seed = str(path.resolve()).replace("\\", "/").lower()
        except Exception:
            seed = f"{str(root)}:{self._relative(path, root)}".replace("\\", "/").lower()
        return f"local_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:16]}"

    def _relative(self, path: Path, root: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return path.name

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _mtime(self, value: float) -> str:
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_ALLOWED_EXTENSIONS",
    "DEFAULT_MAX_FILE_SIZE_MB",
    "DEFAULT_MAX_FILES",
    "FileScanner",
]
