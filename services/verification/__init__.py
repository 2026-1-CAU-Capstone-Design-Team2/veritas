"""Veritas verification layer.

Turns ``runs/<workspace>/`` artifacts into verification outputs —
section flow planning, source reliability judgement, and cross-source
consensus / conflict detection.

See ``VERIFY_DESIGN.md`` for the full design. This package owns all
calculation logic and state (per ARCHITECTURE.md's "Service = state/business
logic owner" rule); ``api/services/`` stays a thin adapter.
"""

from .artifact_loader import ArtifactLoader
from .indexing.dense_index import DenseIndex
from .models import VerificationArtifacts, VerificationConfig
from .persistence import VerificationPersistence
from .service import ALL_TASKS, VerificationService

__all__ = [
    "ALL_TASKS",
    "ArtifactLoader",
    "DenseIndex",
    "VerificationArtifacts",
    "VerificationConfig",
    "VerificationPersistence",
    "VerificationService",
]
