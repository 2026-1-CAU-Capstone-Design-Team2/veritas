"""Backend helpers for :class:`tools.document_cleanup_tool.DocumentCleanupTool`.

The tool itself stays thin (prompt build + LLM call + JSON parse); the
mechanics of paragraph splitting, removal, and per-doc metadata file writing
live here so they are independently testable and reusable.
"""

from .doc_metadata_writer import write_doc_metadata
from .html_body_extractor import (
    ExtractionResult,
    extract_main_text,
    extract_main_text_with_stats,
    is_structured_payload,
)
from .paragraph_index import (
    annotate_paragraphs,
    apply_boilerplate_removal,
    split_paragraphs,
)
from .response_parser import parse_cleanup_response

__all__ = [
    "ExtractionResult",
    "annotate_paragraphs",
    "apply_boilerplate_removal",
    "extract_main_text",
    "extract_main_text_with_stats",
    "is_structured_payload",
    "parse_cleanup_response",
    "split_paragraphs",
    "write_doc_metadata",
]
