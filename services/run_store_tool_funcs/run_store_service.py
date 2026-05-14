import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from core.models import DocRecord
from services.run_store_tool_funcs.path_manager import RunPathManager
from services.run_store_tool_funcs.record_serializer import RecordSerializer


class RunStoreService:
    def __init__(self, root: str | Path):
        self.paths = RunPathManager(root)
        self.serializer = RecordSerializer()
        self.paths.prepare_dirs()
        self.batch_counter = 0

    @property
    def final_path(self):
        return self.paths.final_path

    @property
    def plan_path(self):
        return self.paths.plan_path

    @property
    def request_path(self):
        return self.paths.request_path

    @property
    def grounding_path(self):
        return self.paths.grounding_path

    @property
    def vector_dir(self):
        return self.paths.vector_dir

    @property
    def summary_dir(self):
        return self.paths.summary_dir

    @property
    def clean_md_dir(self):
        return self.paths.clean_md_dir

    @property
    def index_path(self):
        return self.paths.index_path

    @property
    def query_state_path(self):
        return self.paths.query_state_path

    @property
    def plan_history_path(self):
        return self.paths.plan_history_path

    def save_text(self, path: str | Path, content: str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.sanitize_text(content), encoding="utf-8", errors="replace")

    def save_json(self, path: str | Path, payload: dict) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_request(self, user_request: str) -> None:
        self.save_text(self.paths.request_path, user_request.strip() + "\n")

    def load_request(self) -> str:
        if not self.paths.request_path.exists():
            raise FileNotFoundError(f"Missing request file: {self.paths.request_path}")
        return self.read_text_file(str(self.paths.request_path)).strip()

    def save_grounding(self, payload: dict[str, Any]) -> None:
        self.save_json(self.paths.grounding_path, payload)

    def load_grounding(self) -> dict[str, Any]:
        if not self.paths.grounding_path.exists():
            raise FileNotFoundError(f"Missing grounding file: {self.paths.grounding_path}")
        return json.loads(self.read_text_file(str(self.paths.grounding_path)))

    def grounding_exists(self) -> bool:
        return self.paths.grounding_path.exists()

    def save_plan(self, payload: dict) -> None:
        self.save_json(self.paths.plan_path, payload)

    def load_plan(self) -> dict:
        if not self.paths.plan_path.exists():
            raise FileNotFoundError(f"Missing plan file: {self.paths.plan_path}")
        return json.loads(self.read_text_file(str(self.paths.plan_path)))

    def plan_exists(self) -> bool:
        return self.paths.plan_path.exists()

    def save_query_state(self, payload: dict[str, Any]) -> None:
        used_queries = self._normalize_query_list(payload.get("used_queries", []))
        merged = {
            **payload,
            "used_queries": used_queries,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.save_json(self.paths.query_state_path, merged)

    def load_query_state(self) -> dict[str, Any]:
        if not self.paths.query_state_path.exists():
            return {"used_queries": [], "cycles_executed": 0}

        try:
            payload = json.loads(self.read_text_file(str(self.paths.query_state_path)))
        except Exception:
            return {"used_queries": [], "cycles_executed": 0}

        used_queries = self._normalize_query_list(payload.get("used_queries", []))
        return {
            **payload,
            "used_queries": used_queries,
            "cycles_executed": int(payload.get("cycles_executed", 0) or 0),
        }

    def reset_query_state(self) -> None:
        self.save_query_state({"used_queries": [], "cycles_executed": 0})

    def load_plan_history(self) -> list[dict[str, Any]]:
        if not self.paths.plan_history_path.exists():
            return []
        try:
            payload = json.loads(self.read_text_file(str(self.paths.plan_history_path)))
            if isinstance(payload, dict):
                entries = payload.get("entries", [])
            else:
                entries = payload
            if not isinstance(entries, list):
                return []
            return [entry for entry in entries if isinstance(entry, dict)]
        except Exception:
            return []

    def append_plan_history(
        self,
        *,
        reason: str,
        plan: dict[str, Any],
        previous_plan: dict[str, Any] | None = None,
        gap_directions: list[str] | None = None,
    ) -> None:
        previous_plan = previous_plan or {}
        gap_directions = [str(item).strip() for item in (gap_directions or []) if str(item).strip()]

        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "changed": previous_plan != plan,
            "gap_directions": gap_directions,
            "previous_plan": previous_plan,
            "plan": plan,
            "previous_query_count": len(previous_plan.get("search_queries", [])),
            "query_count": len(plan.get("search_queries", [])),
        }

        history = self.load_plan_history()
        history.append(entry)
        self.save_json(self.paths.plan_history_path, {"entries": history})

    def load_records(self) -> list[DocRecord]:
        if not self.paths.index_path.exists():
            return []

        try:
            payload = json.loads(self.read_text_file(str(self.paths.index_path)))
            records = self.serializer.deserialize_records(payload.get("records", []))
        except Exception:
            return []

        existing_batches = sorted(self.paths.summary_dir.glob("batch_*.md"))
        if existing_batches:
            try:
                self.batch_counter = max(int(p.stem.split("_")[1]) for p in existing_batches)
            except Exception:
                self.batch_counter = 0

        return records

    def save_records(self, records: list[DocRecord]) -> None:
        self.save_json(
            self.paths.index_path,
            {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "records": self.serializer.serialize_records(records),
            },
        )

    def load_text_cache(self) -> list[str]:
        texts: list[str] = []
        for record in self.load_records():
            if record.duplicate_of is not None or not record.text_path:
                continue
            path = Path(record.text_path)
            if path.exists() and path.stat().st_size > 0:
                try:
                    texts.append(self.read_text_file(str(path)))
                except Exception:
                    pass
        return texts

    def next_doc_id(self, record_count: int) -> str:
        return f"{record_count:03d}"

    def is_zero_byte_file(self, path_str: str) -> bool:
        if not path_str:
            return True
        path = Path(path_str)
        if not path.exists():
            return True
        return path.stat().st_size == 0

    def is_invalid_document_record(self, record: DocRecord) -> bool:
        return self.is_zero_byte_file(record.text_path) or self.is_zero_byte_file(record.html_path)

    def list_non_duplicate_records(self) -> list[DocRecord]:
        return [r for r in self.load_records() if r.duplicate_of is None]

    def list_duplicate_records(self) -> list[DocRecord]:
        return [r for r in self.load_records() if r.duplicate_of is not None]

    def sanitize_text(self, content: Any) -> str:
        text = str(content or "")
        text = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        text = text.replace("\x00", "")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text

    def read_text_file(self, path_str: str) -> str:
        path = Path(path_str)
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except UnicodeError:
            raw = path.read_bytes()
            for encoding in ("utf-8-sig", "cp949", "euc-kr", "latin-1"):
                try:
                    return raw.decode(encoding, errors="replace")
                except Exception:
                    continue
            return raw.decode("utf-8", errors="replace")

    def get_batch_summary_path(self, batch_index: int):
        return self.paths.batch_path(batch_index)

    def write_document_summary(self, record: DocRecord, content: str) -> None:
        self.save_text(record.summary_path, content)

    def write_batch_summary(self, batch_index: int, content: str) -> None:
        self.save_text(self.paths.batch_path(batch_index), content)

    def clear_batch_summaries(self) -> None:
        for path in self.paths.summary_dir.glob("batch_*.md"):
            try:
                path.unlink()
            except Exception:
                pass
        self.batch_counter = 0

    def load_all_batch_summaries(self) -> list[str]:
        return [
            p.read_text(encoding="utf-8")
            for p in sorted(self.paths.summary_dir.glob("batch_*.md"))
        ]

    def save_final_report(self, content: str) -> None:
        self.save_text(self.paths.final_path, content)

    def set_batch_counter_from_count(self, count: int) -> None:
        self.batch_counter = count

    def normalize_text(self, text: str) -> str:
        text = text.lower()
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def normalize_title(self, title: str) -> str:
        text = str(title or "").lower()
        text = re.sub(r"\[[^\]]+\]", " ", text)
        text = re.sub(r"\([^)]*\)", " ", text)
        text = re.sub(r"\s+[-–—|:]\s+.*$", "", text)
        text = re.sub(r"[^a-z0-9가-힣]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def canonicalize_url(self, url: str) -> str:
        text = str(url or "").strip()
        if not text:
            return ""

        try:
            parsed = urlparse(text)
        except Exception:
            return text

        scheme = (parsed.scheme or "https").lower()
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]

        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        path = path.rstrip("/") or "/"

        arxiv_id = self.extract_arxiv_id(text)
        if arxiv_id:
            return f"https://arxiv.org/html/{arxiv_id}"

        ignored_query_keys = {
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "fbclid",
            "gclid",
        }
        query_pairs = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=False)
            if key.lower() not in ignored_query_keys
        ]
        query = urlencode(sorted(query_pairs))

        return urlunparse((scheme, host, path, "", query, ""))

    def extract_arxiv_id(self, text: str) -> str:
        value = str(text or "")
        match = re.search(
            r"arxiv\.org/(?:abs|html|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?",
            value,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1)

        match = re.search(r"\barxiv\s*:\s*([0-9]{4}\.[0-9]{4,5})(?:v\d+)?\b", value, flags=re.IGNORECASE)
        if match:
            return match.group(1)

        return ""

    def jaccard_similarity(self, a: str, b: str) -> float:
        sa = set(self.normalize_text(a).split())
        sb = set(self.normalize_text(b).split())
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    def find_duplicate(
        self,
        fetched_text: str,
        threshold: float = 0.82,
        *,
        url: str = "",
        final_url: str = "",
        title: str = "",
    ) -> tuple[bool, float, str | None]:
        kept_records = [r for r in self.load_records() if r.duplicate_of is None]

        fetched_urls = {
            self.canonicalize_url(url),
            self.canonicalize_url(final_url),
        }
        fetched_urls.discard("")

        fetched_arxiv_id = (
            self.extract_arxiv_id(url)
            or self.extract_arxiv_id(final_url)
            or self.extract_arxiv_id(fetched_text[:4000])
        )
        fetched_title = self.normalize_title(title)

        best = 0.0
        matched_doc_id: str | None = None

        for record in kept_records:
            record_urls = {
                self.canonicalize_url(record.url),
                self.canonicalize_url(record.final_url),
            }
            record_urls.discard("")

            if fetched_urls and record_urls and fetched_urls & record_urls:
                return True, 1.0, record.doc_id

            record_arxiv_id = (
                self.extract_arxiv_id(record.url)
                or self.extract_arxiv_id(record.final_url)
            )
            if fetched_arxiv_id and record_arxiv_id and fetched_arxiv_id == record_arxiv_id:
                return True, 1.0, record.doc_id

            record_title = self.normalize_title(record.title)
            if fetched_title and record_title:
                title_score = self.jaccard_similarity(fetched_title, record_title)
                if title_score > best:
                    best = title_score
                if title_score >= 0.92 and min(len(fetched_title), len(record_title)) >= 24:
                    matched_doc_id = record.doc_id
                    break

            old_text = ""
            if record.text_path:
                path = Path(record.text_path)
                if path.exists() and path.stat().st_size > 0:
                    try:
                        old_text = self.read_text_file(str(path))
                    except Exception:
                        old_text = ""

            if not old_text:
                continue

            score = self.jaccard_similarity(fetched_text[:6000], old_text[:6000])
            if score > best:
                best = score
            if score >= threshold:
                matched_doc_id = record.doc_id
                break

        if matched_doc_id is None:
            return False, best, None

        return True, best, matched_doc_id

    def write_fetch_error_note(self, doc_id: str, url: str, error: str) -> None:
        self.save_text(
            self.paths.fetch_error_path(doc_id),
            f"# Fetch Error\n\nURL: {url}\n\nError: {error}\n",
        )

    def write_duplicate_record(
        self,
        *,
        doc_id: str,
        title: str,
        url: str,
        final_url: str,
        domain: str,
        search_query: str,
        duplicate_of: str,
        duplicate_score: float,
    ) -> None:
        records = self.load_records()
        index = len(records)
        summary_path = self.paths.summary_path_for(index)

        duplicate_note = (
            f"# Document {doc_id}\n\n"
            f"- URL: {url}\n"
            f"- Final URL: {final_url}\n"
            f"- Duplicate of: {duplicate_of}\n"
            f"- Similarity: {duplicate_score:.3f}\n"
        )
        self.save_text(summary_path, duplicate_note)

        record = DocRecord(
            doc_id=doc_id,
            title=title,
            url=url,
            final_url=final_url,
            domain=domain,
            search_query=search_query,
            text_path="",
            html_path="",
            summary_path=str(summary_path.resolve()),
            duplicate_of=duplicate_of,
            duplicate_score=duplicate_score,
        )
        records.append(record)
        self.save_records(records)

    def write_fetched_record(
        self,
        *,
        doc_id: str,
        title: str,
        url: str,
        final_url: str,
        domain: str,
        search_query: str,
        html: str,
        text: str,
        content_type: str = "",
    ) -> None:
        records = self.load_records()
        html_path = self.paths.raw_html_dir / f"{doc_id}.html"
        # Crawl4AI clean Markdown — stored as .md so every text artifact in the
        # workspace is Markdown (raw HTML is the only non-.md document file).
        text_path = self.paths.clean_md_dir / f"{doc_id}.md"
        summary_path = self.paths.summary_path_for(len(records))

        self.save_text(html_path, html)
        self.save_text(text_path, text)

        record = DocRecord(
            doc_id=doc_id,
            title=title,
            url=url,
            final_url=final_url,
            domain=domain,
            search_query=search_query,
            text_path=str(text_path.resolve()),
            html_path=str(html_path.resolve()),
            summary_path=str(summary_path.resolve()),
        )
        records.append(record)
        self.save_records(records)

    def _normalize_query_list(self, payload: Any) -> list[str]:
        if not isinstance(payload, list):
            return []

        out: list[str] = []
        seen: set[str] = set()

        for item in payload:
            text = str(item).strip()
            if not text:
                continue
            key = " ".join(text.lower().split())
            if key in seen:
                continue
            seen.add(key)
            out.append(text)

        return out
