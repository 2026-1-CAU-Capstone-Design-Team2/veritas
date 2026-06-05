from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from typing import Any


CONTEXT_TIERS = (8192, 16384, 32768, 50000, 90000)
DEFAULT_CONTEXT_MODE = "auto"
DEFAULT_CONTEXT_TOKENS = 32768
APP_MAX_CONTEXT_TOKENS = 90000


@dataclass(frozen=True)
class MemorySnapshot:
    total_bytes: int
    available_bytes: int

    @property
    def total_gb(self) -> float:
        return self.total_bytes / 1024**3 if self.total_bytes > 0 else 0.0

    @property
    def available_gb(self) -> float:
        return self.available_bytes / 1024**3 if self.available_bytes > 0 else 0.0


@dataclass(frozen=True)
class ContextOption:
    tokens: int
    label: str
    risk: str
    recommended: bool = False


def detect_memory() -> MemorySnapshot:
    if os.name == "nt":
        return _windows_memory()
    return _posix_memory()


def recommended_context_tokens(
    *,
    available_bytes: int | None = None,
    model_limit: int | None = None,
) -> int:
    if available_bytes is None:
        available_bytes = detect_memory().available_bytes
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
) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    mode = str(payload.get("mode") or DEFAULT_CONTEXT_MODE).strip().lower()
    if mode not in {"auto", "manual"}:
        mode = DEFAULT_CONTEXT_MODE
    try:
        tokens = int(payload.get("tokens") or DEFAULT_CONTEXT_TOKENS)
    except (TypeError, ValueError):
        tokens = DEFAULT_CONTEXT_TOKENS
    auto_tokens = recommended_context_tokens(model_limit=model_limit)
    if mode == "auto":
        tokens = auto_tokens
    else:
        tokens = clamp_context_tokens(tokens, model_limit=model_limit)
    return {
        "mode": mode,
        "tokens": tokens,
        "lastAutoTokens": auto_tokens,
        "memory": memory_payload(),
    }


def effective_context_tokens(
    settings: dict[str, Any] | None,
    *,
    model_limit: int | None = None,
) -> int:
    env_value = os.getenv("VERITAS_LLAMA_CTX")
    if env_value and env_value.strip():
        try:
            return clamp_context_tokens(int(env_value), model_limit=model_limit)
        except ValueError:
            pass
    llama_context = settings.get("llamaContext") if isinstance(settings, dict) else None
    normalized = normalize_context_settings(llama_context, model_limit=model_limit)
    return int(normalized["tokens"])


def context_options(*, model_limit: int | None = None) -> list[ContextOption]:
    auto_tokens = recommended_context_tokens(model_limit=model_limit)
    options: list[ContextOption] = []
    for tokens in CONTEXT_TIERS:
        clamped = clamp_context_tokens(tokens, model_limit=model_limit)
        if clamped != tokens:
            continue
        options.append(
            ContextOption(
                tokens=tokens,
                label=f"{_format_tokens(tokens)} tokens",
                risk=context_risk(tokens, auto_tokens),
                recommended=tokens == auto_tokens,
            )
        )
    return options


def context_risk(tokens: int, recommended_tokens: int | None = None) -> str:
    recommended = recommended_tokens or recommended_context_tokens()
    if int(tokens) < int(recommended):
        return "여유"
    if int(tokens) == int(recommended):
        return "적합"
    return "위험"


def memory_payload() -> dict[str, float | int]:
    snapshot = detect_memory()
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


def _windows_memory() -> MemorySnapshot:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        return MemorySnapshot(0, 0)
    return MemorySnapshot(int(status.ullTotalPhys), int(status.ullAvailPhys))


def _posix_memory() -> MemorySnapshot:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        return MemorySnapshot(page_size * pages, page_size * available_pages)
    except (AttributeError, OSError, ValueError):
        return MemorySnapshot(0, 0)
