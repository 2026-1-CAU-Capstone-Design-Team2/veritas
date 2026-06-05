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
# read from env/settings at spawn time. Default GPU policy is "try fast first":
# use full GPU offload, then retry with smaller offload counts if the server
# exits before becoming healthy. This restores local speed for models that fit
# in VRAM while still falling back for oversized combinations.
_DEFAULT_CTX = "32768"
_DEFAULT_NP = "1"
_DEFAULT_NGL_RETRIES = ("99", "32", "16", "8", "0")


def _flag(env_name: str, default: str) -> str:
    value = os.getenv(env_name)
    return value if value and value.strip() else default


def _ngl_args(value: str | None = None) -> list[str]:
    resolved = value if value is not None else _ngl_retry_values()[0]
    if resolved and resolved.strip():
        return ["-ngl", resolved.strip()]
    return []


def _ngl_retry_values() -> tuple[str, ...]:
    explicit = os.getenv("VERITAS_LLAMA_NGL")
    if explicit and explicit.strip():
        return (explicit.strip(),)

    retries = os.getenv("VERITAS_LLAMA_NGL_RETRIES")
    if retries and retries.strip():
        values = tuple(part.strip() for part in retries.split(",") if part.strip())
        if values:
            return values

    mode = os.getenv("VERITAS_LLAMA_GPU_MODE", "auto").strip().lower()
    if mode in {"0", "off", "cpu", "none", "false"}:
        return ("0",)
    if mode in {"full", "max", "gpu"}:
        return ("99",)
    return _DEFAULT_NGL_RETRIES


def _np_flag(kind: str = "llm") -> str:
    try:
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
        return str(_parallel_slots_for_model(model, settings))
    except Exception:
        return str(_requested_parallel_slots())


def _requested_np_flag() -> str:
    return str(_requested_parallel_slots())


def _requested_parallel_slots() -> int:
    env_value = os.getenv("VERITAS_LLAMA_NP") or os.getenv("VERITAS_LLM_PARALLEL")
    if env_value and env_value.strip():
        try:
            return max(1, min(5, int(env_value)))
        except ValueError:
            return int(_DEFAULT_NP)
    try:
        from llm.model_settings import load_settings

        settings = load_settings()
        return max(1, min(5, int(settings.get("llmParallel", _DEFAULT_NP))))
    except Exception:
        return int(_DEFAULT_NP)


def _parallel_slots() -> int:
    try:
        return max(1, min(5, int(_requested_np_flag())))
    except ValueError:
        return 1


def _parallel_slots_for_model(model, settings: dict) -> int:
    requested = _requested_parallel_slots()
    try:
        from llm.context_settings import effective_context_tokens
        from llm.hardware_policy import max_parallel_slots

        per_slot = effective_context_tokens(
            settings,
            model_limit=getattr(model, "context_tokens", None),
            model=model,
            parallel_slots=1,
        )
        return max(1, min(requested, max_parallel_slots(model, context_per_slot_tokens=per_slot)))
    except Exception:
        return requested


def _settings_and_model(kind: str):
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
    return settings, model


def _context_plan(kind: str = "llm") -> tuple[int, int, int]:
    """Return ``(parallel_slots, per_slot_context, total_context)``.

    llama-server's ``-c`` flag is the total context pool shared by ``-np``
    slots. Prompt budgeting elsewhere needs the per-request/per-slot value.
    """
    from llm.context_settings import effective_context_tokens

    settings, model = _settings_and_model(kind)
    parallel_slots = _parallel_slots_for_model(model, settings)
    per_slot = effective_context_tokens(
        settings,
        model_limit=getattr(model, "context_tokens", None),
        model=model,
        parallel_slots=parallel_slots,
    )
    per_slot = max(1, int(per_slot))
    total = per_slot * parallel_slots
    model_limit = getattr(model, "context_tokens", None)
    if model_limit and model_limit > 0:
        total = min(total, int(model_limit))
        per_slot = max(1, total // parallel_slots)
    return parallel_slots, per_slot, total


def effective_context_per_slot(kind: str = "llm") -> int:
    """Return the context window available to one llama-server request."""
    env_value = os.getenv("VERITAS_LLAMA_CTX")
    if env_value and env_value.strip():
        try:
            total = max(1, int(env_value.strip()))
            slots = max(1, int(_np_flag(kind)))
            return max(1, total // slots)
        except (TypeError, ValueError):
            pass
    try:
        return _context_plan(kind)[1]
    except Exception:
        try:
            return max(1, int(_DEFAULT_CTX) // max(1, _parallel_slots()))
        except Exception:
            return int(_DEFAULT_CTX)


def _context_flag(kind: str = "llm") -> str:
    env_value = os.getenv("VERITAS_LLAMA_CTX")
    if env_value and env_value.strip():
        return env_value.strip()
    try:
        _, _, total = _context_plan(kind)
        return str(total)
    except Exception:
        return _DEFAULT_CTX


def _common_args(kind: str = "llm", *, ngl: str | None = None) -> list[str]:
    """Common llama-server flags, with per-machine overrides read from env:

      VERITAS_LLAMA_NGL → -ngl  exact GPU layers override            [auto]
      VERITAS_LLAMA_CTX → -c    total context window                 [50000]
      VERITAS_LLAMA_NP  → -np   parallel decode slots                [1]

    With no explicit override we start with ``-ngl 99`` for speed; the spawn
    path retries lower values if that does not become healthy. Read at spawn
    time so overrides apply per launch.
    """
    return [
        *_ngl_args(ngl),
        "-ub", "1024",
        "-b", "1024",
        "-np", _np_flag(kind),
        "--cont-batching",
        "-c", _context_flag(kind),
        "-fa", "on",
    ]


# Default snapshot kept for importers/tests; the live spawn path (``_args``)
# calls ``_common_args()`` so env overrides take effect at runtime.
LLAMA_COMMON_ARGS = _common_args()
LLAMA_LLM_EXTRA_ARGS = ["-ctk", "q8_0", "-ctv", "q4_0"]
LLAMA_EMBEDDING_EXTRA_ARGS = ["--embeddings"]
GRANITE_EMBEDDING_TOKENIZER_OVERRIDE = [
    "--override-kv",
    "tokenizer.ggml.pre=str:gpt-2",
]


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


def _needs_granite_embedding_tokenizer_override(model_path: Path) -> bool:
    normalized = str(model_path).replace("\\", "/").lower()
    return "granite-embedding" in normalized


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

    def _args(self, model_path: Path, *, ngl: str | None = None) -> list[str]:
        model_path = Path(model_path)
        args = [
            str(llama_server_bin()),
            "-m", str(model_path),
            "--host", self.host,
            "--port", str(self.port),
            *_common_args(self.kind, ngl=ngl),
        ]
        if self.kind == "llm":
            args += LLAMA_LLM_EXTRA_ARGS
        else:
            args += LLAMA_EMBEDDING_EXTRA_ARGS
            if _needs_granite_embedding_tokenizer_override(model_path):
                args += GRANITE_EMBEDDING_TOKENIZER_OVERRIDE
        return args

    def _spawn(self, model_path: Path, *, timeout: float) -> None:
        log_path = _log_dir() / f"llama-{self.kind}.log"
        errors: list[str] = []
        attempts = _ngl_retry_values()
        for index, ngl in enumerate(attempts):
            try:
                self._spawn_once(
                    Path(model_path),
                    timeout=timeout,
                    ngl=ngl,
                    log_path=log_path,
                    append=index > 0,
                )
                return
            except RuntimeError as exc:
                errors.append(f"-ngl {ngl}: {exc}")
                self.stop()
                if index < len(attempts) - 1:
                    continue
                joined = "\n".join(errors)
                raise RuntimeError(
                    f"{self.kind} llama-server failed after GPU offload retries:\n"
                    f"{joined}\nsee {log_path}"
                ) from exc

    def _spawn_once(
        self,
        model_path: Path,
        *,
        timeout: float,
        ngl: str | None,
        log_path: Path,
        append: bool,
    ) -> None:
        args = self._args(Path(model_path), ngl=ngl)
        log = log_path.open(
            "a" if append else "w", encoding="utf-8", errors="replace"
        )
        try:
            if append:
                log.write("\n\n--- retry with lower GPU offload ---\n")
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
                    f"(see {log_path})"
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
