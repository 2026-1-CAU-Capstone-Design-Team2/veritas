"""Task 2 — intent coverage (VERIFY_DESIGN.md §4).

Decomposes the user intent into a *multi-query* set drawn from artifacts
(``request.md`` + ``plan.json`` + ``grounding.json`` — no hand-written queries),
groups those queries into facets, and produces a facet × doc coverage matrix
plus a per-doc intent score and per-facet coverage gaps. Retrieval and
community detection are shared with Task 1 (§4.5).
"""
