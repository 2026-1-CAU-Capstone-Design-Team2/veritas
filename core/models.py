from dataclasses import dataclass
from typing import Optional


@dataclass
class DocRecord:
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