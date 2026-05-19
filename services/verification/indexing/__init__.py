"""Shared retrieval primitives: BM25 (sparse), dense embeddings, and RRF fusion.

These are the common channels used by all three verification tasks. They hold
no task logic — just indexing and scoring — and are reused so each task
pipeline stays thin (VERIFY_DESIGN.md §1.6, §2).
"""
