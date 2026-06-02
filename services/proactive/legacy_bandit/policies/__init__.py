"""Bandit policies used by the proactive orchestrator.

Two policies, two responsibilities:

- :class:`DisjointDiscountedLinUCB` picks the *candidate* suggestion type
  given the suggest feature vector (``x_suggest``).
- :class:`ActionCenteredEngagePolicy` decides whether to actually show that
  candidate vs. no-op, given the engage feature vector (``x_engage``) — and
  learns the *incremental* utility of intervening over no-op via the action-
  centering estimator (Greenewald, Tewari et al. style).

Both expose ``to_payload`` / ``from_payload`` for ``policy_store`` round-trip.
"""
from __future__ import annotations

from .action_centered_engage import ActionCenteredEngagePolicy
from .discounted_linucb import DisjointDiscountedLinUCB

__all__ = ["ActionCenteredEngagePolicy", "DisjointDiscountedLinUCB"]
