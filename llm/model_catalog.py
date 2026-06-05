from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from db.db import get_app_data_dir


ModelKind = Literal["llm", "embedding"]


@dataclass(frozen=True)
class ModelSpec:
    id: str
    kind: ModelKind
    name: str
    short_name: str
    repo_id: str
    filename: str | None
    filename_glob: str
    quantization: str
    size_bytes: int
    license: str
    context_tokens: int | None = None

    @property
    def hf_url(self) -> str:
        filename = self.filename or self.filename_glob
        return f"https://huggingface.co/{self.repo_id}/resolve/main/{filename}"


LLM_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="qwen35-0.8b-q8_0",
        kind="llm",
        name="Qwen3.5 0.8B 8-bit",
        short_name="0.8B 8bit",
        repo_id="unsloth/Qwen3.5-0.8B-GGUF",
        filename=None,
        filename_glob="*Q8_0*.gguf",
        quantization="Q8_0",
        size_bytes=639_000_000,
        license="apache-2.0",
        context_tokens=262_144,
    ),
    ModelSpec(
        id="qwen35-2b-q8_0",
        kind="llm",
        name="Qwen3.5 2B 8-bit",
        short_name="2B 8bit",
        repo_id="unsloth/Qwen3.5-2B-GGUF",
        filename=None,
        filename_glob="*Q8_0*.gguf",
        quantization="Q8_0",
        size_bytes=1_570_000_000,
        license="apache-2.0",
        context_tokens=262_144,
    ),
    ModelSpec(
        id="qwen35-4b-q4",
        kind="llm",
        name="Qwen3.5 4B 4-bit",
        short_name="4B 4bit",
        repo_id="unsloth/Qwen3.5-4B-GGUF",
        filename=None,
        filename_glob="*Q4*.gguf",
        quantization="Q4",
        size_bytes=2_780_000_000,
        license="apache-2.0",
        context_tokens=262_144,
    ),
    ModelSpec(
        id="qwen35-9b-q4",
        kind="llm",
        name="Qwen3.5 9B 4-bit",
        short_name="9B 4bit",
        repo_id="unsloth/Qwen3.5-9B-GGUF",
        filename=None,
        filename_glob="*Q4*.gguf",
        quantization="Q4",
        size_bytes=5_840_000_000,
        license="apache-2.0",
        context_tokens=262_144,
    ),
)


EMBEDDING_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="granite-embedding-97m-r2-q8_0",
        kind="embedding",
        name="Granite Embedding 97M Multilingual R2 8-bit",
        short_name="Granite 97M Q8_0",
        repo_id="mykor/granite-embedding-97m-multilingual-r2-GGUF",
        filename=None,
        filename_glob="*Q8_0*.gguf",
        quantization="Q8_0",
        size_bytes=115_000_000,
        license="apache-2.0",
        context_tokens=32_768,
    ),
)


DEFAULT_LLM_MODEL_ID = LLM_MODELS[0].id
DEFAULT_EMBEDDING_MODEL_ID = EMBEDDING_MODELS[0].id


def model_root() -> Path:
    return get_app_data_dir() / "models"


def kind_dir(kind: ModelKind, root: Path | None = None) -> Path:
    return (root or model_root()) / kind


def all_models() -> tuple[ModelSpec, ...]:
    return (*LLM_MODELS, *EMBEDDING_MODELS)


def llm_models() -> tuple[ModelSpec, ...]:
    return LLM_MODELS


def embedding_models() -> tuple[ModelSpec, ...]:
    return EMBEDDING_MODELS


def get_model(model_id: str | None, *, kind: ModelKind | None = None) -> ModelSpec:
    aliases = {
        "qwen35-4b-q4_k_m": "qwen35-4b-q4",
        "qwen35-9b-q4_k_m": "qwen35-9b-q4",
    }
    model_id = aliases.get(str(model_id or ""), model_id)
    candidates = all_models()
    if kind is not None:
        candidates = tuple(model for model in candidates if model.kind == kind)
    for model in candidates:
        if model.id == model_id:
            return model
    if kind == "embedding":
        return EMBEDDING_MODELS[0]
    return LLM_MODELS[0]


def model_dir(spec: ModelSpec, root: Path | None = None) -> Path:
    safe_repo = spec.repo_id.replace("/", "__")
    return kind_dir(spec.kind, root) / safe_repo


def expected_model_path(spec: ModelSpec, root: Path | None = None) -> Path:
    filename = spec.filename or spec.filename_glob.replace("*", spec.id)
    return model_dir(spec, root) / filename


def find_model_file(spec: ModelSpec, root: Path | None = None) -> Path | None:
    directory = model_dir(spec, root)
    if spec.filename:
        path = directory / spec.filename
        if path.exists() and path.stat().st_size > 0:
            return path
    if not directory.exists():
        return None
    matches = sorted(
        path
        for path in directory.glob(spec.filename_glob)
        if path.is_file() and path.stat().st_size > 0
    )
    return matches[0] if matches else None


def is_installed(spec: ModelSpec, root: Path | None = None) -> bool:
    return find_model_file(spec, root) is not None


def installed_llm_models(root: Path | None = None) -> list[ModelSpec]:
    return [model for model in LLM_MODELS if is_installed(model, root)]


def selected_model_from_settings(settings: dict) -> ModelSpec:
    model_settings = settings.get("model") if isinstance(settings, dict) else None
    if not isinstance(model_settings, dict):
        return get_model(DEFAULT_LLM_MODEL_ID, kind="llm")
    model_id = str(model_settings.get("modelId") or "")
    if model_id:
        return get_model(model_id, kind="llm")

    legacy_name = str(model_settings.get("modelName") or "")
    legacy_map = {
        "0.8B": "qwen35-0.8b-q8_0",
        "2B": "qwen35-2b-q8_0",
        "4B": "qwen35-4b-q4",
        "9B": "qwen35-9b-q4",
    }
    return get_model(legacy_map.get(legacy_name, DEFAULT_LLM_MODEL_ID), kind="llm")


def selected_embedding_from_settings(settings: dict) -> ModelSpec:
    model_settings = settings.get("embeddingModel") if isinstance(settings, dict) else None
    if not isinstance(model_settings, dict):
        return get_model(DEFAULT_EMBEDDING_MODEL_ID, kind="embedding")
    return get_model(str(model_settings.get("modelId") or ""), kind="embedding")


def default_model_settings() -> dict:
    llm = get_model(DEFAULT_LLM_MODEL_ID, kind="llm")
    embedding = get_model(DEFAULT_EMBEDDING_MODEL_ID, kind="embedding")
    return {
        "model": {
            "modelId": llm.id,
            "modelName": llm.name,
        },
        "embeddingModel": {
            "modelId": embedding.id,
            "modelName": embedding.name,
        },
        "launcher": {
            "initialModelSelected": False,
        },
        "llamaContext": {
            "mode": "auto",
            "tokens": 32768,
        },
    }


def bytes_label(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1000.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1000.0
