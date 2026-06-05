"""llama-server process supervision — single owner of one child process.

Owns the lifecycle of ONE ``llama-server`` (LLM or embedding): build args from a
GGUF path, spawn the child, health-check it, stop it, restart it with a
different model. The **API process** holds these supervisors so a
settings-driven model switch can restart the server with a new GGUF; the rest
of the app then just calls :meth:`LLMClient.refresh_model_info` to re-detect the
model — no other rewiring.

Adopt-or-spawn: if a healthy server is already listening on the port (e.g. one
started by hand during development), the supervisor *adopts* it — it does not
spawn a duplicate, and it refuses to restart it for a live switch (it is not
ours to kill). In the packaged flow the launcher delegates server startup to
the API process, so the supervisor always *owns* (spawned) the server and live
switching works.

Responsibility boundary: this module knows only about *processes and ports*. It
has no knowledge of settings, the model catalog, or the runtime — callers pass
in a resolved GGUF path. The llama-server flag profile lives here as the single
source of truth so the packaged first-boot (``launcher.py``) and the
API-managed restart use identical flags.
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from pathlib import Path


# llama-server flag profile. The hardware-adaptable knobs (-ngl / -c / -np) are
# read from env at spawn time so a machine without an NVIDIA GPU or with little
# memory can recover without a code change.
_DEFAULT_NGL = "99"
_DEFAULT_CTX = "32768"
_DEFAULT_NP = "5"


def _flag(env_name: str, default: str) -> str:
    value = os.getenv(env_name)
    return value if value and value.strip() else default


def _context_flag(kind: str = "llm") -> str:
    env_value = os.getenv("VERITAS_LLAMA_CTX")
    if env_value and env_value.strip():
        return env_value.strip()
    try:
        from llm.context_settings import effective_context_tokens
        from llm.model_settings import load_settings
        from llm.model_catalog import (
            selected_embedding_from_settings,
            selected_model_from_settings,
        )

        settings = load_settings()
        model = (
            selected_embedding_from_settings(settings)
            if kind == "embedding"
            else selected_model_from_settings(settings)
        )
        return str(
            effective_context_tokens(
                settings,
                model_limit=getattr(model, "context_tokens", None),
            )
        )
    except Exception:
        return _DEFAULT_CTX


def _common_args(kind: str = "llm") -> list[str]:
    """Common llama-server flags, with per-machine overrides read from env:

      VERITAS_LLAMA_NGL → -ngl  GPU layers to offload (0 = CPU-only)  [99]
      VERITAS_LLAMA_CTX → -c    total context window                 [50000]
      VERITAS_LLAMA_NP  → -np   parallel decode slots                [1]

    ``-ngl 99`` (all GPU layers) + a 50k context are fine defaults on a GPU
    box, but can make llama-server *exit on load* on a machine with no NVIDIA
    GPU / little memory. The env overrides let such a machine fall back (e.g.
    ``VERITAS_LLAMA_NGL=0`` for CPU-only, ``VERITAS_LLAMA_CTX=8192`` for low
    memory) without touching code. Read at spawn time so they apply per launch.
    """
    return [
        "-ngl", _flag("VERITAS_LLAMA_NGL", _DEFAULT_NGL),
        "-ub", "1024",
        "-b", "1024",
        "-np", _flag("VERITAS_LLAMA_NP", _DEFAULT_NP),
        "--cont-batching",
        "-c", _context_flag(kind),
        "-fa", "on",
    ]


# Default snapshot kept for importers/tests; the live spawn path (``_args``)
# calls ``_common_args()`` so env overrides take effect at runtime.
LLAMA_COMMON_ARGS = _common_args()
LLAMA_LLM_EXTRA_ARGS = ["-ctk", "q8_0", "-ctv", "q4_0"]
LLAMA_EMBEDDING_EXTRA_ARGS = ["--embeddings"]


def llama_server_bin() -> Path:
    """Resolve the llama-server executable (env override → bundled → PATH)."""
    env_path = os.getenv("VERITAS_LLAMA_SERVER_BIN")
    if env_path:
        return Path(env_path)
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    repo_root = Path(__file__).resolve().parents[1]
    for candidate in (
        repo_root / "bin" / exe,
        repo_root / "llama.cpp" / "build" / "bin" / exe,
    ):
        if candidate.exists():
            return candidate
    return Path(exe)  # rely on PATH


def _http_ok(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except Exception:
        return False


def _log_dir() -> Path:
    try:
        from db.db import get_app_data_dir

        path = get_app_data_dir() / "logs"
    except Exception:
        path = Path.home() / ".veritas" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _creation_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


class LlamaServer:
    """Supervises one ``llama-server`` child (``kind`` = ``"llm"`` | ``"embedding"``)."""

    def __init__(self, kind: str, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.kind = kind
        self.host = host
        self.port = port
        self._proc: subprocess.Popen | None = None
        self._owned = False  # True only while WE own a running child

    @property
    def models_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1/models"

    def is_healthy(self, timeout: float = 1.0) -> bool:
        return _http_ok(self.models_url, timeout)

    @property
    def owned(self) -> bool:
        return (
            self._owned
            and self._proc is not None
            and self._proc.poll() is None
        )

    def ensure_started(self, model_path: Path, *, timeout: float = 180.0) -> bool:
        """Adopt an already-healthy server, else spawn one.

        Returns True if WE spawned (own) it, False if an existing server was
        adopted. Never spawns a duplicate on an occupied port.
        """
        if self.is_healthy(0.5):
            self._owned = False
            return False
        self._spawn(model_path, timeout=timeout)
        return True

    def restart(self, model_path: Path, *, timeout: float = 180.0) -> None:
        """Restart the server with ``model_path``. Requires that we own it."""
        if not self.owned and self.is_healthy(0.5):
            raise RuntimeError(
                f"{self.kind} llama-server on :{self.port} is externally managed "
                "and cannot be restarted for a live model switch. Launch VERITAS "
                "through the launcher so the app owns the server process."
            )
        self.stop()
        self._spawn(model_path, timeout=timeout)

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        self._owned = False
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # -- internals -----------------------------------------------------------

    def _args(self, model_path: Path) -> list[str]:
        args = [
            str(llama_server_bin()),
            "-m", str(model_path),
            "--host", self.host,
            "--port", str(self.port),
            *_common_args(self.kind),
        ]
        args += LLAMA_LLM_EXTRA_ARGS if self.kind == "llm" else LLAMA_EMBEDDING_EXTRA_ARGS
        return args

    def _spawn(self, model_path: Path, *, timeout: float) -> None:
        args = self._args(Path(model_path))
        log = (_log_dir() / f"llama-{self.kind}.log").open(
            "w", encoding="utf-8", errors="replace"
        )
        try:
            log.write("$ " + " ".join(args) + "\n\n")
            log.flush()
            self._proc = subprocess.Popen(
                args,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=_creation_flags(),
            )
        finally:
            log.close()  # child keeps its own inherited handle
        self._owned = True

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_healthy(1.0):
                return
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"{self.kind} llama-server exited before becoming ready "
                    f"(see {_log_dir() / f'llama-{self.kind}.log'})"
                )
            time.sleep(0.3)
        raise RuntimeError(
            f"{self.kind} llama-server did not become ready within {int(timeout)}s"
        )


__all__ = [
    "LlamaServer",
    "llama_server_bin",
    "LLAMA_COMMON_ARGS",
    "LLAMA_LLM_EXTRA_ARGS",
    "LLAMA_EMBEDDING_EXTRA_ARGS",
]
