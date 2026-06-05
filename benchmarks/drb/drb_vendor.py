"""Locate and validate the vendored DeepResearch Bench (DRB) checkout.

DRB lives at the repo-internal path ``deep_research_bench/`` (a temporarily
vendored external evaluator tree, separate from Veritas production code). This
module is the single place that knows that layout: it resolves the root,
validates the required files exist, builds the official raw-output path, and
refuses traversal-like roots so the repo-internal CLIs can't be pointed outside
the tree.

Pure path logic — no DRB import, no network, no LLM.
"""

from __future__ import annotations

import re
from pathlib import Path


DEFAULT_DRB_ROOT = "deep_research_bench"

# Files/dirs that must exist for the checkout to be a usable DRB tree.
REQUIRED_FILES = (
    "README.md",
    "LICENSE",
    "requirements.txt",
    "run_benchmark.sh",
    "deepresearch_bench_race.py",
    "data/prompt_data/query.jsonl",
)
REQUIRED_DIRS = ("utils", "prompt")

QUERY_SUBPATH = "data/prompt_data/query.jsonl"
RAW_DATA_SUBDIR = "data/test_data/raw_data"

# A model name becomes a file stem (``<model_name>.jsonl``) and a results dir, so
# it must not carry path separators or traversal.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class DRBLayoutError(ValueError):
    """Raised for a missing/invalid DRB layout or an unsafe root/model name."""


def _repo_root() -> Path:
    # benchmarks/drb/drb_vendor.py → repo root is two parents up.
    return Path(__file__).resolve().parents[2]


def resolve_drb_root(root: str | Path | None = None) -> Path:
    """Resolve the DRB root to an absolute path, rejecting traversal.

    ``None`` resolves to the repo-internal default ``deep_research_bench/``. An
    explicit relative root is resolved against the repo root. Any root with a
    ``..`` segment is rejected — these CLIs are meant to stay inside the vendored
    tree, so a traversal-like root is always an error rather than silently
    escaping the repo.
    """
    raw = DEFAULT_DRB_ROOT if root is None else str(root)
    candidate = Path(raw)
    if ".." in candidate.parts:
        raise DRBLayoutError(f"DRB root must not contain '..': {raw!r}")
    if candidate.is_absolute():
        return candidate.resolve()
    return (_repo_root() / candidate).resolve()


def missing_layout_entries(root: str | Path | None = None) -> list[str]:
    """Return the required files/dirs absent under *root* (empty when valid)."""
    base = resolve_drb_root(root)
    missing: list[str] = []
    for rel in REQUIRED_FILES:
        if not (base / rel).is_file():
            missing.append(rel)
    for rel in REQUIRED_DIRS:
        if not (base / rel).is_dir():
            missing.append(rel + "/")
    return missing


def validate_layout(root: str | Path | None = None) -> Path:
    """Resolve *root* and confirm the DRB layout; raise on anything missing."""
    base = resolve_drb_root(root)
    missing = missing_layout_entries(base)
    if missing:
        raise DRBLayoutError(
            f"DRB layout invalid under {base}: missing {missing}"
        )
    return base


def is_valid_layout(root: str | Path | None = None) -> bool:
    try:
        validate_layout(root)
        return True
    except DRBLayoutError:
        return False


def query_file_path(root: str | Path | None = None) -> Path:
    """Path to the 100-task query file (``data/prompt_data/query.jsonl``)."""
    return resolve_drb_root(root) / QUERY_SUBPATH


def raw_data_dir(root: str | Path | None = None) -> Path:
    return resolve_drb_root(root) / RAW_DATA_SUBDIR


def raw_output_path(model_name: str, root: str | Path | None = None) -> Path:
    """Official raw-output path ``.../raw_data/<model_name>.jsonl``.

    ``model_name`` is validated so it can only ever be a flat file stem — no
    slashes, no traversal — since it also names a results directory downstream.
    """
    if not _MODEL_NAME_RE.match(str(model_name or "")):
        raise DRBLayoutError(f"invalid model_name: {model_name!r}")
    return raw_data_dir(root) / f"{model_name}.jsonl"


def meta_output_path(model_name: str, root: str | Path | None = None) -> Path:
    """Sidecar metadata path (``<raw_output>.meta.jsonl``)."""
    raw = raw_output_path(model_name, root)
    return raw.with_name(raw.name + ".meta.jsonl")


__all__ = [
    "DEFAULT_DRB_ROOT",
    "REQUIRED_FILES",
    "REQUIRED_DIRS",
    "DRBLayoutError",
    "resolve_drb_root",
    "missing_layout_entries",
    "validate_layout",
    "is_valid_layout",
    "query_file_path",
    "raw_data_dir",
    "raw_output_path",
    "meta_output_path",
]
