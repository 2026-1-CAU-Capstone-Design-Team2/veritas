from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .knowledge_models import SourceKind


@dataclass(frozen=True)
class TableColumnProfile:
    name: str
    inferred_type: str
    null_count: int
    non_null_count: int
    sample_values: list[str]
    min_value: float | None = None
    max_value: float | None = None
    mean_value: float | None = None


@dataclass(frozen=True)
class TableProfile:
    source_id: str
    sheet_name: str | None
    row_count: int
    column_count: int
    columns: list[TableColumnProfile]
    sample_rows_markdown: str
    summary_markdown: str


@dataclass(frozen=True)
class LocalFileManifestEntry:
    source_id: str
    root_id: str
    absolute_path: str
    relative_path: str
    file_name: str
    extension: str
    size_bytes: int
    modified_at: str
    content_hash: str
    parser_status: str
    parser_error: str = ""
    extracted_text_path: str = ""
    table_profile_path: str = ""


@dataclass(frozen=True)
class ParsedLocalDocument:
    source_id: str
    source_kind: SourceKind
    markdown_text: str
    table_profiles: list[TableProfile] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LocalCorpusIndexResult:
    workspace_id: str
    indexed_count: int
    skipped_count: int
    failed_count: int
    vector_count: int
    sources: list[dict[str, Any]]
    manifest_path: str


@dataclass(frozen=True)
class LocalCorpusMutationResult:
    workspace_id: str
    removed_count: int
    source_ids: list[str]


__all__ = [
    "LocalCorpusIndexResult",
    "LocalCorpusMutationResult",
    "LocalFileManifestEntry",
    "ParsedLocalDocument",
    "TableColumnProfile",
    "TableProfile",
]
