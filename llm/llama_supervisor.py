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
_DEFAULT_CTX = "50000"
_DEFAULT_NP = "5"


def _flag(env_name: str, default: str) -> str:
    value = os.getenv(env_name)
    return value if value and value.strip() else default


def _common_args() -> list[str]:
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
        "-c", _flag("VERITAS_LLAMA_CTX", _DEFAULT_CTX),
        "-fa", "on",
    ]


# Default snapshot kept for importers/tests; the live spawn path (``_args``)
# calls ``_common_args()`` so env overrides take effect at runtime.
LLAMA_COMMON_ARGS = _common_args()
LLAMA_LLM_EXTRA_ARGS = ["-ctk", "q8_0", "-ctv", "q4_0"]
LLAMA_EMBEDDING_EXTRA_ARGS = ["--embeddings"]


def _expanded_path(value: str | None) -> Path | None:
    if not value or not value.strip():
        return None
    return Path(os.path.expandvars(os.path.expanduser(value.strip())))


def _windows_installer_roots() -> list[Path]:
    """Known Windows installer locations that should not depend on PATH."""
    if os.name != "nt":
        return []

    local_app_data = _expanded_path(os.getenv("LOCALAPPDATA"))
    if local_app_data is None:
        return []

    roots = [
        local_app_data / "VERITAS" / "bin",
        local_app_data / "VERITAS" / "llama.cpp" / "bin",
        local_app_data / "VERITAS" / "runtime" / "llama.cpp" / "bin",
    ]

    winget_packages = local_app_data / "Microsoft" / "WinGet" / "Packages"
    if winget_packages.exists():
        roots.extend(sorted(winget_packages.glob("ggml.llamacpp_*")))

    return roots


def llama_server_candidates(*, repo_root: Path | None = None) -> list[Path]:
    """Return executable candidates in the order VERITAS should try them."""
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    root = repo_root or Path(__file__).resolve().parents[1]

    candidates: list[Path] = []
    install_dir = _expanded_path(os.getenv("VERITAS_LLAMA_INSTALL_DIR"))
    if install_dir is not None:
        candidates.append(install_dir / exe)

    candidates.extend(
        [
            root / "bin" / exe,
            root / "llama.cpp" / "build" / "bin" / exe,
        ]
    )
    candidates.extend(installer_root / exe for installer_root in _windows_installer_roots())
    candidates.append(Path(exe))  # final fallback: rely on PATH
    return candidates


def llama_server_bin() -> Path:
    """Resolve the llama-server executable.

    Precedence:
      1. VERITAS_LLAMA_SERVER_BIN exact executable override
      2. VERITAS_LLAMA_INSTALL_DIR / bundled / known installer locations
      3. PATH fallback
    """
    env_path = _expanded_path(os.getenv("VERITAS_LLAMA_SERVER_BIN"))
    if env_path is not None:
        return env_path

    candidates = llama_server_candidates()
    for candidate in candidates[:-1]:
        if candidate.exists():
            return candidate
    return candidates[-1]


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
            *_common_args(),
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
            popen_kwargs = {
                "stdout": log,
                "stderr": subprocess.STDOUT,
                "creationflags": _creation_flags(),
            }
            exe_path = Path(args[0])
            if exe_path.is_absolute():
                popen_kwargs["cwd"] = str(exe_path.parent)
            try:
                self._proc = subprocess.Popen(args, **popen_kwargs)
            except OSError as exc:
                raise RuntimeError(
                    f"failed to start {self.kind} llama-server "
                    f"executable={args[0]!r}: {exc}"
                ) from exc
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
    "llama_server_candidates",
    "LLAMA_COMMON_ARGS",
    "LLAMA_LLM_EXTRA_ARGS",
    "LLAMA_EMBEDDING_EXTRA_ARGS",
]
