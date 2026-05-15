"""Veritas verification layer.

Turns ``runs/<workspace>/`` artifacts into three verification outputs —
section clustering, intent coverage, cross-source consensus — using only an
embedding model and IR/NLP algorithms (no extra LLM generation calls).

See ``VERIFY_DESIGN.md`` for the full design. This package owns all
calculation logic and state (per ARCHITECTURE.md's "Service = state/business
logic owner" rule); ``api/services/`` stays a thin adapter.
"""
