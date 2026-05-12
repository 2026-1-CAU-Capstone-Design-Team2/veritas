from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/v1/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "be"}


@router.get("/")
async def root() -> dict[str, str]:
    return {"service": "veritas", "status": "running", "docs": "/docs"}
