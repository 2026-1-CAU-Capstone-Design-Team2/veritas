from pathlib import Path


class RunPathManager:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.corpus_dir = self.root / "corpus"
        self.raw_html_dir = self.corpus_dir / "raw_html"
        # Crawl4AI-extracted Markdown for each fetched document — pre-cleanup
        # source. Renamed from ``clean_md`` because that name was historically
        # misleading: Crawl4AI's output still carries nav / footer / share /
        # cookie boilerplate that the downstream layers had to step around.
        self.raw_md_dir = self.root / "raw_md"
        # Post-cleanup Markdown — the document_cleanup tool removes the LLM-
        # identified boilerplate paragraphs from raw_md and writes the result
        # here. This is now the *real* clean source: it feeds batch summary,
        # RAG indexing, verify sentence retrieval, and the doc detail UI.
        self.clean_md_dir = self.root / "clean_md"
        self.summary_dir = self.root / "summary"
        self.vector_dir = self.root / "chromadb"

        # Per-document verified citation-evidence atoms (localized claim +
        # verbatim source quote + offsets). Bounded snippets only — never a raw
        # body. Read by the citation popup to anchor cross-language citations.
        self.citation_evidence_dir = self.summary_dir / "citation_evidence"
        # Per-marker resolution map for the rendered final report (audit +
        # preview confidence): which [doc_NNN] occurrences resolved to a
        # verified evidence atom vs document-level fallback.
        self.final_citations_path = self.summary_dir / "final_citations.json"

        self.index_path = self.summary_dir / "index.json"
        self.request_path = self.summary_dir / "request.md"
        self.grounding_path = self.summary_dir / "grounding.json"
        self.plan_path = self.summary_dir / "plan.json"
        self.query_state_path = self.summary_dir / "query_state.json"
        self.plan_history_path = self.summary_dir / "plan_history.json"
        # Run timing (start / end / elapsed) — persisted so the elapsed time
        # survives completion and an API restart, not just a live frontend run.
        self.timing_path = self.summary_dir / "timing.json"
        self.final_path = self.root / "final.md"

    def prepare_dirs(self) -> None:
        self.raw_html_dir.mkdir(parents=True, exist_ok=True)
        self.raw_md_dir.mkdir(parents=True, exist_ok=True)
        self.clean_md_dir.mkdir(parents=True, exist_ok=True)
        self.summary_dir.mkdir(parents=True, exist_ok=True)
        self.vector_dir.mkdir(parents=True, exist_ok=True)

    def summary_path_for(self, index: int):
        return self.summary_dir / f"doc_{index:03d}.md"

    def citation_evidence_path(self, doc_id: str):
        return self.citation_evidence_dir / f"{doc_id}.json"

    def batch_path(self, batch_index: int):
        return self.summary_dir / f"batch_{batch_index:03d}.md"

    def fetch_error_path(self, error_id: str):
        # Fetch-error notes live in their own ``fetch_error_*`` namespace so they
        # never share a number with a kept document's ``doc_*.md`` summary.
        return self.summary_dir / f"fetch_error_{error_id}.md"