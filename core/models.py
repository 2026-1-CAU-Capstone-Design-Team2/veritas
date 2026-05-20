"""Shared document records.

Two views of the same logical document, intentionally kept as separate
dataclasses because they exist at different lifecycle stages and the
overlap is data-only (no shared behaviour):

* :class:`IndexedDocRecord` — *author-side* row in
  ``runs/<ws>/summary/index.json``. Tracks where each file lives on disk
  and the duplicate-of relationship. Written by
  ``services.run_store_tool_funcs.RunStoreService.write_fetched_record`` /
  ``write_duplicate_record``.

* :class:`ParsedDocRecord` — *reader-side* doc loaded into the verification
  layer. Carries the parsed content of ``summary/doc_<id>.md`` (summary,
  key_points, reliability_notes, keywords) and the post-cleanup body
  (``clean_md_text``). Built by
  ``services.verification.artifact_loader.ArtifactLoader.load_docs``.

The two share six identity fields (``doc_id``, ``title``, ``url``,
``final_url``, ``domain``, ``search_query``) but otherwise diverge. We
deliberately do *not* abstract those six into a shared base — there is no
polymorphic call site that would benefit, and dataclass inheritance with
default-field ordering is fragile enough to outweigh the small DRY win.
If a future change ever needs to treat both records uniformly, the right
move is to introduce a tiny ``DocIdentity`` value object then; today,
plain duplication is the honest design.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class IndexedDocRecord:
    """A row in ``runs/<ws>/summary/index.json``.

    Author-side view: identifies a document by its stable fields and the
    on-disk paths the autosurvey pipeline wrote it to. ``duplicate_of`` is
    set when the fetched URL turned out to match an already-collected
    document (cleanup-content-hash dedup); ``duplicate_score`` is the
    similarity score that triggered the dedup decision.

    The required positional fields match the ``DocRecord`` constructor
    that used to live here, so existing kwargs-style call sites
    (``DocRecord(doc_id=..., title=..., ...)``) keep working with the
    rename alone.
    """

    doc_id: str
    title: str
    url: str
    final_url: str
    domain: str
    search_query: str
    text_path: str
    html_path: str
    summary_path: str
    duplicate_of: Optional[str] = None
    duplicate_score: float = 0.0


@dataclass
class ParsedDocRecord:
    """A research document loaded into the verification layer.

    Reader-side view: ``index.json`` metadata + parsed ``doc_<id>.md``
    sections + the post-cleanup body (``clean_md_text``).

    Duplicate documents (``index.json`` ``duplicate_of`` set) carry no
    clean_md / chunks but are still loaded — consensus / diversity need to
    see them — with ``is_duplicate=True`` and an empty ``clean_md_text``.
    Fetch-error stubs (``doc_<id>_error.md``) are skipped entirely by the
    loader so they never reach this dataclass.
    """

    doc_id: str
    title: str = ""
    url: str = ""
    final_url: str = ""
    domain: str = ""
    search_query: str = ""
    duplicate_of: str | None = None
    is_duplicate: bool = False
    summary: str = ""
    key_points: list[str] = field(default_factory=list)
    reliability_notes: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    clean_md_text: str = ""  # full post-cleanup markdown; "" for duplicates


__all__ = ["IndexedDocRecord", "ParsedDocRecord"]
