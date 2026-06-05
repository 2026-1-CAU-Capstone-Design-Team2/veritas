from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from db.db import get_app_data_dir


ModelKind = Literal["llm", "embedding"]


@dataclass(frozen=True)
class QuantizationSpec:
    key: str
    label: str
    filename_glob: str
    estimated_bits_per_weight: float
    quality_rank: int


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
    family: str = ""
    parameter_size_b: float | None = None
    active_parameter_size_b: float | None = None
    architecture: str = ""
    quantization_key: str = ""
    estimated_bits_per_weight: float | None = None
    kv_bytes_per_token: int | None = None
    recommended: bool = False
    display_order: int = 0

    @property
    def hf_url(self) -> str:
        filename = self.filename or self.filename_glob
        return f"https://huggingface.co/{self.repo_id}/resolve/main/{filename}"

    @property
    def parameter_label(self) -> str:
        if self.parameter_size_b is None:
            return ""
        if float(self.parameter_size_b).is_integer():
            return f"{int(self.parameter_size_b)}B"
        return f"{self.parameter_size_b:g}B"


QUANTIZATION_LEVELS: tuple[QuantizationSpec, ...] = (
    QuantizationSpec(
        key="bf16",
        label="BF16",
        filename_glob="*Qwen3.5*-BF16*.gguf",
        estimated_bits_per_weight=16.0,
        quality_rank=7,
    ),
    QuantizationSpec(
        key="q8_0",
        label="Q8",
        filename_glob="*Q8*.gguf",
        estimated_bits_per_weight=8.0,
        quality_rank=6,
    ),
    QuantizationSpec(
        key="q6",
        label="Q6",
        filename_glob="*Q6*.gguf",
        estimated_bits_per_weight=6.0,
        quality_rank=5,
    ),
    QuantizationSpec(
        key="q5",
        label="Q5",
        filename_glob="*Q5*.gguf",
        estimated_bits_per_weight=5.0,
        quality_rank=4,
    ),
    QuantizationSpec(
        key="q4",
        label="Q4",
        filename_glob="*Q4*.gguf",
        estimated_bits_per_weight=4.25,
        quality_rank=3,
    ),
    QuantizationSpec(
        key="q3",
        label="Q3",
        filename_glob="*Q3*.gguf",
        estimated_bits_per_weight=3.5,
        quality_rank=2,
    ),
    QuantizationSpec(
        key="q2",
        label="Q2",
        filename_glob="*Q2*.gguf",
        estimated_bits_per_weight=2.75,
        quality_rank=1,
    ),
)


@dataclass(frozen=True)
class _LLMSizeSpec:
    size_key: str
    label: str
    repo_id: str
    parameter_size_b: float
    context_tokens: int
    kv_bytes_per_token: int
    architecture: str = "dense"
    active_parameter_size_b: float | None = None


QWEN35_CONTEXT_TOKENS = 262_144


_LLM_SIZE_SPECS: tuple[_LLMSizeSpec, ...] = (
    _LLMSizeSpec(
        size_key="0.8b",
        label="0.8B",
        repo_id="unsloth/Qwen3.5-0.8B-GGUF",
        parameter_size_b=0.8,
        context_tokens=QWEN35_CONTEXT_TOKENS,
        kv_bytes_per_token=16 * 1024,
    ),
    _LLMSizeSpec(
        size_key="2b",
        label="2B",
        repo_id="unsloth/Qwen3.5-2B-GGUF",
        parameter_size_b=2.0,
        context_tokens=QWEN35_CONTEXT_TOKENS,
        kv_bytes_per_token=24 * 1024,
    ),
    _LLMSizeSpec(
        size_key="4b",
        label="4B",
        repo_id="unsloth/Qwen3.5-4B-GGUF",
        parameter_size_b=4.0,
        context_tokens=QWEN35_CONTEXT_TOKENS,
        kv_bytes_per_token=48 * 1024,
    ),
    _LLMSizeSpec(
        size_key="9b",
        label="9B",
        repo_id="unsloth/Qwen3.5-9B-GGUF",
        parameter_size_b=9.0,
        context_tokens=QWEN35_CONTEXT_TOKENS,
        kv_bytes_per_token=72 * 1024,
    ),
    _LLMSizeSpec(
        size_key="27b",
        label="27B",
        repo_id="unsloth/Qwen3.5-27B-GGUF",
        parameter_size_b=27.0,
        context_tokens=QWEN35_CONTEXT_TOKENS,
        kv_bytes_per_token=128 * 1024,
    ),
    _LLMSizeSpec(
        size_key="35b-a3b",
        label="35B-A3B",
        repo_id="unsloth/Qwen3.5-35B-A3B-GGUF",
        parameter_size_b=35.0,
        active_parameter_size_b=3.0,
        architecture="moe",
        context_tokens=QWEN35_CONTEXT_TOKENS,
        kv_bytes_per_token=160 * 1024,
    ),
)


_KNOWN_SIZE_BYTES: dict[tuple[str, str], int] = {
    ("0.8b", "q8_0"): 639_000_000,
    ("2b", "q8_0"): 1_570_000_000,
    ("4b", "q4"): 2_780_000_000,
    ("9b", "bf16"): 17_000_000_000,
    ("9b", "q4"): 5_840_000_000,
    ("27b", "bf16"): 54_000_000_000,
    ("35b-a3b", "bf16"): 69_400_000_000,
}


def _model_id(size: _LLMSizeSpec, quant: QuantizationSpec) -> str:
    return f"qwen35-{size.size_key}-{quant.key}"


def _estimate_size_bytes(size: _LLMSizeSpec, quant: QuantizationSpec) -> int:
    known = _KNOWN_SIZE_BYTES.get((size.size_key, quant.key))
    if known is not None:
        return known
    raw = size.parameter_size_b * 1_000_000_000 * (
        quant.estimated_bits_per_weight / 8.0
    )
    return int(raw * 1.12)


def _build_llm_models() -> tuple[ModelSpec, ...]:
    models: list[ModelSpec] = []
    order = 0
    for size in _LLM_SIZE_SPECS:
        for quant in QUANTIZATION_LEVELS:
            model_id = _model_id(size, quant)
            models.append(
                ModelSpec(
                    id=model_id,
                    kind="llm",
                    name=f"Qwen3.5 {size.label} {quant.label}",
                    short_name=f"{size.label} {quant.label}",
                    repo_id=size.repo_id,
                    filename=None,
                    filename_glob=quant.filename_glob,
                    quantization=quant.label,
                    size_bytes=_estimate_size_bytes(size, quant),
                    license="apache-2.0",
                    context_tokens=size.context_tokens,
                    family="Qwen3.5",
                    parameter_size_b=size.parameter_size_b,
                    active_parameter_size_b=size.active_parameter_size_b,
                    architecture=size.architecture,
                    quantization_key=quant.key,
                    estimated_bits_per_weight=quant.estimated_bits_per_weight,
                    kv_bytes_per_token=size.kv_bytes_per_token,
                    recommended=model_id in {
                        "qwen35-0.8b-q8_0",
                        "qwen35-2b-q8_0",
                        "qwen35-4b-q4",
                        "qwen35-9b-q4",
                    },
                    display_order=order,
                )
            )
            order += 1
    return tuple(models)


LLM_MODELS: tuple[ModelSpec, ...] = _build_llm_models()


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
        family="Granite Embedding",
        parameter_size_b=0.097,
        quantization_key="q8_0",
        estimated_bits_per_weight=8.0,
        kv_bytes_per_token=0,
    ),
)


DEFAULT_LLM_MODEL_ID = "qwen35-0.8b-q8_0"
DEFAULT_EMBEDDING_MODEL_ID = EMBEDDING_MODELS[0].id

def _build_model_aliases() -> dict[str, str]:
    available_ids = {model.id for model in LLM_MODELS}
    size_aliases = {
        "0.8b": "0.8b",
        "2b": "2b",
        "4b": "4b",
        "9b": "9b",
        "27b": "27b",
        "35b": "35b-a3b",
        "35b-a3b": "35b-a3b",
    }
    quant_aliases = {
        "q16": "bf16",
        "f16": "bf16",
        "bf16": "bf16",
        "q8": "q8_0",
        "q8_0": "q8_0",
        "q6": "q6",
        "q6_k": "q6",
        "q5": "q5",
        "q5_k_m": "q5",
        "q4": "q4",
        "q4_k_m": "q4",
        "q3": "q3",
        "q3_k_m": "q3",
        "q2": "q2",
        "q2_k": "q2",
    }
    aliases: dict[str, str] = {}
    for source_size, canonical_size in size_aliases.items():
        for source_quant, canonical_quant in quant_aliases.items():
            source = f"qwen35-{source_size}-{source_quant}"
            target = f"qwen35-{canonical_size}-{canonical_quant}"
            if source != target and target in available_ids:
                aliases[source] = target
    return aliases


MODEL_ALIASES: dict[str, str] = _build_model_aliases()


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


def quantization_levels() -> tuple[QuantizationSpec, ...]:
    return QUANTIZATION_LEVELS


def llm_model_sizes() -> tuple[str, ...]:
    return tuple(size.label for size in _LLM_SIZE_SPECS)


def llm_variants_for(
    *,
    family: str | None = None,
    parameter_size_b: float | None = None,
) -> tuple[ModelSpec, ...]:
    models = LLM_MODELS
    if family:
        family_normalized = family.strip().lower()
        models = tuple(
            model for model in models if model.family.strip().lower() == family_normalized
        )
    if parameter_size_b is not None:
        models = tuple(
            model
            for model in models
            if model.parameter_size_b is not None
            and abs(model.parameter_size_b - float(parameter_size_b)) < 0.001
        )
    return models


def get_model(model_id: str | None, *, kind: ModelKind | None = None) -> ModelSpec:
    model_id = MODEL_ALIASES.get(str(model_id or ""), model_id)
    candidates = all_models()
    if kind is not None:
        candidates = tuple(model for model in candidates if model.kind == kind)
    for model in candidates:
        if model.id == model_id:
            return model
    if kind == "embedding":
        return EMBEDDING_MODELS[0]
    return get_model(DEFAULT_LLM_MODEL_ID, kind="llm")


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
        "27B": "qwen35-27b-q4",
        "35B": "qwen35-35b-a3b-q4",
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
    return f"{int(value)} B"
