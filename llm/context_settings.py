from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .hardware_policy import (
    RISK_FIT,
    RISK_RELAXED,
    RISK_RISKY,
    MemorySnapshot,
    detect_memory,
    estimate_runtime,
    recommended_context_tokens as recommended_model_context_tokens,
)

if TYPE_CHECKING:
    from .model_catalog import ModelSpec


CONTEXT_TIERS = (8192, 16384, 32768, 50000, 90000)
DEFAULT_CONTEXT_MODE = "auto"
DEFAULT_CONTEXT_TOKENS = 32768
APP_MAX_CONTEXT_TOKENS = 90000


@dataclass(frozen=True)
class ContextOption:
    tokens: int
    label: str
    risk: str
    recommended: bool = False


def recommended_context_tokens(
    *,
    available_bytes: int | None = None,
    model_limit: int | None = None,
    model: "ModelSpec | None" = None,
    parallel_slots: int = 1,
) -> int:
    if available_bytes is None:
        available_bytes = detect_memory().available_bytes
    if model is not None:
        return recommended_model_context_tokens(
            model,
            context_tiers=CONTEXT_TIERS,
            available_bytes=available_bytes,
            parallel_slots=parallel_slots,
            model_limit=model_limit,
            app_limit=APP_MAX_CONTEXT_TOKENS,
        )

    available_gb = max(0.0, float(available_bytes) / 1024**3)
    if available_gb < 8:
        tokens = 8192
    elif available_gb < 16:
        tokens = 16384
    elif available_gb < 32:
        tokens = 32768
    elif available_gb < 64:
        tokens = 50000
    else:
        tokens = 90000
    return clamp_context_tokens(tokens, model_limit=model_limit)


def clamp_context_tokens(tokens: int, *, model_limit: int | None = None) -> int:
    limit = APP_MAX_CONTEXT_TOKENS
    if model_limit and model_limit > 0:
        limit = min(limit, int(model_limit))
    return max(CONTEXT_TIERS[0], min(int(tokens), limit))


def normalize_context_settings(
    payload: dict[str, Any] | None,
    *,
    model_limit: int | None = None,
    model: "ModelSpec | None" = None,
    parallel_slots: int = 1,
) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    mode = str(payload.get("mode") or DEFAULT_CONTEXT_MODE).strip().lower()
    if mode not in {"auto", "manual"}:
        mode = DEFAULT_CONTEXT_MODE
    try:
        tokens = int(payload.get("tokens") or DEFAULT_CONTEXT_TOKENS)
    except (TypeError, ValueError):
        tokens = DEFAULT_CONTEXT_TOKENS

    snapshot = detect_memory()
    auto_tokens = recommended_context_tokens(
        available_bytes=snapshot.available_bytes,
        model_limit=model_limit,
        model=model,
        parallel_slots=parallel_slots,
    )
    if mode == "auto":
        tokens = auto_tokens
    else:
        tokens = clamp_context_tokens(tokens, model_limit=model_limit)

    result: dict[str, Any] = {
        "mode": mode,
        "tokens": tokens,
        "lastAutoTokens": auto_tokens,
        "memory": memory_payload(snapshot),
    }
    if model is not None:
        estimate = estimate_runtime(
            model,
            context_per_slot_tokens=tokens,
            parallel_slots=parallel_slots,
            available_bytes=snapshot.available_bytes,
        )
        result["risk"] = estimate.risk
        result["maxParallelSlots"] = estimate.max_parallel_slots
        result["hardware"] = estimate.to_payload()
    return result


def effective_context_tokens(
    settings: dict[str, Any] | None,
    *,
    model_limit: int | None = None,
    model: "ModelSpec | None" = None,
    parallel_slots: int = 1,
) -> int:
    env_value = os.getenv("VERITAS_LLAMA_CTX")
    if env_value and env_value.strip():
        try:
            return clamp_context_tokens(int(env_value), model_limit=model_limit)
        except ValueError:
            pass
    llama_context = settings.get("llamaContext") if isinstance(settings, dict) else None
    normalized = normalize_context_settings(
        llama_context,
        model_limit=model_limit,
        model=model,
        parallel_slots=parallel_slots,
    )
    return int(normalized["tokens"])


def context_options(
    *,
    model_limit: int | None = None,
    model: "ModelSpec | None" = None,
    parallel_slots: int = 1,
) -> list[ContextOption]:
    auto_tokens = recommended_context_tokens(
        model_limit=model_limit,
        model=model,
        parallel_slots=parallel_slots,
    )
    options: list[ContextOption] = []
    for tokens in CONTEXT_TIERS:
        clamped = clamp_context_tokens(tokens, model_limit=model_limit)
        if clamped != tokens:
            continue
        options.append(
            ContextOption(
                tokens=tokens,
                label=f"{_format_tokens(tokens)} tokens",
                risk=context_risk(
                    tokens,
                    auto_tokens,
                    model=model,
                    parallel_slots=parallel_slots,
                ),
                recommended=tokens == auto_tokens,
            )
        )
    return options


def context_risk(
    tokens: int,
    recommended_tokens: int | None = None,
    *,
    model: "ModelSpec | None" = None,
    parallel_slots: int = 1,
    available_bytes: int | None = None,
) -> str:
    if model is not None:
        return estimate_runtime(
            model,
            context_per_slot_tokens=tokens,
            parallel_slots=parallel_slots,
            available_bytes=available_bytes,
        ).risk
    recommended = recommended_tokens or recommended_context_tokens(
        available_bytes=available_bytes
    )
    if int(tokens) < int(recommended):
        return RISK_RELAXED
    if int(tokens) == int(recommended):
        return RISK_FIT
    return RISK_RISKY


def memory_payload(snapshot: MemorySnapshot | None = None) -> dict[str, float | int]:
    snapshot = snapshot or detect_memory()
    return {
        "totalBytes": snapshot.total_bytes,
        "availableBytes": snapshot.available_bytes,
        "totalGb": round(snapshot.total_gb, 1),
        "availableGb": round(snapshot.available_gb, 1),
    }


def _format_tokens(tokens: int) -> str:
    if tokens % 1000 == 0:
        return f"{tokens // 1000}K"
    if tokens % 1024 == 0:
        return f"{tokens // 1024}K"
    return str(tokens)
