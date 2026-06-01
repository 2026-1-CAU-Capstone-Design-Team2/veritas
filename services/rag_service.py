from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from core.memory.request import CallConstraints, CallRequest
from core.prompts import (
    QUERY_REWRITE_PROMPT,
    QUERY_REWRITE_SYSTEM_PROMPT,
    RAG_DRAFT_CONTEXT_TEMPLATE,
    RAG_EMPTY_CONTEXT_PROMPT_TEMPLATE,
    RAG_SYSTEM_PROMPT,
    RAG_USER_PROMPT_TEMPLATE,
)

if TYPE_CHECKING:
    from storage.vector_store import VectorStore


class RAGService:
    """Service-owned RAG implementation.

    This class owns indexing, retrieval, query rewriting, document formatting,
    and document-grounded answer generation. LLM-facing tool schemas should wrap
    this service instead of owning RAG state themselves.
    """

    def __init__(
        self,
        llm,
        vector_store: VectorStore,
        *,
        n_results: int = 5,
        max_context_chars: int = 12000,
        max_embed_chars: int = 900,
        chunk_overlap_chars: int = 120,
        max_history_turns: int = 3,
    ) -> None:
        self.llm = llm
        self.vector_store = vector_store
        self.n_results = n_results
        self.max_context_chars = max_context_chars
        self.max_embed_chars = max_embed_chars
        self.chunk_overlap_chars = chunk_overlap_chars
        self.max_history_turns = max_history_turns

    def clear_index(self) -> None:
        self.vector_store.clear()

    def get_document_count(self) -> int:
        return self.vector_store.get_document_count()

    def close(self) -> None:
        """Release the underlying ChromaDB vector store handles.

        Called when a workspace is switched away from or deleted so its
        SQLite file handle does not keep the workspace directory locked.
        """
        vector_store = getattr(self, "vector_store", None)
        if vector_store is not None:
            try:
                vector_store.close()
            except Exception:
                pass

    def _supports_memory_calls(self) -> bool:
        """True when the LLM client exposes the memory-engaged call API."""
        return callable(getattr(self.llm, "call", None)) and callable(
            getattr(self.llm, "iter_call", None)
        )

    def _memory_runtime(self):
        """Return the bound MemoryRuntime, or None when memory is unavailable."""
        return getattr(self.llm, "memory_runtime", None)

    def _recent_history_text(self) -> str:
        """Flat recent-turn block from FIFO memory, used by query rewriting and
        the empty-context grounded prompt. Thin wrapper over
        :meth:`MemoryRuntime.recent_history_text` so this surface and ChatAgent
        share a single definition of "recent history".
        """
        runtime = self._memory_runtime()
        if runtime is None:
            return "(No previous conversation)"
        return runtime.recent_history_text(turns=self.max_history_turns)

    def _normalize_for_embedding(self, text: str) -> str:
        text = text.replace("\r\n", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _chunk_text(self, text: str) -> list[str]:
        text = self._normalize_for_embedding(text)
        if not text:
            return []
        if len(text) <= self.max_embed_chars:
            return [text]

        chunks: list[str] = []
        start = 0
        text_len = len(text)

        while start < text_len:
            end = min(start + self.max_embed_chars, text_len)
            if end < text_len:
                split_at = max(
                    text.rfind("\n\n", start, end),
                    text.rfind("\n", start, end),
                    text.rfind(". ", start, end),
                    text.rfind(" ", start, end),
                )
                if split_at > start + (self.max_embed_chars // 3):
                    end = split_at + 1

            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)

            if end >= text_len:
                break
            start = max(end - self.chunk_overlap_chars, start + 1)

        return chunks

    def _append_chunked_document(
        self,
        *,
        base_doc_id: str,
        content: str,
        metadata: dict[str, Any],
        doc_ids: list[str],
        contents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        chunks = self._chunk_text(content)
        for chunk_index, chunk in enumerate(chunks):
            chunk_doc_id = base_doc_id if len(chunks) == 1 else f"{base_doc_id}:chunk_{chunk_index:03d}"
            chunk_metadata = dict(metadata)
            chunk_metadata.update(
                {
                    "parent_doc_id": base_doc_id,
                    "chunk_index": chunk_index,
                    "chunk_count": len(chunks),
                }
            )
            doc_ids.append(chunk_doc_id)
            contents.append(chunk)
            metadatas.append(chunk_metadata)

    def index_autosurvey_output(self, clean_md_dir: Path, index_path: Path | None = None, clear_first: bool = True) -> int:
        """Index a workspace's clean Markdown documents for RAG.

        The RAG answer source is each document's Crawl4AI clean Markdown
        (``clean_md/<doc_id>.md``), not the lossy per-document summaries — so a
        retrieved chunk carries the fuller original content. Per-doc summaries
        stay a separate UX layer and are not indexed here.
        """
        if clear_first:
            self.clear_index()

        metadata_map: dict[str, dict[str, str]] = {}
        if index_path and index_path.exists():
            try:
                index_data = json.loads(index_path.read_text(encoding="utf-8"))
                for record in index_data.get("records", []):
                    metadata_map[record["doc_id"]] = {
                        "title": record.get("title", ""),
                        "url": record.get("url", ""),
                        "domain": record.get("domain", ""),
                        "search_query": record.get("search_query", ""),
                    }
            except Exception as e:
                print(f"[rag] Warning: Could not load index.json: {e}")

        clean_md_files = sorted(clean_md_dir.glob("*.md")) if clean_md_dir else []
        if not clean_md_files:
            print("[rag] No clean_md files found to index")
            return 0

        doc_ids: list[str] = []
        contents: list[str] = []
        metadatas: list[dict[str, Any]] = []

        for clean_md_file in clean_md_files:
            # clean_md/<doc_id>.md — the stem is the doc_id. Duplicate documents
            # never get a clean_md file, so there is nothing to skip here.
            doc_id = clean_md_file.stem
            content = clean_md_file.read_text(encoding="utf-8")
            if not content.strip():
                continue

            self._append_chunked_document(
                base_doc_id=doc_id,
                content=content,
                metadata=metadata_map.get(doc_id, {}),
                doc_ids=doc_ids,
                contents=contents,
                metadatas=metadatas,
            )

        if not doc_ids:
            print("[rag] No valid documents to index")
            return 0

        print(f"[rag] Generating embeddings for {len(doc_ids)} chunks...")
        embeddings = self.llm.embed_batch(contents)
        self.vector_store.add_documents(
            doc_ids=doc_ids,
            contents=contents,
            embeddings=embeddings,
            metadatas=metadatas,
        )

        print(f"[rag] Indexed {len(doc_ids)} chunks")
        return len(doc_ids)

    def index_all_markdown(self, base_dir: Path, clear_first: bool = True) -> int:
        if clear_first:
            self.clear_index()

        doc_ids: list[str] = []
        contents: list[str] = []
        metadatas: list[dict[str, Any]] = []

        md_files = sorted(
            p for p in base_dir.rglob("*.md")
            if p.is_file()
            and "chromadb" not in p.parts
            and not any(part.startswith(".") for part in p.parts)
        )

        if not md_files:
            print("[rag] No markdown files found to index")
            return 0

        print(f"[rag] Found {len(md_files)} markdown files under {base_dir}")

        for md_file in md_files:
            if "_error" in md_file.stem:
                continue

            content = md_file.read_text(encoding="utf-8")
            if "Duplicate of:" in content or not content.strip():
                continue

            rel_path = md_file.relative_to(base_dir)
            safe_parts = [part.replace(":", "_") for part in rel_path.with_suffix("").parts]
            base_doc_id = "/".join(safe_parts)
            file_path = str(rel_path)
            parent_folder = str(rel_path.parent) if str(rel_path.parent) != "." else "root"

            self._append_chunked_document(
                base_doc_id=base_doc_id,
                content=content,
                metadata={
                    "type": "markdown",
                    "source_folder": parent_folder,
                    "file_path": file_path,
                    "file_name": md_file.name,
                },
                doc_ids=doc_ids,
                contents=contents,
                metadatas=metadatas,
            )

        if not doc_ids:
            print("[rag] No valid markdown files found to index")
            return 0

        print(f"[rag] Generating embeddings for {len(doc_ids)} chunks...")
        embeddings = self.llm.embed_batch(contents)
        self.vector_store.add_documents(
            doc_ids=doc_ids,
            contents=contents,
            embeddings=embeddings,
            metadatas=metadatas,
        )

        print(f"[rag] Indexed {len(doc_ids)} chunks")
        return len(doc_ids)

    def _rewrite_query_with_context(self, question: str) -> str:
        history = self._recent_history_text()
        # No prior turns recorded yet -> nothing to resolve, search the raw question.
        if history == "(No previous conversation)":
            return question

        rewrite_prompt = QUERY_REWRITE_PROMPT.format(history=history, question=question)
        # Query rewriting is an internal, transient call: it must not be recorded
        # as a conversation turn and needs no memory injection of its own.
        rewritten = self.llm.ask(
            QUERY_REWRITE_SYSTEM_PROMPT,
            rewrite_prompt,
            reasoning=False,
            sampling_params={"temperature": 0.0, "top_p": 0.2, "presence_penalty": 0.0},
            extra_sampling_params={"top_k": 5, "min_p": 0.0, "repeat_penalty": 1.0},
        ).strip()

        print(f"[rag] Rewritten query: {rewritten}")
        return rewritten if rewritten else question

    def retrieve(self, query: str, use_history: bool = True) -> list[dict[str, Any]]:
        search_query = self._rewrite_query_with_context(query) if use_history else query
        query_embedding = self.llm.embed(search_query)
        return self.vector_store.query(
            query_text=search_query,
            query_embedding=query_embedding,
            n_results=self.n_results,
        )

    def _strip_weak_evidence_sections(self, content: str) -> str:
        """Remove metadata-like sections that are useful for retrieval but weak as answer evidence."""
        text = str(content or "").replace("\r\n", "\n")
        lines = text.splitlines()
        kept: list[str] = []
        skip_section = False

        weak_headings = {
            "keywords",
            "reliability notes",
            "source notes",
        }

        for line in lines:
            stripped = line.strip()
            lower = stripped.lower()

            if lower.startswith("## "):
                heading = lower[3:].strip().strip(":")
                skip_section = heading in weak_headings
                if skip_section:
                    continue
            elif lower.startswith("# "):
                skip_section = False

            if skip_section:
                continue

            if lower.startswith("- search query:"):
                continue

            kept.append(line)

        sanitized = "\n".join(kept)
        sanitized = re.sub(r"\n{3,}", "\n\n", sanitized).strip()
        return sanitized

    def _has_substantive_context(self, content: str) -> bool:
        text = str(content or "").strip()
        if not text:
            return False
        # Headers and separators alone are not substantive evidence.
        evidence_lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                continue
            if re.fullmatch(r"[-_=*]{3,}", stripped):
                continue
            if stripped.startswith("#"):
                continue
            evidence_lines.append(stripped)
        return len(" ".join(evidence_lines)) >= 80

    def format_retrieved_documents(self, documents: list[Any]) -> str:
        parts: list[str] = []
        total_chars = 0

        for item in documents:
            if not isinstance(item, dict):
                continue

            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            label = metadata.get("parent_doc_id", item.get("doc_id", "unknown"))
            file_path = metadata.get("file_path", "")
            header = f"[Document {label}]"
            if file_path:
                header += f" ({file_path})"

            raw_content = str(item.get("content") or "").strip()
            content = self._strip_weak_evidence_sections(raw_content)
            if not self._has_substantive_context(content):
                continue

            doc_text = f"{header}\n{content}"
            if total_chars + len(doc_text) > self.max_context_chars:
                break

            parts.append(doc_text)
            total_chars += len(doc_text)

        return "\n\n---\n\n".join(parts)

    def _grounded_user_prompt(
        self, question: str, *, use_history: bool, doc_context: str = ""
    ) -> str:
        """Build the strict-RAG user prompt for a question.

        Retrieves workspace documents and formats them as the answer context.
        When nothing *substantive* is retrieved — including when the only hits
        are off-topic enough that ``format_retrieved_documents`` strips them —
        returns the empty-context prompt so ``RAG_SYSTEM_PROMPT`` makes the
        model say it lacks the information rather than answering from general
        knowledge. ``doc_context`` (the editor's open draft) rides along as
        clearly-subordinate context, never as factual evidence.

        Note: relevance is enforced by the LLM reading the retrieved context
        against the question, not by an embedding-distance gate — this
        multilingual embedder's cosine floor is too high to separate on- from
        off-topic by distance (measured: on-topic and cross-topic best
        distances overlap), so a numeric threshold would drop on-topic hits.
        """
        retrieved = self.retrieve(question, use_history=use_history)
        # The grounded answer call goes through MemoryAwareLLMClient.call/iter_call
        # (see _rag_call_request), so recent turns ride in as separate chat
        # messages — the prompt body no longer carries a {history} slot.
        context = self.format_retrieved_documents(retrieved) if retrieved else ""
        draft_block = (
            RAG_DRAFT_CONTEXT_TEMPLATE.format(draft=doc_context.strip())
            if doc_context and doc_context.strip()
            else ""
        )
        if not context.strip():
            return (
                RAG_EMPTY_CONTEXT_PROMPT_TEMPLATE.format(question=question)
                + draft_block
            )
        return (
            RAG_USER_PROMPT_TEMPLATE.format(
                context=context, question=question
            )
            + draft_block
        )

    def _rag_call_request(self, *, user_prompt: str, question: str) -> CallRequest:
        """Build the memory-engaged request for a grounded RAG answer.

        - profile="rag": the ProfilePolicyDispatcher allows a small recall pull
          (recall_limit=2), so working/recall ride along as secondary context
          while documents stay the primary evidence.
        - grounded=False so working context is allowed; the RAG_SYSTEM_PROMPT in
          task_instruction keeps "answer only from the documents" in force.
        - record_content=question so the FIFO/recall turn stores the clean user
          question, not the full doc-stuffed prompt.
        - enable_memory_tools=False keeps iter_call on the streaming path.
        """
        return CallRequest(
            task_instruction=RAG_SYSTEM_PROMPT,
            user_content=user_prompt,
            record_content=question,
            constraints=CallConstraints(),
            use_history=True,
            profile="rag",
            stream_label="rag",
            method_hint="rag",
            enable_memory_tools=False,
        )

    def answer(
        self,
        question: str,
        stream: bool = False,
        use_history: bool = True,
        *,
        doc_context: str = "",
    ) -> str:
        user_prompt = self._grounded_user_prompt(
            question, use_history=use_history, doc_context=doc_context
        )
        if self._supports_memory_calls():
            return self.llm.call(
                self._rag_call_request(user_prompt=user_prompt, question=question)
            )
        return self.llm.ask(
            RAG_SYSTEM_PROMPT,
            user_prompt,
            reasoning=False,
            stream=stream,
            stream_label="rag",
        )

    def iter_answer(
        self, question: str, *, use_history: bool = True, doc_context: str = ""
    ) -> Iterator[str]:
        """Streaming counterpart of :meth:`answer` — yields chunks for SSE.

        Uses the same strict grounding as :meth:`answer` (``RAG_SYSTEM_PROMPT``
        plus retrieved workspace context, or the empty-context refusal prompt
        when nothing relevant is found), so a streamed chat answer is grounded
        exactly like the one-shot path instead of falling back to the permissive
        tool-synthesis prompt.
        """
        user_prompt = self._grounded_user_prompt(
            question, use_history=use_history, doc_context=doc_context
        )
        if self._supports_memory_calls():
            yield from self.llm.iter_call(
                self._rag_call_request(user_prompt=user_prompt, question=question)
            )
            return
        yield from self.llm.iter_ask(
            RAG_SYSTEM_PROMPT,
            user_prompt,
            reasoning=False,
            stream_label="rag",
        )


__all__ = ["RAGService"]
