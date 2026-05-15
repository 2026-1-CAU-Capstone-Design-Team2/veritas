"""Disk -> in-memory domain model loader for the verification layer.

Together with ``persistence.py`` this is the *only* module that touches disk
(VERIFY_DESIGN.md §1.2); every other module stays close to a pure function.

It reads an existing ``runs/<workspace>/`` produced by AutoSurvey:

* ``summary/index.json``      -> document metadata
* ``summary/doc_<id>.md``     -> per-doc Summary / Key Points / Reliability Notes
* ``summary/plan.json``       -> kept as a dict (LLM-authored external input)
* ``summary/grounding.json``  -> kept as a dict
* ``summary/request.md``      -> raw request text
* ``clean_md/<id>.md``        -> full cleaned body
* ``chromadb/``               -> embedded chunk vectors + metadata

Paths are re-derived from the workspace directory via ``RunPathManager`` rather
than trusting the absolute paths baked into ``index.json`` at generation time —
those break the moment a workspace is moved or copied.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import numpy as np

from services.run_store_tool_funcs.path_manager import RunPathManager

from .indexing.dense_index import l2_normalize
from .models import ChunkRecord, DocRecord, KeyPointRecord

logger = logging.getLogger(__name__)

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$", re.M)
_CHUNK_COLLECTION = "research_docs"


# ---------------------------------------------------------------------------
# doc_<id>.md parsing — pure functions
# ---------------------------------------------------------------------------


def _split_sections(md_text: str) -> dict[str, str]:
    """Map each ``## Heading`` to its body text.

    Content before the first ``##`` (the ``# Document NNN`` header and the
    ``- Title:`` / ``- Domain:`` metadata block) lands under the ``""`` key and
    is ignored — that metadata is read from ``index.json`` instead.
    """
    sections: dict[str, str] = {}
    current = ""
    buffer: list[str] = []
    for line in md_text.splitlines():
        heading = _SECTION_RE.match(line)
        if heading:
            sections[current] = "\n".join(buffer).strip()
            current = heading.group(1).strip()
            buffer = []
        else:
            buffer.append(line)
    sections[current] = "\n".join(buffer).strip()
    return sections


def _extract_bullets(block: str) -> list[str]:
    """Pull ``- ``/``* `` bullet lines out of a section body."""
    return [m.group(1).strip() for m in _BULLET_RE.finditer(block) if m.group(1).strip()]


def _looks_like_fetch_error(md_text: str) -> bool:
    """True for a ``doc_<id>_error.md``-style fetch-error stub (``# Fetch Error``)."""
    return md_text.lstrip()[:64].lower().startswith("# fetch error")


def _parse_doc_summary(md_text: str) -> tuple[str, list[str], list[str], list[str]]:
    """Return ``(summary, key_points, reliability_notes, keywords)`` from a doc body.

    ``doc_<id>.md`` is an LLM-generated fixed format, so a section split plus
    bullet extraction is enough. Headings are matched case-insensitively.
    """
    sections = {name.lower(): body for name, body in _split_sections(md_text).items()}
    summary = sections.get("summary", "").strip()
    key_points = _extract_bullets(sections.get("key points", ""))
    reliability_notes = _extract_bullets(sections.get("reliability notes", ""))
    keywords = _extract_bullets(sections.get("keywords", ""))
    return summary, key_points, reliability_notes, keywords


def key_points_from_docs(docs: list[DocRecord]) -> list[KeyPointRecord]:
    """Flatten kept documents' Key Points and Reliability Notes into claim units.

    Pure function so the facade can derive Key Points from already-loaded docs
    without a second disk pass. ``kp_id`` is a stable corpus-wide index.
    """
    key_points: list[KeyPointRecord] = []
    next_id = 0
    for doc in docs:
        for text in doc.key_points:
            key_points.append(
                KeyPointRecord(kp_id=next_id, text=text, doc_id=doc.doc_id, domain=doc.domain, kind="key_point")
            )
            next_id += 1
        for text in doc.reliability_notes:
            key_points.append(
                KeyPointRecord(
                    kp_id=next_id, text=text, doc_id=doc.doc_id, domain=doc.domain, kind="reliability_note"
                )
            )
            next_id += 1
    return key_points


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class ArtifactLoader:
    """Loads ``runs/<workspace>/`` artifacts into the verification domain model.

    Workspace-agnostic and stateless: it is constructed once with the ``runs/``
    root and every method takes a ``workspace`` (a directory name under that
    root, or an absolute path). Caching is the facade's job (§1.4), not the
    loader's.
    """

    def __init__(self, output_root: str | Path = "runs") -> None:
        self._output_root = Path(output_root)

    def _paths(self, workspace: str | Path) -> RunPathManager:
        root = Path(workspace)
        if not root.is_absolute():
            root = self._output_root / workspace
        return RunPathManager(root)

    @staticmethod
    def _read_json(path: Path) -> dict:
        if not path.exists():
            logger.warning("verification: artifact not found: %s", path)
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("verification: failed to read %s: %s", path, exc)
            return {}

    # --- LLM-authored JSON / text artifacts: returned as-is (dict / str) ------

    def load_plan(self, workspace: str | Path) -> dict:
        """``summary/plan.json`` — ``topic``, ``goal``, ``must_cover[]``, ``keywords[]`` …"""
        return self._read_json(self._paths(workspace).plan_path)

    def load_grounding(self, workspace: str | Path) -> dict:
        """``summary/grounding.json`` — ``grounded_terms[]``, ``candidate_entities[]`` …"""
        return self._read_json(self._paths(workspace).grounding_path)

    def load_request(self, workspace: str | Path) -> str:
        """``summary/request.md`` — the original user request, raw text."""
        path = self._paths(workspace).request_path
        if not path.exists():
            logger.warning("verification: request.md not found: %s", path)
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("verification: failed to read %s: %s", path, exc)
            return ""

    # --- Domain models -------------------------------------------------------

    def load_docs(self, workspace: str | Path) -> list[DocRecord]:
        """Build one :class:`DocRecord` per ``index.json`` record.

        ``index.json`` is the authoritative list of kept documents, so
        ``doc_<id>_error.md`` fetch-error stubs are never reached (they have no
        index record). A doc whose summary file is missing or is itself a fetch
        error is still returned — with empty summary fields — so document counts
        stay consistent across the pipelines.
        """
        paths = self._paths(workspace)
        index = self._read_json(paths.index_path)
        records = index.get("records", []) if isinstance(index, dict) else []

        docs: list[DocRecord] = []
        for record in records:
            doc_id = str(record.get("doc_id", "")).strip()
            if not doc_id:
                continue
            duplicate_of = record.get("duplicate_of")
            doc = DocRecord(
                doc_id=doc_id,
                title=record.get("title", ""),
                url=record.get("url", ""),
                final_url=record.get("final_url", ""),
                domain=record.get("domain", ""),
                search_query=record.get("search_query", ""),
                duplicate_of=duplicate_of,
                is_duplicate=duplicate_of is not None,
            )

            summary_path = paths.summary_path_for(int(doc_id)) if doc_id.isdigit() else paths.summary_dir / f"doc_{doc_id}.md"
            if summary_path.exists():
                md_text = summary_path.read_text(encoding="utf-8")
                if _looks_like_fetch_error(md_text):
                    logger.warning("verification: skipping fetch-error summary for doc %s", doc_id)
                else:
                    doc.summary, doc.key_points, doc.reliability_notes, doc.keywords = _parse_doc_summary(md_text)

            clean_md_path = paths.clean_md_dir / f"{doc_id}.md"
            if clean_md_path.exists():
                doc.clean_md_text = clean_md_path.read_text(encoding="utf-8")
            elif not doc.is_duplicate:
                logger.warning("verification: clean_md missing for non-duplicate doc %s", doc_id)

            docs.append(doc)

        docs.sort(key=lambda d: d.doc_id)
        return docs

    def load_key_points(self, workspace: str | Path) -> list[KeyPointRecord]:
        """Key Points + Reliability Notes from every kept doc, as claim units."""
        return key_points_from_docs(self.load_docs(workspace))

    def load_chunks(self, workspace: str | Path) -> list[ChunkRecord]:
        """Read embedded chunks out of ``runs/<workspace>/chromadb/``.

        Chunk vectors already exist (AutoSurvey indexed clean_md into ChromaDB),
        so they are read here rather than re-embedded. Embeddings are
        L2-normalized so downstream cosine similarity is a plain dot product.
        Chunks are returned in a deterministic ``(parent_doc_id, chunk_index)``
        order — ChromaDB's own ``get()`` order is unspecified.
        """
        # Imported lazily so the disk/JSON parsing above stays usable even when
        # the heavyweight ChromaDB dependency cannot be imported.
        from storage.vector_store import VectorStore

        vector_dir = self._paths(workspace).vector_dir
        if not vector_dir.exists():
            logger.warning("verification: chromadb dir not found: %s", vector_dir)
            return []

        store = VectorStore(vector_dir, _CHUNK_COLLECTION)
        try:
            raw = store.collection.get(include=["embeddings", "documents", "metadatas"])
        finally:
            # On Windows an open ChromaDB SQLite handle locks the workspace dir.
            store.close()

        ids = raw.get("ids") or []
        documents = raw.get("documents") or []
        metadatas = raw.get("metadatas") or []
        embeddings = raw.get("embeddings")

        chunks: list[ChunkRecord] = []
        for position, chunk_id in enumerate(ids):
            meta = metadatas[position] if position < len(metadatas) else {}
            meta = meta or {}
            text = documents[position] if position < len(documents) else ""

            embedding = None
            if embeddings is not None and position < len(embeddings):
                vector = np.asarray(embeddings[position], dtype=np.float32)
                if vector.size:
                    embedding = l2_normalize(vector)

            parent_doc_id = str(meta.get("parent_doc_id") or chunk_id.split(":", 1)[0])
            chunks.append(
                ChunkRecord(
                    chunk_id=chunk_id,
                    parent_doc_id=parent_doc_id,
                    chunk_index=int(meta.get("chunk_index", 0) or 0),
                    chunk_count=int(meta.get("chunk_count", 1) or 1),
                    text=text or "",
                    domain=str(meta.get("domain", "")),
                    title=str(meta.get("title", "")),
                    url=str(meta.get("url", "")),
                    search_query=str(meta.get("search_query", "")),
                    embedding=embedding,
                )
            )

        chunks.sort(key=lambda c: (c.parent_doc_id, c.chunk_index))
        return chunks


__all__ = ["ArtifactLoader", "key_points_from_docs"]
