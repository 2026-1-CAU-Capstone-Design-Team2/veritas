from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import fnmatch
from pathlib import Path
import re
import shutil

import httpx

from .model_catalog import ModelSpec, find_model_file, kind_dir, model_dir


ProgressCallback = Callable[[int, int | None], None]


@dataclass(frozen=True)
class LocalModelStatus:
    spec: ModelSpec
    installed: bool
    path: Path | None
    size_bytes: int


def local_status(spec: ModelSpec) -> LocalModelStatus:
    path = find_model_file(spec)
    size = path.stat().st_size if path and path.exists() else 0
    return LocalModelStatus(
        spec=spec,
        installed=path is not None,
        path=path,
        size_bytes=size,
    )


def available_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


_SPLIT_GGUF_RE = re.compile(r"^(.*-)(\d{5})-of-(\d{5})(\.gguf)$")


def resolve_hf_filenames(spec: ModelSpec, *, timeout: float = 30.0) -> list[str]:
    api_url = f"https://huggingface.co/api/models/{spec.repo_id}"
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(api_url)
        response.raise_for_status()
        payload = response.json()

    siblings = payload.get("siblings") or []
    filenames: list[str] = []
    for item in siblings:
        if isinstance(item, dict):
            name = str(item.get("rfilename") or "")
            if name:
                filenames.append(name)

    if spec.filename and spec.filename in filenames:
        return [spec.filename]

    matches = [
        name for name in filenames
        if fnmatch.fnmatch(name, spec.filename_glob)
    ]
    if matches:
        # Prefer a file at the repo root over split files or nested artifacts.
        matches.sort(key=lambda value: ("/" in value, len(value), value))
        return _expand_split_gguf(matches[0], matches)

    expected = spec.filename or spec.filename_glob
    raise RuntimeError(f"No GGUF file matching {expected!r} in {spec.repo_id}")


def resolve_hf_filename(spec: ModelSpec, *, timeout: float = 30.0) -> str:
    return resolve_hf_filenames(spec, timeout=timeout)[0]


def _expand_split_gguf(first: str, matches: list[str]) -> list[str]:
    match = _SPLIT_GGUF_RE.match(first)
    if match is None:
        return [first]
    prefix, _index, count_text, suffix = match.groups()
    count = int(count_text)
    available = set(matches)
    expected = [
        f"{prefix}{index:05d}-of-{count:05d}{suffix}"
        for index in range(1, count + 1)
    ]
    if all(filename in available for filename in expected):
        return expected
    return [first]


def download_model(
    spec: ModelSpec,
    *,
    progress: ProgressCallback | None = None,
    hf_token: str | None = None,
) -> Path:
    filenames = resolve_hf_filenames(spec)
    destination = model_dir(spec)
    destination.mkdir(parents=True, exist_ok=True)
    first_target: Path | None = None
    for filename in filenames:
        target = destination / Path(filename).name
        if first_target is None:
            first_target = target
        _download_one_file(
            repo_id=spec.repo_id,
            filename=filename,
            target=target,
            progress=progress,
            hf_token=hf_token,
        )
    if first_target is None:
        raise RuntimeError(f"No GGUF file matching {spec.filename_glob!r} in {spec.repo_id}")
    return first_target


def _download_one_file(
    *,
    repo_id: str,
    filename: str,
    target: Path,
    progress: ProgressCallback | None,
    hf_token: str | None,
) -> Path:
    partial = target.with_suffix(target.suffix + ".part")

    headers: dict[str, str] = {}
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"

    resume_from = partial.stat().st_size if partial.exists() else 0
    if resume_from > 0:
        headers["Range"] = f"bytes={resume_from}-"

    url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"
    with httpx.Client(timeout=None, follow_redirects=True) as client:
        with client.stream("GET", url, headers=headers) as response:
            if response.status_code == 416:
                partial.replace(target)
                if progress:
                    size = target.stat().st_size
                    progress(size, size)
                return target
            if resume_from > 0 and response.status_code != 206:
                resume_from = 0
            response.raise_for_status()

            content_length = response.headers.get("Content-Length")
            try:
                remaining = int(content_length) if content_length else None
            except ValueError:
                remaining = None
            total = (resume_from + remaining) if remaining is not None else None

            mode = "ab" if resume_from > 0 and response.status_code == 206 else "wb"
            downloaded = resume_from if mode == "ab" else 0
            with partial.open(mode) as file:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    file.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)

    partial.replace(target)
    if progress:
        size = target.stat().st_size
        progress(size, size)
    return target


def ensure_model_dirs() -> None:
    # Keep both top-level kind folders visible even before any download.
    kind_dir("llm").mkdir(parents=True, exist_ok=True)
    kind_dir("embedding").mkdir(parents=True, exist_ok=True)
