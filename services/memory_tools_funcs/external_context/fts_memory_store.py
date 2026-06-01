"""SQLite FTS5 storage backing the recall tier."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from core.memory.models import MemoryItem
from services.memory_tools_funcs.store import MemoryStore
from services.memory_tools_funcs.token_counter import TokenCounter


class FtsMemoryStore:
    """SQLite-only FTS5 store with one-time legacy migration."""

    def __init__(
        self,
        *,
        store: MemoryStore,
        legacy_path: Path,
        legacy_db_path: Path,
        table_name: str,
        fts_name: str,
        default_tier: str,
        migration_key: str,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.store = store
        self.db_path = store.db_path
        self.legacy_path = Path(legacy_path)
        self.legacy_db_path = Path(legacy_db_path)
        self.table_name = self._validate_identifier(table_name)
        self.fts_name = self._validate_identifier(fts_name)
        self.default_tier = str(default_tier or "")
        self.migration_key = str(migration_key or f"{self.default_tier}_migrated")
        self.token_counter = token_counter or TokenCounter()
        self._legacy_migrated = False

    def add(self, item: MemoryItem) -> None:
        """Add one memory item to SQLite."""
        self._ensure_migrated()
        self._append_sqlite(self.store.item_to_dict(item))

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return latest rows in chronological order."""
        limit = int(limit)
        if limit <= 0 or not self._has_storage():
            return []
        self._ensure_migrated()
        return self._tail_sqlite(limit=limit)

    # Reranker over-fetch ratio. BM25 on a trigram index returns a usable
    # ordering but it's biased by document length and rare-ngram bursts, so
    # we pull ~3× the caller's limit as candidates and re-sort by lexical
    # overlap with the original query (intent match) plus a light recency
    # boost (newer turns matter more for "what did we discuss"). Capped
    # absolute so a tiny limit doesn't degenerate to 1-2 candidates.
    _RERANK_CANDIDATE_FACTOR = 3
    _RERANK_MIN_CANDIDATES = 10
    # Weight of recency in the rerank score, applied as a multiplicative
    # bump in (1 - _RERANK_RECENCY_WEIGHT, 1.0]. Small: order overwhelmingly
    # follows overlap, recency only breaks ties.
    _RERANK_RECENCY_WEIGHT = 0.15

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Search rows by FTS5 and re-rank by lexical overlap + recency.

        BM25 alone is noisy on the trigram index for short / generic Korean
        queries — overlap with the original question's content tokens is a
        stronger precision signal. We over-fetch BM25 candidates, optionally
        pad with a SQL ``LIKE`` fallback when the trigram index alone misses
        short / compound-prefix keywords (e.g. "주가", "삼성" matching
        "목표주가" / "삼성전자"), and reorder the union by overlap. Callers
        see the same shape (a sliced top-``limit`` list).
        """
        query = str(query or "").strip()
        if not query or not self._has_storage():
            return []
        self._ensure_migrated()
        bm25_limit = max(int(limit) * self._RERANK_CANDIDATE_FACTOR, self._RERANK_MIN_CANDIDATES)
        candidates = self._search_sqlite(query, limit=bm25_limit)
        # Trigram cannot index ngrams shorter than 3 chars, and phrase matching
        # on a single token never aligns with a longer compound noun
        # ("삼성" vs "삼성전자"). When the FTS pool is thin OR the query
        # carries trigram-uncoverable tokens, fall back to substring LIKE
        # on the base table — small per-workspace row counts make the scan
        # cost negligible.
        needs_like = (
            len(candidates) < bm25_limit
            and bool(self._like_terms(query))
        )
        if needs_like:
            seen_ids = {str(r.get("id") or "") for r in candidates}
            extras = self._like_fallback(query, limit=bm25_limit - len(candidates))
            for row in extras:
                row_id = str(row.get("id") or "")
                if row_id and row_id not in seen_ids:
                    candidates.append(row)
                    seen_ids.add(row_id)
        if not candidates:
            return []
        return self._rerank(query, candidates)[: max(0, int(limit))]

    # LIKE-fallback dial: minimum length for a token to be worth a substring
    # scan. 2 captures Korean 2-char nouns ("주가", "전망", "배당") that the
    # trigram tokenizer can't index, and prefix slices of compound nouns
    # ("삼성" → "삼성전자"). Length-1 tokens are too noisy.
    _MIN_LIKE_TERM_LEN = 2
    _MAX_LIKE_TERMS = 4

    @classmethod
    def _like_terms(cls, query: str) -> list[str]:
        """Pick LIKE-search terms — same content tokens as ``_fts_query``
        but with a relaxed length floor (2 chars instead of 3) so short
        Korean nouns and compound-noun prefixes are recoverable. Returns
        an ordered, length-desc, deduped list.
        """
        raw = re.findall(r"[\w가-힣]+", str(query or "").lower())
        seen: set[str] = set()
        out: list[str] = []
        for tok in raw:
            stem = cls._strip_particle(tok)
            if len(stem) < cls._MIN_LIKE_TERM_LEN:
                continue
            if stem in cls._KO_STOPWORDS:
                continue
            if stem in seen:
                continue
            seen.add(stem)
            out.append(stem)
        out.sort(key=len, reverse=True)
        return out[: cls._MAX_LIKE_TERMS]

    def _like_fallback(self, query: str, *, limit: int) -> list[dict[str, Any]]:
        """Substring-scan ``self.table_name`` for rows whose content contains
        any of the query's LIKE-eligible terms. Cheap because per-workspace
        recall row counts stay in the low hundreds even after long
        sessions, and the rows are local SQLite.

        Returns rows in the base table's "newest first" order so the rerank
        layer that follows still has a meaningful recency signal."""
        like_terms = self._like_terms(query)
        if not like_terms or limit <= 0:
            return []
        clauses = " OR ".join("content LIKE ?" for _ in like_terms)
        params = tuple(f"%{t}%" for t in like_terms) + (int(limit),)
        sql = (
            f"SELECT * FROM {self.table_name} "
            f"WHERE {clauses} "
            f"ORDER BY created_at DESC LIMIT ?"
        )
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(sql, params).fetchall()
        return [self._sqlite_row_to_dict(row) for row in rows]

    @classmethod
    def _query_terms(cls, text: str) -> set[str]:
        """Extract the same content-bearing terms ``_fts_query`` uses for
        matching, so the reranker compares apples to apples (post-particle-
        stripping, post-stopword, length-filtered)."""
        raw = re.findall(r"[\w가-힣]+", str(text or "").lower())
        out: set[str] = set()
        for tok in raw:
            stem = cls._strip_particle(tok)
            if len(stem) < cls._MIN_FTS_TERM_LEN:
                continue
            if stem in cls._KO_STOPWORDS:
                continue
            out.add(stem)
        return out

    @classmethod
    def _rerank(
        cls, query: str, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Re-order BM25 + LIKE candidates by lexical overlap with the
        query's content terms, with a small recency bonus to break ties
        toward newer turns. Pure-Python, no embedding.

        Overlap is measured as **substring** containment (not word-set
        intersection), so short Korean keywords like "주가" / "전망" and
        compound-noun prefixes like "삼성" correctly hit rows that store
        "목표주가는" / "삼성전자" — the same gap the LIKE fallback covers
        in retrieval. Using `_like_terms` (2-char floor) instead of
        `_query_terms` (3-char floor) keeps the reranker and the LIKE
        fallback aligned on what counts as a content token.

        Falls back to the BM25 + LIKE order when the query carries no
        scoreable terms (all stopwords / particles only).
        """
        q_terms = cls._like_terms(query)
        if not q_terms:
            return list(candidates)
        n = len(candidates)
        if n <= 1:
            return list(candidates)

        scored: list[tuple[float, int, dict[str, Any]]] = []
        for idx, row in enumerate(candidates):
            content = str(row.get("content") or "").lower()
            overlap = sum(1 for t in q_terms if t in content)
            overlap_ratio = overlap / len(q_terms)
            # Recency proxy: BM25 candidates are sorted by rank ASC; rows
            # carrying a newer ``created_at`` aren't reliably surfaced by
            # BM25, so we add the row's relative position-in-time inside
            # the candidate set. Newest candidate -> 1.0, oldest -> 0.0.
            recency = (n - 1 - idx) / max(1, n - 1)
            score = overlap_ratio + cls._RERANK_RECENCY_WEIGHT * recency
            # Stable secondary: original BM25 rank (lower idx = better).
            scored.append((score, -idx, row))
        scored.sort(key=lambda triple: (triple[0], triple[1]), reverse=True)
        return [row for _score, _tiebreak, row in scored]

    def _connect(self) -> sqlite3.Connection:
        """Open an independent connection for tests and legacy utilities."""
        conn = self.store._connect()
        conn.row_factory = sqlite3.Row
        return conn

    # FTS5 tokenizer of choice. We use ``trigram`` because the data is mostly
    # Korean (agglutinative + frequent compound nouns like "삼성전자"), where the
    # default ``unicode61`` tokenizer — which splits on whitespace/punctuation
    # only — buries recall behind exact-token matching: "삼성" never matches
    # "삼성전자", "목표주가" never matches "목표 주가", and any change in
    # particle/ending ("목표주가는" vs "목표 주가") drops the row from search.
    # ``trigram`` indexes overlapping 3-char ngrams, so partial matches work
    # across compound nouns and inflection.
    _FTS_TOKENIZER = "trigram"

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table_name} (
                id TEXT PRIMARY KEY,
                tier TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                token_count INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{{}}'
            )
            """
        )
        self.store.ensure_migration_meta(conn)
        self._ensure_fts_with_tokenizer(conn)
        conn.commit()

    def _ensure_fts_with_tokenizer(self, conn: sqlite3.Connection) -> None:
        """Ensure the FTS virtual table uses the desired tokenizer.

        Idempotent: when the FTS table already exists with the target
        tokenizer it returns immediately. When the legacy ``unicode61`` FTS
        is found, the table is dropped and recreated with the new tokenizer
        and every row from the base table is re-inserted so live recall
        survives the migration.
        """
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (self.fts_name,),
        ).fetchone()
        target_clause = f"tokenize='{self._FTS_TOKENIZER}'"
        if existing is not None and target_clause in str(existing[0] or ""):
            return
        if existing is not None:
            conn.execute(f"DROP TABLE {self.fts_name}")
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE {self.fts_name}
            USING fts5(id UNINDEXED, content, source, tokenize='{self._FTS_TOKENIZER}')
            """
        )
        rows = conn.execute(
            f"SELECT id, content, source FROM {self.table_name}"
        ).fetchall()
        if rows:
            conn.executemany(
                f"INSERT INTO {self.fts_name} (id, content, source) VALUES (?, ?, ?)",
                [(r[0], r[1] or "", r[2] or "") for r in rows],
            )

    def _append_sqlite(self, row: dict[str, Any]) -> None:
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            self._upsert_row(conn, row)
            if (
                not self.legacy_path.exists()
                and not self.legacy_db_path.exists()
                and not self._is_migration_done(conn)
            ):
                self._mark_migration_done(conn)
            conn.commit()

    def _upsert_row(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        item_id = str(row.get("id") or "").strip()
        if not item_id:
            return
        metadata = row.get("metadata")
        metadata_json = (
            json.dumps(metadata, ensure_ascii=False)
            if isinstance(metadata, dict)
            else "{}"
        )
        conn.execute(
            f"""
            INSERT OR REPLACE INTO {self.table_name}
                (id, tier, role, content, source, created_at, token_count, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                str(row.get("tier") or self.default_tier),
                str(row.get("role") or ""),
                str(row.get("content") or ""),
                str(row.get("source") or ""),
                str(row.get("created_at") or ""),
                int(row.get("token_count") or 0),
                metadata_json,
            ),
        )
        conn.execute(f"DELETE FROM {self.fts_name} WHERE id = ?", (item_id,))
        conn.execute(
            f"INSERT INTO {self.fts_name} (id, content, source) VALUES (?, ?, ?)",
            (
                item_id,
                str(row.get("content") or ""),
                str(row.get("source") or ""),
            ),
        )

    def _tail_sqlite(self, *, limit: int) -> list[dict[str, Any]]:
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"""
                SELECT * FROM {self.table_name}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [self._sqlite_row_to_dict(row) for row in reversed(rows)]

    def _search_sqlite(self, query: str, *, limit: int) -> list[dict[str, Any]]:
        match_query = self._fts_query(query)
        if not match_query:
            return []
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"""
                SELECT {self.table_name}.*, bm25({self.fts_name}) AS rank
                FROM {self.fts_name}
                JOIN {self.table_name} ON {self.table_name}.id = {self.fts_name}.id
                WHERE {self.fts_name} MATCH ?
                ORDER BY rank ASC, {self.table_name}.created_at DESC
                LIMIT ?
                """,
                (match_query, max(1, int(limit))),
            ).fetchall()
        return [self._sqlite_row_to_dict(row) for row in rows]

    def _ensure_migrated(self) -> None:
        if self._legacy_migrated:
            return
        if (
            not self.store.db_path.exists()
            and not self.legacy_db_path.exists()
            and not self.legacy_path.exists()
        ):
            self._legacy_migrated = True
            return
        with self.store.connection() as conn:
            self._ensure_schema(conn)
            if self._is_migration_done(conn):
                conn.commit()
                self._legacy_migrated = True
                self._rename_legacy()
                return
            for row in self._read_legacy_sqlite_rows():
                self._upsert_row(conn, row)
            for row in self.store.read_jsonl(self.legacy_path):
                migrated = dict(row)
                migrated["token_count"] = self.token_counter.count(
                    str(migrated.get("content") or "")
                )
                self._upsert_row(conn, migrated)
            self._mark_migration_done(conn)
            conn.commit()
        self._legacy_migrated = True
        self._rename_legacy()

    def _read_legacy_sqlite_rows(self) -> list[dict[str, Any]]:
        if not self.legacy_db_path.exists():
            return []
        try:
            with closing(sqlite3.connect(str(self.legacy_db_path), timeout=5.0)) as conn:
                conn.row_factory = sqlite3.Row
                exists = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = ?
                    """,
                    (self.table_name,),
                ).fetchone()
                if not exists:
                    return []
                rows = conn.execute(f"SELECT * FROM {self.table_name}").fetchall()
        except Exception:
            return []
        return [self._sqlite_row_to_dict(row) for row in rows]

    def _is_migration_done(self, conn: sqlite3.Connection) -> bool:
        return self.store.is_migrated(conn, self.migration_key)

    def _mark_migration_done(self, conn: sqlite3.Connection) -> None:
        self.store.mark_migrated(conn, self.migration_key)

    def _rename_legacy(self) -> None:
        self.store.rename_legacy(self.legacy_path)
        self.store.rename_legacy(self.legacy_db_path)

    def _has_storage(self) -> bool:
        return (
            self.store.db_path.exists()
            or self.legacy_db_path.exists()
            or self.legacy_path.exists()
        )

    # Tokens that occur in nearly every Korean conversational query and would
    # otherwise burn BM25 weight without identifying anything specific. Kept
    # small on purpose — over-pruning hurts recall more than it helps. Extend
    # only with words that are (a) length ≥ 2 (length-1 tokens are already
    # filtered out below) AND (b) genuinely content-free across topics.
    _KO_STOPWORDS = frozenset({
        # demonstratives / dummies
        "그거", "그게", "그건", "그곳", "이거", "이게", "이건", "이곳", "저거", "저게",
        # adverbs / interjections
        "좀", "더", "다시", "또", "그리고", "하지만", "그러나", "그래서", "그럼", "그런데",
        # interrogatives
        "어떻게", "어떤", "어떠한", "왜", "언제", "어디", "어디서", "무엇", "뭘", "뭐", "어느",
        # generic verbs/predicates frequently in chat
        "있어", "있나", "있나요", "있는지", "있을까", "있습니다", "있다",
        "없어", "없나요", "없습니다", "없다",
        "되나요", "됩니다", "된다",
        "한다", "합니다", "해줘", "해주세요",
        "알려줘", "알려주세요", "보여줘", "말해줘",
        # generic nouns/phrases
        "관련", "관련해서", "대해", "대해서", "위해", "통해", "따라", "같은", "정리",
        "내용", "이야기", "얘기", "사항", "정보",
        # particles that re.findall(r"[\w가-힣]+") can accidentally bundle
        "에서", "에게", "으로", "에서는", "에서도",
    })

    # Tokens shorter than this are skipped — the FTS5 ``trigram`` tokenizer
    # cannot index n-grams below this length, so a 2-char term yields a
    # phrase query with zero ngrams and always misses. The bench confirmed:
    # "AI" / "삼성" / "주가" / "전망" all fail under trigram unless they
    # already appear as part of a longer indexed run.
    _MIN_FTS_TERM_LEN = 3

    # Hard cap on terms ORed into one MATCH query. Beyond ~6 the OR list
    # starts hitting every row and BM25 degenerates into "most-common-word"
    # ranking. Stays well under FTS5's 1024-token limit.
    _MAX_FTS_TERMS = 6

    # Korean postpositions ("조사") that, when left attached to a noun, break
    # trigram phrase matching: the indexed row carries a different particle
    # (e.g. "리스크가") and the query carries another ("리스크와"), so the
    # last trigram of the phrase never aligns. Stripping the trailing
    # particle reduces "리스크와" → "리스크", which trigram-matches both
    # "리스크가" and "리스크는". Order matters: 2-char particles first so
    # "으로" is stripped before falling through to "로".
    _KO_PARTICLES = (
        "에서", "에게", "으로", "이라고", "라고", "한테", "께서", "이여",
        "의", "이", "가", "을", "를", "은", "는", "도", "만",
        "에", "와", "과", "로", "야", "여",
    )

    @classmethod
    def _strip_particle(cls, token: str) -> str:
        """Remove a trailing Korean postposition aggressively, leaving the
        length filtering to the caller (``_fts_query`` keeps ``≥3`` for
        trigram, ``_like_terms`` keeps ``≥2`` for substring). Stripping
        early — not deferring to a length floor here — is what makes
        "삼성에" reduce to "삼성" so the LIKE fallback can recover
        compound-noun prefixes ("삼성전자"). Safe on non-Korean tokens:
        they don't end with any entry in :attr:`_KO_PARTICLES`."""
        if len(token) < 2:
            return token
        for particle in cls._KO_PARTICLES:
            stem_len = len(token) - len(particle)
            if stem_len >= 1 and token.endswith(particle):
                return token[: -len(particle)]
        return token

    @classmethod
    def _fts_query(cls, query: str) -> str:
        """Build a trigram-tokenizer-friendly MATCH expression.

        Strategy (no external dependencies — pure stdlib):
        1. Extract word-like tokens (Hangul / latin / digits).
        2. Drop stopwords and tokens too short for trigram indexing.
        3. Sort by length descending so high-information compound nouns
           (e.g. "목표주가", "주주환원") win over generic ones.
        4. OR-combine the top-N. The reranker layer (separate step) then
           re-orders the top-N hits by lexical overlap with the original
           query, which is what restores precision in the AND direction
           without losing recall in the OR direction.
        """
        raw = re.findall(r"[\w가-힣]+", str(query or "").lower())
        seen: set[str] = set()
        terms: list[str] = []
        for tok in raw:
            tok = cls._strip_particle(tok)
            if len(tok) < cls._MIN_FTS_TERM_LEN:
                continue
            if tok in cls._KO_STOPWORDS:
                continue
            if tok in seen:
                continue
            seen.add(tok)
            terms.append(tok)
        # Longer first — compound nouns are typically more discriminative.
        terms.sort(key=len, reverse=True)
        terms = terms[: cls._MAX_FTS_TERMS]
        if not terms:
            return ""
        return " OR ".join(f'"{t}"' for t in terms)

    @staticmethod
    def _sqlite_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except Exception:
            metadata = {}
        return {
            "id": row["id"],
            "tier": row["tier"],
            "role": row["role"],
            "content": row["content"],
            "source": row["source"],
            "created_at": row["created_at"],
            "token_count": int(row["token_count"] or 0),
            "metadata": metadata if isinstance(metadata, dict) else {},
        }

    @staticmethod
    def _validate_identifier(value: str) -> str:
        text = str(value or "")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
            raise ValueError(f"unsafe sqlite identifier: {value!r}")
        return text
