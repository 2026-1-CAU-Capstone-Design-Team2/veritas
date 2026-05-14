from pathlib import Path


class RunPathManager:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.corpus_dir = self.root / "corpus"
        self.raw_html_dir = self.corpus_dir / "raw_html"
        # Crawl4AI-extracted clean Markdown for each fetched document. This is
        # the RAG answer source and the input to both per-doc and batch summary.
        self.clean_md_dir = self.root / "clean_md"
        self.summary_dir = self.root / "summary"
        self.vector_dir = self.root / "chromadb"

        self.index_path = self.summary_dir / "index.json"
        self.request_path = self.summary_dir / "request.md"
        self.grounding_path = self.summary_dir / "grounding.json"
        self.plan_path = self.summary_dir / "plan.json"
        self.query_state_path = self.summary_dir / "query_state.json"
        self.plan_history_path = self.summary_dir / "plan_history.json"
        self.final_path = self.root / "final.md"

    def prepare_dirs(self) -> None:
        self.raw_html_dir.mkdir(parents=True, exist_ok=True)
        self.clean_md_dir.mkdir(parents=True, exist_ok=True)
        self.summary_dir.mkdir(parents=True, exist_ok=True)
        self.vector_dir.mkdir(parents=True, exist_ok=True)

    def summary_path_for(self, index: int):
        return self.summary_dir / f"doc_{index:03d}.md"

    def batch_path(self, batch_index: int):
        return self.summary_dir / f"batch_{batch_index:03d}.md"

    def fetch_error_path(self, error_id: str):
        # Fetch-error notes live in their own ``fetch_error_*`` namespace so they
        # never share a number with a kept document's ``doc_*.md`` summary.
        return self.summary_dir / f"fetch_error_{error_id}.md"