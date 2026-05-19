"""Task 3 — cross-source consensus (VERIFY_DESIGN.md §5).

Treats every doc summary's Key Points as corpus-internal claim units, groups
them into concept clusters with a dual-channel similarity graph, derives domain
authority from the corpus itself (HITS — no external whitelist), and flags
candidate conflicts. ``consensus_pipeline`` is the entry point; the algorithm
bodies live in the sibling modules.
"""
