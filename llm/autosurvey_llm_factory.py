from __future__ import annotations

import os
from typing import Any

from .openai_chat_llm import (
    DEFAULT_AUTOSURVEY_OPENAI_MODEL,
    OpenAIChatLLMClient,
)


_LOCAL_PROVIDERS = {"", "local", "llama", "llama-server", "llama_server"}


def build_autosurvey_llm(default_llm: Any):
    """Return the LLM client used by AutoSurvey generation tools.

    The default remains the local llama-server client. Setting
    ``VERITAS_AUTOSURVEY_LLM_PROVIDER=openai`` swaps only the AutoSurvey
    research-generation role to OpenAI; embeddings, chat, screen, and verify
    keep using the caller-provided local client unless wired otherwise.
    """

    persisted_settings = _load_persisted_settings()
    stored_settings = _autosurvey_openai_settings(persisted_settings)
    provider = (
        os.getenv("VERITAS_AUTOSURVEY_LLM_PROVIDER")
        or str(stored_settings.get("provider") or "")
        or "local"
    ).strip().lower()
    if provider in _LOCAL_PROVIDERS:
        return default_llm
    if provider != "openai":
        raise RuntimeError(
            "Unsupported VERITAS_AUTOSURVEY_LLM_PROVIDER="
            f"{provider!r}. Expected 'local' or 'openai'."
        )

    api_key = (
        os.getenv("OPENAI_API_KEY")
        or str(stored_settings.get("apiKey") or "")
    ).strip()
    if not api_key:
        raise RuntimeError(
            "VERITAS_AUTOSURVEY_LLM_PROVIDER=openai requires OPENAI_API_KEY "
            "or a saved OpenAI API key in Settings."
        )

    model = os.getenv(
        "VERITAS_AUTOSURVEY_OPENAI_MODEL",
        DEFAULT_AUTOSURVEY_OPENAI_MODEL,
    ).strip()
    if not model:
        model = DEFAULT_AUTOSURVEY_OPENAI_MODEL
    n_ctx = _env_int(
        "VERITAS_AUTOSURVEY_OPENAI_N_CTX",
        _default_n_ctx_for_model(model),
        min_value=1,
    )
    max_parallel = _env_int(
        "VERITAS_AUTOSURVEY_OPENAI_MAX_PARALLEL",
        _persisted_llm_parallel(persisted_settings),
        min_value=1,
    )
    stream_summary = _env_bool(
        "VERITAS_AUTOSURVEY_OPENAI_STREAM_SUMMARY",
        bool(getattr(default_llm, "stream_summary", False)),
    )
    service_tier = os.getenv("VERITAS_AUTOSURVEY_OPENAI_SERVICE_TIER", "").strip()
    trace_latency = os.getenv("VERITAS_TRACE_LATENCY", "1") != "0"

    print(
        "[autosurvey][llm] provider=openai "
        f"model={model} n_ctx={n_ctx} max_parallel={max_parallel} "
        f"service_tier={service_tier or 'auto'}"
    )
    return OpenAIChatLLMClient(
        api_key=api_key,
        model=model,
        n_ctx=n_ctx,
        max_parallel=max_parallel,
        service_tier=service_tier,
        trace_latency=trace_latency,
        stream_summary=stream_summary,
    )


def _default_n_ctx_for_model(model: str) -> int:
    normalized = str(model or "").strip().lower()
    if normalized == "gpt-5.5":
        return 1_050_000
    return 400_000


def _env_int(key: str, default: int, *, min_value: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return int(default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return int(default)
    return max(min_value, value)


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _load_persisted_settings() -> dict[str, Any]:
    try:
        from db.app_state import read_json

        settings = read_json("settings", {})
    except Exception:
        return {}
    return settings if isinstance(settings, dict) else {}


def _autosurvey_openai_settings(settings: dict[str, Any]) -> dict[str, Any]:
    autosurvey_openai = settings.get("autosurveyOpenAI")
    return autosurvey_openai if isinstance(autosurvey_openai, dict) else {}


def _persisted_llm_parallel(settings: dict[str, Any]) -> int:
    try:
        return max(1, min(5, int(settings.get("llmParallel", 1))))
    except (TypeError, ValueError):
        return 1


__all__ = ["build_autosurvey_llm"]
