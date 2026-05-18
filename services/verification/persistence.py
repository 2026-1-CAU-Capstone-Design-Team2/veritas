"""Disk IO for verification artifacts (VERIFY_DESIGN.md §0.2 / §1.2).

Together with ``artifact_loader.py`` this is the *only* module that writes to
disk; every other module stays close to a pure function. Output layout:

```
runs/<workspace>/verification/
    meta.json                # config_hash · timestamp · tasks completed · doc count
    sections.json            # Task 1 — SectionResult
    intent_coverage.json     # Task 2 — IntentResult
    consensus.json           # Task 3 — ConsensusResult
```

The frontend reads these back through ``api/services/verify_service.py`` —
this module knows nothing about UI-facing payloads (level / matchRate / …).
That adaptation belongs in the API thin wrapper.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .models import (
    ConceptCluster,
    ConflictFlag,
    ConsensusResult,
    CoverageGap,
    Facet,
    FlowSection,
    IntentResult,
    SectionResult,
    SentenceAssignment,
    VerificationArtifacts,
    VerificationConfig,
)

logger = logging.getLogger(__name__)

_META_FILE = "meta.json"
_SECTIONS_FILE = "sections.json"
_INTENT_FILE = "intent_coverage.json"
_CONSENSUS_FILE = "consensus.json"


# ---------------------------------------------------------------------------
# JSON-safe helpers
# ---------------------------------------------------------------------------


def _to_jsonable(value):
    """Recursively convert dataclasses / numpy values into JSON-safe primitives."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if dataclasses.is_dataclass(value):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    return value


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("verification: failed to read %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Public IO
# ---------------------------------------------------------------------------


class VerificationPersistence:
    """``VerificationArtifacts`` <-> ``runs/<ws>/verification/*.json``.

    Stateless: instantiated once with the ``runs/`` root and called per workspace.
    Each task file is written independently so a partial run (e.g. only Task 3)
    does not clobber previously-saved results for the other tasks — the meta
    file then records which tasks the current results came from.
    """

    def __init__(self, output_root: str | Path = "runs") -> None:
        self._output_root = Path(output_root)

    # -- paths ----------------------------------------------------------------

    def _verification_dir(self, workspace: str | Path) -> Path:
        root = Path(workspace)
        if not root.is_absolute():
            root = self._output_root / workspace
        return root / "verification"

    def has_results(self, workspace: str | Path) -> bool:
        """True if a previous run wrote at least the meta file for this workspace."""
        return (self._verification_dir(workspace) / _META_FILE).exists()

    # -- write ----------------------------------------------------------------

    def persist(
        self,
        workspace: str | Path,
        artifacts: VerificationArtifacts,
        *,
        cfg: VerificationConfig,
        completed_tasks: list[str],
        doc_count: int,
    ) -> Path:
        """Write whichever task results ``artifacts`` carries + a fresh meta file.

        ``completed_tasks`` is the canonical record of which task pipelines
        actually ran in this invocation (so a Task 1-only rerun does not
        invalidate a previously-saved Task 3 result on disk).
        """
        directory = self._verification_dir(workspace)
        directory.mkdir(parents=True, exist_ok=True)

        if artifacts.sections is not None and "sections" in completed_tasks:
            _write_json(directory / _SECTIONS_FILE, _serialize_section_result(artifacts.sections))
        if artifacts.intent is not None and "intent" in completed_tasks:
            _write_json(directory / _INTENT_FILE, _serialize_intent_result(artifacts.intent))
        if artifacts.consensus is not None and "consensus" in completed_tasks:
            _write_json(directory / _CONSENSUS_FILE, _serialize_consensus_result(artifacts.consensus))

        _write_json(
            directory / _META_FILE,
            {
                "configHash": cfg.fingerprint(),
                "completedTasks": list(completed_tasks),
                "documentCount": int(doc_count),
                "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )
        return directory

    # -- read -----------------------------------------------------------------

    def load(self, workspace: str | Path) -> tuple[VerificationArtifacts, dict] | None:
        """Reload whatever the last run persisted. Returns ``None`` if no meta file."""
        directory = self._verification_dir(workspace)
        meta = _read_json(directory / _META_FILE)
        if meta is None:
            return None

        artifacts = VerificationArtifacts(config_hash=str(meta.get("configHash") or ""))

        sections_payload = _read_json(directory / _SECTIONS_FILE)
        if sections_payload is not None:
            artifacts.sections = _deserialize_section_result(sections_payload)

        intent_payload = _read_json(directory / _INTENT_FILE)
        if intent_payload is not None:
            artifacts.intent = _deserialize_intent_result(intent_payload)

        consensus_payload = _read_json(directory / _CONSENSUS_FILE)
        if consensus_payload is not None:
            artifacts.consensus = _deserialize_consensus_result(consensus_payload)

        return artifacts, meta


# ---------------------------------------------------------------------------
# Per-result serializers / deserializers
# ---------------------------------------------------------------------------


def _serialize_section_result(result: SectionResult) -> dict:
    return _to_jsonable(result)


def _deserialize_section_result(payload: dict) -> SectionResult:
    sections: list[FlowSection] = []
    for item in payload.get("sections", []):
        if not isinstance(item, dict):
            continue
        assignments = [
            SentenceAssignment(
                doc_id=str(a.get("doc_id", "")),
                paragraph_index=int(a.get("paragraph_index", 0)),
                sentence_index=int(a.get("sentence_index", 0)),
                text=str(a.get("text", "")),
                fit_score=float(a.get("fit_score", 0.0)),
            )
            for a in item.get("sentence_assignments", [])
            if isinstance(a, dict)
        ]
        sections.append(
            FlowSection(
                id=int(item.get("id", 0)),
                order=int(item.get("order", item.get("id", 0))),
                title=str(item.get("title", "")),
                description=str(item.get("description", "")),
                role=str(item.get("role", "body")),
                keywords=[str(k) for k in item.get("keywords", [])],
                sentence_assignments=assignments,
            )
        )
    return SectionResult(
        sections=sections,
        flow_source=str(payload.get("flow_source") or "llm"),
        sentence_count=int(payload.get("sentence_count") or 0),
        document_count=int(payload.get("document_count") or 0),
    )


def _serialize_intent_result(result: IntentResult) -> dict:
    return _to_jsonable(result)


def _deserialize_intent_result(payload: dict) -> IntentResult:
    facets = [
        Facet(
            id=int(item.get("id", 0)),
            label_terms=[str(t) for t in item.get("label_terms", [])],
            origin_queries=[str(o) for o in item.get("origin_queries", [])],
        )
        for item in payload.get("facets", [])
        if isinstance(item, dict)
    ]
    matrix_raw = payload.get("doc_facet_matrix")
    matrix = (
        np.asarray(matrix_raw, dtype=np.float32)
        if isinstance(matrix_raw, list) and matrix_raw
        else None
    )
    doc_intent = {str(k): float(v) for k, v in (payload.get("doc_intent_score") or {}).items()}
    gaps = [
        CoverageGap(
            facet_id=int(item.get("facet_id", 0)),
            label_terms=[str(t) for t in item.get("label_terms", [])],
            top_doc_score=float(item.get("top_doc_score", 0.0)),
        )
        for item in payload.get("coverage_gap", [])
        if isinstance(item, dict)
    ]
    return IntentResult(
        facets=facets,
        doc_facet_matrix=matrix,
        doc_intent_score=doc_intent,
        coverage_gap=gaps,
        doc_order=[str(d) for d in payload.get("doc_order", [])],
    )


def _serialize_consensus_result(result: ConsensusResult) -> dict:
    return _to_jsonable(result)


def _deserialize_consensus_result(payload: dict) -> ConsensusResult:
    clusters = [
        ConceptCluster(
            id=int(item.get("id", 0)),
            label_terms=[str(t) for t in item.get("label_terms", [])],
            kp_ids=[int(i) for i in item.get("kp_ids", [])],
            domains=[str(d) for d in item.get("domains", [])],
            pagerank=float(item.get("pagerank", 0.0)),
            diversity=float(item.get("diversity", 0.0)),
            authority_mean=float(item.get("authority_mean", 0.0)),
            composite=float(item.get("composite", 0.0)),
        )
        for item in payload.get("concept_clusters", [])
        if isinstance(item, dict)
    ]
    conflicts = [
        ConflictFlag(
            cluster_id=int(item.get("cluster_id", 0)),
            type=str(item.get("type", "")),
            score=float(item.get("score", 0.0)),
            evidence_kp_ids=[int(i) for i in item.get("evidence_kp_ids", [])],
            partition=(
                {int(k): int(v) for k, v in item["partition"].items()}
                if isinstance(item.get("partition"), dict)
                else None
            ),
        )
        for item in payload.get("conflicts", [])
        if isinstance(item, dict)
    ]
    domain_auth = {str(k): float(v) for k, v in (payload.get("domain_authority") or {}).items()}
    return ConsensusResult(
        concept_clusters=clusters,
        domain_authority=domain_auth,
        conflicts=conflicts,
    )


__all__ = ["VerificationPersistence"]
