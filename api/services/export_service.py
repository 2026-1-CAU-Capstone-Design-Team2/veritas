"""Export a markdown document to DOCX / PDF / HTML / MD via the ``pandoc`` CLI.

The editor stores plain markdown; exporting to anything richer than ``.md``
shells out to pandoc. We deliberately do *not* add a Python conversion
dependency (per project policy) — pandoc is the one external tool.

Failure handling is user-facing: a missing pandoc, or a missing PDF engine
(pandoc needs a separate LaTeX / wkhtmltopdf / weasyprint engine to make a
PDF), surfaces as a friendly Korean :class:`HTTPException` message rather than
a raw stderr dump. ``.md`` never touches pandoc, so MD export always works.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import HTTPException


_PANDOC_FORMATS = {"docx", "pdf", "html"}
_PANDOC_TIMEOUT_SEC = 120


def export(content: str, fmt: str, output_path: str) -> dict[str, object]:
    """Write *content* (markdown) to *output_path* in the requested format.

    Returns ``{"ok": True, "path": ..., "format": ..., "engine": ...}`` on
    success; raises :class:`HTTPException` with a friendly message otherwise.
    """
    fmt = (fmt or "").lower().strip()
    if not output_path or not str(output_path).strip():
        raise HTTPException(status_code=422, detail="저장 경로가 비어 있습니다.")

    out = Path(output_path).expanduser()
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=422,
            detail=f"저장 폴더를 만들 수 없습니다: {out.parent} ({e})",
        ) from e

    body = content or ""

    # MD: a plain write, no external tool — always available.
    if fmt == "md":
        try:
            out.write_text(body, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"파일 저장 실패: {e}") from e
        return {"ok": True, "path": str(out), "format": "md", "engine": "direct"}

    if fmt not in _PANDOC_FORMATS:
        raise HTTPException(status_code=422, detail=f"지원하지 않는 내보내기 형식입니다: {fmt}")

    pandoc = shutil.which("pandoc")
    if pandoc is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "pandoc이 설치되어 있지 않아 DOCX/PDF/HTML로 내보낼 수 없습니다. "
                "https://pandoc.org/install.html 에서 설치한 뒤 다시 시도해 주세요. "
                "(MD 형식은 pandoc 없이도 내보낼 수 있습니다.)"
            ),
        )

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "input.md"
        src.write_text(body, encoding="utf-8")

        cmd = [pandoc, str(src), "-f", "markdown", "-o", str(out)]
        if fmt == "html":
            cmd += ["-t", "html5", "-s"]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_PANDOC_TIMEOUT_SEC,
            )
        except FileNotFoundError as e:
            raise HTTPException(
                status_code=422,
                detail="pandoc 실행 파일을 찾을 수 없습니다. 설치 상태를 확인해 주세요.",
            ) from e
        except subprocess.TimeoutExpired as e:
            raise HTTPException(
                status_code=504,
                detail="문서 변환이 시간 초과되었습니다. 문서 크기를 줄이고 다시 시도해 주세요.",
            ) from e

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if fmt == "pdf":
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "PDF로 내보내려면 PDF 엔진이 추가로 필요합니다. "
                        "TeX 배포판(MiKTeX·TeX Live의 xelatex/pdflatex) 또는 wkhtmltopdf를 설치하거나, "
                        "DOCX/HTML/MD 형식으로 내보내 주세요.\n\n"
                        f"[pandoc] {stderr[:500]}"
                    ),
                )
            raise HTTPException(
                status_code=500,
                detail=f"문서 변환에 실패했습니다.\n\n[pandoc] {stderr[:500]}",
            )

    return {"ok": True, "path": str(out), "format": fmt, "engine": "pandoc"}
