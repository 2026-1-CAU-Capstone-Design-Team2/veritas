"""Task 1 — section clustering (VERIFY_DESIGN.md §3).

Groups ``plan.must_cover[]`` into auto-identified report sections, retrieves
chunk-level evidence per section (BM25 + dense, fused with RRF), aggregates to
doc-level scores, and labels each section with c-TF-IDF terms drawn from its
own corpus. The community-detection step reuses ``graph.py`` and the labelling
step reuses ``labeling.py`` — this package owns only Task 1 orchestration
(§1.1, §4.5).
"""
