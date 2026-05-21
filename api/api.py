from __future__ import annotations

import json
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api_common import new_id
from .api_routes import (
    dashboard_router,
    document_assist_router,
    documents_router,
    draft_chat_router,
    editor_router,
    feedback_router,
    frontend_router,
    research_router,
    screen_monitoring_router,
    settings_router,
    system_router,
    verify_router,
    workspaces_router,
    write_router,
)

app = FastAPI(title="VERITAS API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    trace_id = new_id("tr")
    detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": "HTTP_ERROR",
                "message": detail,
                "traceId": trace_id,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    trace_id = new_id("tr")
    messages = [error.get("msg", "validation error") for error in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "; ".join(messages) or "validation error",
                "traceId": trace_id,
                "details": exc.errors(),
            }
        },
    )


app.include_router(system_router)
app.include_router(dashboard_router)
app.include_router(workspaces_router)
app.include_router(research_router)
app.include_router(settings_router)
app.include_router(verify_router)
app.include_router(draft_chat_router)
app.include_router(editor_router)
app.include_router(documents_router)
app.include_router(feedback_router)
app.include_router(write_router)
app.include_router(document_assist_router)
app.include_router(screen_monitoring_router)
app.include_router(frontend_router)


@app.on_event("startup")
def _eager_runtime_startup() -> None:
    """When the launcher manages the servers (``VERITAS_MANAGE_LLAMA=1``), build
    the runtime eagerly so the llama-servers are spawned during API startup —
    then ``/api/v1/health`` only responds once they are ready, and the launcher's
    health wait naturally covers llama bring-up. Without the flag, runtime
    construction stays lazy (first request), unchanged."""
    if os.getenv("VERITAS_MANAGE_LLAMA", "0") != "1":
        return
    try:
        from .services.agent_runtime import get_runtime

        get_runtime()
    except Exception as exc:  # noqa: BLE001 - logged; health check will reflect failure
        print(f"[api][startup][warn] eager runtime init failed: {exc}")


@app.on_event("shutdown")
def _shutdown_runtime() -> None:
    """Stop API-owned llama-servers on graceful uvicorn shutdown (e.g. SIGINT).

    Note: a hard Windows TerminateProcess from the launcher won't run this; the
    launcher should rely on a process group / job object for guaranteed teardown
    (follow-up). atexit in AgentRuntime covers normal interpreter exits."""
    if os.getenv("VERITAS_MANAGE_LLAMA", "0") != "1":
        return
    try:
        from .services.agent_runtime import shutdown_runtime

        shutdown_runtime()
    except Exception:
        pass
