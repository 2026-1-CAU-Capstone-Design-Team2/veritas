from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SourceScope(str, Enum):
    EXTERNAL = "external"
    LOCAL = "local"


class SourceKind(str, Enum):
    WEB_PAGE = "web_page"
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    MARKDOWN = "markdown"
    XLSX = "xlsx"
    CSV = "csv"
    TABLE_SUMMARY = "table_summary"
    UNKNOWN = "unknown"


class PrivacyLabel(str, Enum):
    PUBLIC_WEB = "public_web"
    LOCAL_PRIVATE = "local_private"
    LOCAL_APPROVED_EXTERNAL = "local_approved_external"


@dataclass(frozen=True)
class KnowledgeSourceRecord:
    source_id: str
    workspace_id: str
    source_scope: SourceScope
    source_kind: SourceKind
    title: str
    canonical_uri: str
    display_path: str
    privacy_label: PrivacyLabel
    content_hash: str
    created_at: str = ""
    modified_at: str = ""
    parser_version: str = ""
    status: str = "indexed"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeChunkRecord:
    chunk_id: str
    source_id: str
    workspace_id: str
    source_scope: SourceScope
    source_kind: SourceKind
    text: str
    chunk_index: int
    chunk_count: int
    page_start: int | None = None
    page_end: int | None = None
    sheet_name: str | None = None
    row_start: int | None = None
    row_end: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    source_id: str
    workspace_id: str
    source_scope: SourceScope
    source_kind: SourceKind
    privacy_label: PrivacyLabel
    text: str
    title: str = ""
    display_path: str = ""
    distance: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "KnowledgeChunkRecord",
    "KnowledgeSourceRecord",
    "PrivacyLabel",
    "RetrievedChunk",
    "SourceKind",
    "SourceScope",
]
