from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request

from db.db import get_app_data_dir
from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from llm.model_catalog import (
    DEFAULT_EMBEDDING_MODEL_ID,
    bytes_label,
    find_model_file,
    get_model,
    installed_llm_models,
    llm_models,
    model_root,
    selected_embedding_from_settings,
    selected_model_from_settings,
)
from llm.model_manager import available_bytes, download_model, ensure_model_dirs
from llm.model_settings import (
    launcher_initial_model_selected,
    load_settings,
    save_selected_models,
)


LLAMA_COMMON_ARGS = [
    "-ngl",
    "99",
    "-ub",
    "2048",
    "-b",
    "2048",
    "-np",
    "5",
    "--cont-batching",
    "-c",
    "90000",
]
LLAMA_LLM_EXTRA_ARGS = ["-ctk", "q8_0", "-ctv", "q4_0"]
LLAMA_EMBEDDING_EXTRA_ARGS = ["--embeddings"]
_CONSOLE_LOGS: bool | None = None


def console_logs_enabled() -> bool:
    if _CONSOLE_LOGS is not None:
        return _CONSOLE_LOGS
    if "--console-logs" in sys.argv:
        return True
    return os.getenv("VERITAS_LOG_MODE", "").strip().lower() in {
        "console",
        "stdout",
        "terminal",
    }


def configure_console_logs_from_argv() -> None:
    global _CONSOLE_LOGS
    _CONSOLE_LOGS = console_logs_enabled()
    while "--console-logs" in sys.argv:
        sys.argv.remove("--console-logs")


class DownloadWorker(QObject):
    # Qt int signals are 32-bit on Windows. GGUF downloads commonly exceed
    # 2GB, so pass Python ints as objects to avoid progress overflow.
    progress = Signal(object, object)
    status = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, llm_model_id: str, embedding_model_id: str) -> None:
        super().__init__()
        self.llm_model_id = llm_model_id
        self.embedding_model_id = embedding_model_id

    def run(self) -> None:
        try:
            specs = [
                get_model(self.llm_model_id, kind="llm"),
                get_model(self.embedding_model_id, kind="embedding"),
            ]
            for spec in specs:
                existing = find_model_file(spec)
                if existing is not None:
                    continue
                self.status.emit(f"Downloading {spec.short_name}...")

                def on_progress(done: int, total: int | None) -> None:
                    if total and total > 0:
                        self.progress.emit(done, total)
                    else:
                        self.progress.emit(0, 0)

                download_model(spec, progress=on_progress, hf_token=os.getenv("HF_TOKEN"))
            save_selected_models(
                llm_model_id=self.llm_model_id,
                embedding_model_id=self.embedding_model_id,
                mark_initial_selected=True,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the setup UI
            self.failed.emit(str(exc))
            return
        self.finished.emit()


class ModelSetupDialog(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VERITAS Model Setup")
        self.setMinimumWidth(520)
        self._settings = load_settings()
        self._selected_embedding_id = DEFAULT_EMBEDDING_MODEL_ID
        selected_llm = selected_model_from_settings(self._settings)
        selected_embedding = selected_embedding_from_settings(self._settings)
        self._selected_embedding_id = selected_embedding.id

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("Select the local GGUF model VERITAS should load.")
        title.setWordWrap(True)
        layout.addWidget(title)

        self.model_combo = QComboBox()
        for spec in llm_models():
            installed = "installed" if find_model_file(spec) else "not installed"
            self.model_combo.addItem(
                f"{spec.short_name} - {bytes_label(spec.size_bytes)} - {installed}",
                spec.id,
            )
        index = max(0, self.model_combo.findData(selected_llm.id))
        self.model_combo.setCurrentIndex(index)
        self.model_combo.currentIndexChanged.connect(self._refresh_status)
        layout.addWidget(self.model_combo)

        embedding_label = QLabel(
            f"Embedding: {selected_embedding.short_name} "
            f"({bytes_label(selected_embedding.size_bytes)})"
        )
        embedding_label.setWordWrap(True)
        layout.addWidget(embedding_label)

        self.disk_label = QLabel()
        self.disk_label.setWordWrap(True)
        layout.addWidget(self.disk_label)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.install_button = QPushButton("Install / Continue")
        self.install_button.clicked.connect(self._install_or_accept)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.install_button)
        layout.addLayout(actions)

        self._thread: QThread | None = None
        self._worker: DownloadWorker | None = None
        self._refresh_status()

    def selected_llm_id(self) -> str:
        return str(self.model_combo.currentData())

    def _required_specs(self) -> list:
        specs = [
            get_model(self.selected_llm_id(), kind="llm"),
            get_model(self._selected_embedding_id, kind="embedding"),
        ]
        return [spec for spec in specs if find_model_file(spec) is None]

    def _refresh_status(self) -> None:
        ensure_model_dirs()
        free = available_bytes(model_root())
        missing = self._required_specs()
        required = int(sum(spec.size_bytes for spec in missing) * 1.15)
        self.disk_label.setText(
            f"Model path: {model_root()} | Free: {bytes_label(free)} | "
            f"Required: {bytes_label(required)}"
        )
        if missing:
            names = ", ".join(spec.short_name for spec in missing)
            self.status_label.setText(f"Missing model files: {names}")
        else:
            self.status_label.setText("All required model files are installed.")

    def _install_or_accept(self) -> None:
        missing = self._required_specs()
        if not missing:
            save_selected_models(
                llm_model_id=self.selected_llm_id(),
                embedding_model_id=self._selected_embedding_id,
                mark_initial_selected=True,
            )
            self.accept()
            return

        required = int(sum(spec.size_bytes for spec in missing) * 1.15)
        free = available_bytes(model_root())
        if free < required:
            QMessageBox.critical(
                self,
                "Not enough disk space",
                f"Need about {bytes_label(required)}, but only {bytes_label(free)} is free.",
            )
            return

        self.install_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.progress.setValue(0)
        self._thread = QThread(self)
        self._worker = DownloadWorker(self.selected_llm_id(), self._selected_embedding_id)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.status.connect(self.status_label.setText)
        self._worker.progress.connect(self._on_progress)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.start()

    def _on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self.progress.setRange(0, 0)
            return
        self.progress.setRange(0, 100)
        self.progress.setValue(min(100, int(done * 100 / total)))
        self.status_label.setText(
            f"Downloading... {bytes_label(done)} / {bytes_label(total)}"
        )

    def _on_failed(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.install_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        QMessageBox.critical(self, "Model download failed", message)
        self._refresh_status()

    def _on_finished(self) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.accept()


def needs_model_setup() -> bool:
    ensure_model_dirs()
    settings = load_settings()
    selected_llm = selected_model_from_settings(settings)
    selected_embedding = selected_embedding_from_settings(settings)
    if find_model_file(selected_llm) is None:
        return True
    if find_model_file(selected_embedding) is None:
        return True
    if not installed_llm_models():
        return True
    return not launcher_initial_model_selected(settings)


def llama_server_bin() -> Path:
    env_path = os.getenv("VERITAS_LLAMA_SERVER_BIN")
    if env_path:
        return Path(env_path)
    exe_name = "llama-server.exe" if os.name == "nt" else "llama-server"
    candidates = [
        Path(__file__).resolve().parent / "bin" / exe_name,
        Path(__file__).resolve().parent / "llama.cpp" / "build" / "bin" / exe_name,
        Path(exe_name),
    ]
    for candidate in candidates:
        if candidate.exists() or candidate == Path(exe_name):
            return candidate
    return Path(exe_name)


def wait_http(url: str, *, timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0):
                return True
        except Exception:
            time.sleep(0.25)
    return False


def embedding_http_available(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = payload.get("data") if isinstance(payload, dict) else None
        model_id = ""
        if isinstance(models, list) and models:
            first = models[0]
            if isinstance(first, dict):
                model_id = str(first.get("id") or "")
        if not model_id:
            return False
        body = json.dumps(
            {
                "model": model_id,
                "input": "veritas embedding health check",
                "encoding_format": "float",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/embeddings",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def launcher_log_dir() -> Path:
    path = get_app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path(name: str) -> Path:
    return launcher_log_dir() / f"{name}.log"


def _tail(path: Path, *, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return text[-max_chars:].strip()


def _popen_logged(
    args: list[str],
    *,
    name: str,
    env: dict[str, str] | None = None,
    shell: bool = False,
    stream_to_console: bool = True,
) -> subprocess.Popen:
    command = args if not shell else " ".join(args)
    process_env = dict(os.environ if env is None else env)
    process_env.setdefault("PYTHONUNBUFFERED", "1")
    path = log_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"$ {' '.join(args) if isinstance(args, list) else args}\n\n", encoding="utf-8")
    if console_logs_enabled() and stream_to_console:
        print(f"[launcher][{name}] {' '.join(args)}", flush=True)
        process = subprocess.Popen(
            command,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=process_env,
            cwd=Path(__file__).resolve().parent,
            creationflags=creation_flags(),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        _start_output_stream(process, name, path)
        return process

    log = path.open("w", encoding="utf-8", errors="replace")
    log.write(f"$ {' '.join(args) if isinstance(args, list) else args}\n\n")
    log.flush()
    try:
        process = subprocess.Popen(
            command,
            shell=shell,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=process_env,
            cwd=Path(__file__).resolve().parent,
            creationflags=creation_flags(),
        )
    finally:
        log.close()
    return process


def _start_output_stream(process: subprocess.Popen, name: str, path: Path) -> None:
    def _stream() -> None:
        stream = process.stdout
        if stream is None:
            return
        with path.open("a", encoding="utf-8", errors="replace") as log:
            for line in stream:
                prefix = "" if line.startswith(("[llm]", "[api]")) else f"[{name}] "
                print(f"{prefix}{line}", end="", flush=True)
                log.write(line)
                log.flush()

    thread = threading.Thread(
        target=_stream,
        name=f"veritas-log-{name}",
        daemon=True,
    )
    thread.start()


def wait_service(
    process: subprocess.Popen | None,
    url: str,
    *,
    name: str,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    path = log_path(name)
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0):
                return
        except Exception:
            pass
        if process is not None and process.poll() is not None:
            tail = _tail(path)
            detail = f"\n\nLast log lines from {path}:\n{tail}" if tail else f"\n\nLog: {path}"
            raise RuntimeError(f"{name} exited before it became ready.{detail}")
        time.sleep(0.25)
    tail = _tail(path)
    detail = f"\n\nLast log lines from {path}:\n{tail}" if tail else f"\n\nLog: {path}"
    raise RuntimeError(f"{name} did not become ready within {int(timeout)}s.{detail}")


def creation_flags() -> int:
    if os.name != "nt":
        return 0
    if console_logs_enabled():
        return 0
    return subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]


def runtime_python() -> str:
    return os.getenv("VERITAS_PYTHON") or sys.executable


def check_python_dependencies() -> None:
    if runtime_python() == sys.executable:
        missing = [
            module
            for module in ("fastapi", "uvicorn", "openai")
            if importlib.util.find_spec(module) is None
        ]
    else:
        code = (
            "import importlib.util; "
            "mods=('fastapi','uvicorn','openai'); "
            "missing=[m for m in mods if importlib.util.find_spec(m) is None]; "
            "print(','.join(missing)); "
            "raise SystemExit(1 if missing else 0)"
        )
        result = subprocess.run(
            [runtime_python(), "-c", code],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parent,
            creationflags=creation_flags(),
        )
        missing = [
            item.strip()
            for item in (result.stdout or "").split(",")
            if item.strip()
        ]
    if not missing:
        return
    modules = ", ".join(missing)
    raise RuntimeError(
        "Python dependencies are missing for the API runtime: "
        f"{modules}\n\nRun:\npython -m pip install -r requirements.txt"
    )


def start_llama(kind: str, model_path: Path, port: int) -> subprocess.Popen | None:
    if wait_http(f"http://127.0.0.1:{port}/v1/models", timeout=0.5):
        if kind == "embedding" and not embedding_http_available(port):
            raise RuntimeError(
                f"An existing server is already listening on 127.0.0.1:{port}, "
                "but /v1/embeddings is not usable. Stop that process and restart "
                "the embedding llama-server with --embeddings."
            )
        return None

    args = [
        str(llama_server_bin()),
        "-m",
        str(model_path),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        *LLAMA_COMMON_ARGS,
    ]
    if kind == "llm":
        args.extend(LLAMA_LLM_EXTRA_ARGS)
    elif kind == "embedding":
        args.extend(LLAMA_EMBEDDING_EXTRA_ARGS)
    return _popen_logged(args, name=f"llama-{kind}", stream_to_console=False)


def start_api(api_port: int) -> subprocess.Popen | None:
    if wait_http(f"http://127.0.0.1:{api_port}/api/v1/health", timeout=0.5):
        if console_logs_enabled():
            print(
                f"[launcher][api] reusing existing API on 127.0.0.1:{api_port}; "
                "logs from that already-running process cannot be attached.",
                flush=True,
            )
        return None
    command = os.getenv("VERITAS_API_CMD")
    if command:
        return _popen_logged([command], name="api", shell=True)
    return _popen_logged(
        [runtime_python(), "-m", "api", "--api", "--port", str(api_port)],
        name="api",
    )


def start_ui(api_port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["VERITAS_API_BASE_URL"] = f"http://127.0.0.1:{api_port}"
    command = os.getenv("VERITAS_UI_CMD")
    if command:
        return _popen_logged(
            [command],
            name="ui",
            shell=True,
            env=env,
            stream_to_console=False,
        )
    return _popen_logged(
        [runtime_python(), "-m", "frontend.main"],
        env=env,
        name="ui",
        stream_to_console=False,
    )


def terminate(processes: list[subprocess.Popen | None]) -> None:
    for process in reversed(processes):
        if process is None or process.poll() is not None:
            continue
        process.terminate()
    for process in reversed(processes):
        if process is None:
            continue
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


_KILL_JOB = None  # Windows Job Object handle; kept alive for the launcher lifetime.


def _install_kill_on_close_job() -> None:
    """Tie every descendant process to this launcher's lifetime (Windows).

    Creates a Job Object with ``KILL_ON_JOB_CLOSE`` and assigns the launcher
    itself to it; child + grandchild processes (the API, the UI, and the
    llama-servers the API spawns) inherit the job. When the launcher dies —
    graceful exit, console-window close, OR Task Manager kill — the OS
    terminates the whole job tree. This is the only teardown that survives a
    hard kill (``finally`` / ``terminate`` run only on a clean exit), so it is
    what actually prevents the orphaned llama-server holding port 8080. The
    handle is stored in a module global so it is never GC'd early — closing the
    handle is what triggers the kill, and we want that to happen exactly when
    the launcher process ends.
    """
    global _KILL_JOB
    if os.name != "nt" or _KILL_JOB is not None:
        return
    try:
        import win32api
        import win32job

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        info["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, info
        )
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
        _KILL_JOB = job
    except Exception as exc:  # pragma: no cover - best effort
        print(f"[launcher][warn] kill-on-close job 설정 실패 (orphan 위험): {exc}", flush=True)


def main() -> int:
    configure_console_logs_from_argv()
    # Guarantee no orphaned llama-server/API/UI even on a hard launcher kill.
    _install_kill_on_close_job()
    app = QApplication(sys.argv)
    if needs_model_setup():
        dialog = ModelSetupDialog()
        if dialog.exec() != QDialog.Accepted:
            return 1

    settings = load_settings()
    llm_spec = selected_model_from_settings(settings)
    embedding_spec = selected_embedding_from_settings(settings)
    llm_path = find_model_file(llm_spec)
    embedding_path = find_model_file(embedding_spec)
    if llm_path is None or embedding_path is None:
        QMessageBox.critical(None, "Missing model", "Required GGUF model files are missing.")
        return 1

    llm_port = int(os.getenv("VERITAS_LLM_PORT", "8080"))
    embed_port = int(os.getenv("VERITAS_EMBED_PORT", "8081"))
    api_port = int(os.getenv("VERITAS_API_PORT", "8000"))
    os.environ["VERITAS_LLM_HOST"] = "127.0.0.1"
    os.environ["VERITAS_LLM_PORT"] = str(llm_port)
    os.environ["VERITAS_EMBED_HOST"] = "127.0.0.1"
    os.environ["VERITAS_EMBED_PORT"] = str(embed_port)
    os.environ["VERITAS_LLM_PARALLEL"] = str(settings.get("llmParallel", 1))
    # The API process now owns the llama-server lifecycle so a settings-driven
    # model switch can restart it (live model switching). The launcher just
    # flags the API to manage llama and starts the API; the API spawns + waits
    # for the llama-servers during its own startup, so the API health wait below
    # naturally covers llama bring-up (hence the longer timeout). The early
    # find_model_file checks above still give a fast, clear "missing model"
    # error before we hand off to the API.
    os.environ["VERITAS_MANAGE_LLAMA"] = "1"

    processes: list[subprocess.Popen | None] = []
    try:
        check_python_dependencies()
        api_process = start_api(api_port)
        processes.append(api_process)
        wait_service(
            api_process,
            f"http://127.0.0.1:{api_port}/api/v1/health",
            name="api",
            timeout=300.0,
        )

        ui_process = start_ui(api_port)
        processes.append(ui_process)
        return ui_process.wait()
    except Exception as exc:  # noqa: BLE001 - user-facing launcher boundary
        QMessageBox.critical(None, "VERITAS failed to launch", str(exc))
        return 1
    finally:
        terminate(processes)


if __name__ == "__main__":
    raise SystemExit(main())
