from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model_catalog import ModelSpec, find_model_file


MAX_PARALLEL_SLOTS = 5
RISK_RELAXED = "여유"
RISK_FIT = "적합"
RISK_RISKY = "위험"

_GIB = 1024**3
_MIB = 1024**2


@dataclass(frozen=True)
class MemorySnapshot:
    total_bytes: int
    available_bytes: int

    @property
    def total_gb(self) -> float:
        return self.total_bytes / _GIB if self.total_bytes > 0 else 0.0

    @property
    def available_gb(self) -> float:
        return self.available_bytes / _GIB if self.available_bytes > 0 else 0.0


@dataclass(frozen=True)
class RuntimeEstimate:
    model_id: str
    model_name: str
    context_per_slot_tokens: int
    parallel_slots: int
    requested_total_context_tokens: int
    total_context_tokens: int
    model_context_limit_tokens: int | None
    context_clamped: bool
    model_weight_bytes: int
    kv_cache_bytes: int
    runtime_buffer_bytes: int
    estimated_bytes: int
    total_ram_bytes: int
    available_ram_bytes: int
    usable_ram_bytes: int
    risk_ratio: float
    risk: str
    max_parallel_slots: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "modelId": self.model_id,
            "modelName": self.model_name,
            "contextPerSlotTokens": self.context_per_slot_tokens,
            "parallelSlots": self.parallel_slots,
            "requestedTotalContextTokens": self.requested_total_context_tokens,
            "totalContextTokens": self.total_context_tokens,
            "modelContextLimitTokens": self.model_context_limit_tokens,
            "contextClamped": self.context_clamped,
            "modelWeightBytes": self.model_weight_bytes,
            "kvCacheBytes": self.kv_cache_bytes,
            "runtimeBufferBytes": self.runtime_buffer_bytes,
            "estimatedBytes": self.estimated_bytes,
            "estimatedGb": round(self.estimated_bytes / _GIB, 2),
            "totalRamBytes": self.total_ram_bytes,
            "availableRamBytes": self.available_ram_bytes,
            "usableRamBytes": self.usable_ram_bytes,
            "availableRamGb": round(self.available_ram_bytes / _GIB, 1)
            if self.available_ram_bytes > 0
            else 0.0,
            "riskRatio": round(self.risk_ratio, 3),
            "risk": self.risk,
            "maxParallelSlots": self.max_parallel_slots,
        }


def detect_memory() -> MemorySnapshot:
    if os.name == "nt":
        return _windows_memory()
    return _posix_memory()


def usable_memory_bytes(available_bytes: int) -> int:
    if available_bytes <= 0:
        return 0
    reserve = max(1 * _GIB, int(available_bytes * 0.15))
    reserve = min(reserve, 4 * _GIB)
    return max(0, int(available_bytes) - reserve)


def model_weight_bytes(
    model: ModelSpec,
    *,
    prefer_installed_file: bool = True,
) -> int:
    if prefer_installed_file:
        path = find_model_file(model)
        if path is not None:
            try:
                return int(Path(path).stat().st_size)
            except OSError:
                pass
    if model.size_bytes > 0:
        return int(model.size_bytes)
    if model.parameter_size_b and model.estimated_bits_per_weight:
        raw = model.parameter_size_b * 1_000_000_000 * (
            model.estimated_bits_per_weight / 8.0
        )
        return int(raw * 1.12)
    return 0


def kv_cache_bytes_per_token(model: ModelSpec) -> int:
    if model.kv_bytes_per_token and model.kv_bytes_per_token > 0:
        return int(model.kv_bytes_per_token)
    params = float(model.parameter_size_b or 0.0)
    if params <= 1.0:
        return 16 * 1024
    if params <= 2.0:
        return 24 * 1024
    if params <= 4.0:
        return 48 * 1024
    if params <= 9.0:
        return 72 * 1024
    if params <= 27.0:
        return 128 * 1024
    return 160 * 1024


def kv_cache_bytes(model: ModelSpec, total_context_tokens: int) -> int:
    return max(0, int(total_context_tokens)) * kv_cache_bytes_per_token(model)


def runtime_buffer_bytes(model: ModelSpec, weight_bytes: int | None = None) -> int:
    weight = int(weight_bytes if weight_bytes is not None else model_weight_bytes(model))
    model_scaled = max(256 * _MIB, int(weight * 0.08))
    return int(768 * _MIB + min(model_scaled, 4 * _GIB))


def classify_risk(estimated_bytes: int, available_bytes: int) -> tuple[str, float, int]:
    usable = usable_memory_bytes(available_bytes)
    if usable <= 0:
        return RISK_RISKY, 999.0, usable
    ratio = max(0.0, float(estimated_bytes) / float(usable))
    if ratio <= 0.72:
        return RISK_RELAXED, ratio, usable
    if ratio <= 1.0:
        return RISK_FIT, ratio, usable
    return RISK_RISKY, ratio, usable


def clamp_parallel_slots(value: int | None, *, hard_limit: int = MAX_PARALLEL_SLOTS) -> int:
    try:
        slots = int(value if value is not None else 1)
    except (TypeError, ValueError):
        slots = 1
    return max(1, min(max(1, int(hard_limit)), slots))


def _memory_snapshot(available_bytes: int | None = None) -> MemorySnapshot:
    if available_bytes is None:
        return detect_memory()
    return MemorySnapshot(total_bytes=0, available_bytes=max(0, int(available_bytes)))


def _context_totals(
    model: ModelSpec,
    *,
    context_per_slot_tokens: int,
    parallel_slots: int,
) -> tuple[int, int, bool]:
    per_slot = max(1, int(context_per_slot_tokens))
    slots = clamp_parallel_slots(parallel_slots)
    requested_total = per_slot * slots
    total = requested_total
    limit = model.context_tokens
    if limit and limit > 0:
        total = min(total, int(limit))
    return requested_total, total, total != requested_total


def _estimated_bytes_for(
    model: ModelSpec,
    *,
    context_per_slot_tokens: int,
    parallel_slots: int,
    prefer_installed_file: bool = True,
) -> tuple[int, int, int, int, bool, int]:
    requested_total, total_context, clamped = _context_totals(
        model,
        context_per_slot_tokens=context_per_slot_tokens,
        parallel_slots=parallel_slots,
    )
    weight = model_weight_bytes(model, prefer_installed_file=prefer_installed_file)
    kv = kv_cache_bytes(model, total_context)
    buffer = runtime_buffer_bytes(model, weight)
    return weight + kv + buffer, weight, kv, buffer, clamped, requested_total


def max_parallel_slots(
    model: ModelSpec,
    *,
    context_per_slot_tokens: int,
    available_bytes: int | None = None,
    hard_limit: int = MAX_PARALLEL_SLOTS,
    prefer_installed_file: bool = True,
) -> int:
    snapshot = _memory_snapshot(available_bytes)
    maximum = 1
    for slots in range(1, clamp_parallel_slots(hard_limit) + 1):
        if model.context_tokens and context_per_slot_tokens * slots > model.context_tokens:
            break
        estimated, *_ = _estimated_bytes_for(
            model,
            context_per_slot_tokens=context_per_slot_tokens,
            parallel_slots=slots,
            prefer_installed_file=prefer_installed_file,
        )
        risk, _, _ = classify_risk(estimated, snapshot.available_bytes)
        if risk == RISK_RISKY:
            break
        maximum = slots
    return maximum


def model_fit_context_tokens(model: ModelSpec, *, app_limit: int | None = None) -> int:
    """Largest automatic context tier that fits the model size class."""
    params = float(model.parameter_size_b or 0.0)
    if params <= 9.0:
        target = 8192
    elif params <= 27.0:
        target = 16384
    else:
        target = 32768

    limit = app_limit if app_limit and app_limit > 0 else target
    if model.context_tokens and model.context_tokens > 0:
        limit = min(limit, int(model.context_tokens))
    return max(1, min(target, int(limit)))


def estimate_runtime(
    model: ModelSpec,
    *,
    context_per_slot_tokens: int,
    parallel_slots: int = 1,
    available_bytes: int | None = None,
    hard_parallel_limit: int = MAX_PARALLEL_SLOTS,
    prefer_installed_file: bool = True,
) -> RuntimeEstimate:
    slots = clamp_parallel_slots(parallel_slots, hard_limit=hard_parallel_limit)
    snapshot = _memory_snapshot(available_bytes)
    estimated, weight, kv, buffer, clamped, requested_total = _estimated_bytes_for(
        model,
        context_per_slot_tokens=context_per_slot_tokens,
        parallel_slots=slots,
        prefer_installed_file=prefer_installed_file,
    )
    _, total_context, _ = _context_totals(
        model,
        context_per_slot_tokens=context_per_slot_tokens,
        parallel_slots=slots,
    )
    risk, ratio, usable = classify_risk(estimated, snapshot.available_bytes)
    maximum = max_parallel_slots(
        model,
        context_per_slot_tokens=context_per_slot_tokens,
        available_bytes=snapshot.available_bytes,
        hard_limit=hard_parallel_limit,
        prefer_installed_file=prefer_installed_file,
    )
    return RuntimeEstimate(
        model_id=model.id,
        model_name=model.name,
        context_per_slot_tokens=max(1, int(context_per_slot_tokens)),
        parallel_slots=slots,
        requested_total_context_tokens=requested_total,
        total_context_tokens=total_context,
        model_context_limit_tokens=model.context_tokens,
        context_clamped=clamped,
        model_weight_bytes=weight,
        kv_cache_bytes=kv,
        runtime_buffer_bytes=buffer,
        estimated_bytes=estimated,
        total_ram_bytes=snapshot.total_bytes,
        available_ram_bytes=snapshot.available_bytes,
        usable_ram_bytes=usable,
        risk_ratio=ratio,
        risk=risk,
        max_parallel_slots=maximum,
    )


def recommended_context_tokens(
    model: ModelSpec,
    *,
    context_tiers: tuple[int, ...],
    available_bytes: int | None = None,
    parallel_slots: int = 1,
    model_limit: int | None = None,
    app_limit: int | None = None,
    prefer_installed_file: bool = True,
) -> int:
    limit = app_limit if app_limit and app_limit > 0 else max(context_tiers)
    if model_limit and model_limit > 0:
        limit = min(limit, int(model_limit))
    if model.context_tokens and model.context_tokens > 0:
        limit = min(limit, int(model.context_tokens))
    limit = min(limit, model_fit_context_tokens(model, app_limit=app_limit))
    target_parallel_slots = max(
        clamp_parallel_slots(parallel_slots),
        MAX_PARALLEL_SLOTS,
    )
    if model.context_tokens and model.context_tokens > 0:
        per_slot_parallel_limit = int(model.context_tokens) // target_parallel_slots
        if per_slot_parallel_limit > 0:
            limit = min(limit, per_slot_parallel_limit)
    candidates = sorted({tokens for tokens in context_tiers if tokens <= limit})
    if not candidates:
        return max(1, min(context_tiers[0], limit))

    snapshot = _memory_snapshot(available_bytes)
    for tokens in reversed(candidates):
        estimate = estimate_runtime(
            model,
            context_per_slot_tokens=tokens,
            parallel_slots=target_parallel_slots,
            available_bytes=snapshot.available_bytes,
            prefer_installed_file=prefer_installed_file,
        )
        if estimate.risk != RISK_RISKY:
            return tokens
    return candidates[0]


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
