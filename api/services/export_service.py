"""Export a markdown document to DOCX / HTML / MD (via ``pandoc``) and PDF (via
a pure-Python ``markdown`` -> HTML -> ``xhtml2pdf`` pipeline).

The editor stores plain markdown. ``.md`` is a direct write (no tool). DOCX and
HTML shell out to pandoc, which ships bundled via ``pypandoc-binary`` so
``pip install -r requirements.txt`` enables them out of the box.

PDF deliberately does *not* use pandoc: pandoc can only make a PDF through a
separate engine (LaTeX / wkhtmltopdf / weasyprint), none of which pip-installs
cleanly on Windows. Instead PDF renders in-process with ``xhtml2pdf`` +
``reportlab`` and embeds a Korean TTF, so it works after a plain pip install and
CJK text always renders — no external engine, no conda step.

Failure handling is user-facing: a missing pandoc (DOCX/HTML) surfaces as a
friendly Korean :class:`HTTPException` rather than a raw stderr dump. ``.md`` and
PDF never touch pandoc, so they work regardless.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException


_PANDOC_FORMATS = {"docx", "html"}
_PANDOC_TIMEOUT_SEC = 120


def _env_bin_dirs() -> list[Path]:
    """Binary dirs of the conda/venv this Python runs in.

    The API may be launched via ``envs/<name>/python.exe`` directly (without
    ``conda activate``), so a conda-installed pandoc lives under the env but is
    NOT on ``PATH``. Resolving against ``sys.prefix`` makes export work
    regardless of how the process was started.
    """
    prefix = Path(sys.prefix)
    return [prefix / "Library" / "bin", prefix / "Scripts", prefix / "bin", prefix]


def _find_executable(*names: str) -> str | None:
    """Locate a CLI tool on PATH, then in this Python env's binary dirs, and
    (for pandoc) finally via a pip-installed ``pypandoc-binary`` bundle."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for directory in _env_bin_dirs():
        for name in names:
            for candidate in (directory / name, directory / f"{name}.exe"):
                if candidate.is_file():
                    return str(candidate)
    if "pandoc" in names:
        # `pypandoc-binary` (requirements.txt) ships the pandoc executable inside
        # the package rather than on PATH; ask it where that is.
        try:
            import pypandoc  # noqa: PLC0415 — optional, imported lazily

            path = pypandoc.get_pandoc_path()
            if path:
                # On Windows pypandoc reports the path WITHOUT the ".exe" suffix
                # (the bundled file is pandoc.exe), so probe both spellings —
                # same dual-probe the env-bin-dirs loop above uses.
                for candidate in (Path(path), Path(f"{path}.exe")):
                    if candidate.is_file():
                        return str(candidate)
        except Exception:
            pass
    return None


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

    # PDF: pure-Python markdown -> HTML -> PDF (xhtml2pdf). No pandoc and no
    # external PDF engine, so it works after a plain `pip install`.
    if fmt == "pdf":
        return _export_pdf(body, out)

    if fmt not in _PANDOC_FORMATS:
        raise HTTPException(status_code=422, detail=f"지원하지 않는 내보내기 형식입니다: {fmt}")

    pandoc = _find_executable("pandoc")
    if pandoc is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "pandoc이 설치되어 있지 않아 DOCX/HTML로 내보낼 수 없습니다. "
                "https://pandoc.org/install.html 에서 설치한 뒤 다시 시도해 주세요. "
                "(MD·PDF 형식은 pandoc 없이도 내보낼 수 있습니다.)"
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
            raise HTTPException(
                status_code=500,
                detail=f"문서 변환에 실패했습니다.\n\n[pandoc] {stderr[:500]}",
            )

    return {"ok": True, "path": str(out), "format": fmt, "engine": "pandoc"}


# --------------------------------------------------------------------------
# PDF export: markdown -> HTML -> PDF, entirely in-process (no pandoc, no
# external PDF engine). Pure pip dependencies (markdown + xhtml2pdf/reportlab).
# --------------------------------------------------------------------------

# CSS font-family our generated HTML references; the real TTF/CID font is bound
# to this name in _register_korean_font(). Kept lowercase — xhtml2pdf lowercases
# font names during CSS lookup.
_PDF_FONT_NAME = "veritaskorean"
_PDF_FONT_READY = False

# Korean fonts to embed, (regular, bold) by preference. Malgun Gothic ships with
# every Windows since Vista; its bold face is a separate file. Embedding a real
# TTF means the PDF renders identically in any viewer.
_KOREAN_TTF_CANDIDATES: tuple[tuple[str, str | None], ...] = (
    (r"C:\Windows\Fonts\malgun.ttf", r"C:\Windows\Fonts\malgunbd.ttf"),
    (r"C:\Windows\Fonts\gulim.ttc", None),
)

_PDF_CSS = """
@page { size: A4; margin: 2cm; }
body { font-family: "__FONT__"; font-size: 10.5pt; line-height: 1.5; color: #222; }
h1 { font-size: 20pt; margin: 0 0 12pt; }
h2 { font-size: 15pt; margin: 16pt 0 8pt; }
h3 { font-size: 12.5pt; margin: 14pt 0 6pt; }
p { margin: 0 0 8pt; }
ul, ol { margin: 0 0 8pt 18pt; }
li { margin: 0 0 3pt; }
table { border-collapse: collapse; margin: 8pt 0; }
th, td { border: 1px solid #999; padding: 4px 8px; }
th { background: #f0f0f0; }
pre { background: #f5f5f5; border: 1px solid #ddd; padding: 8px; }
code { font-family: "__FONT__"; background: #f5f5f5; }
blockquote { margin: 8pt 0; padding-left: 12pt; border-left: 3px solid #ccc; color: #555; }
"""


def _register_korean_font() -> str:
    """Bind a Unicode Korean font to ``_PDF_FONT_NAME``; return that name for use
    in CSS ``font-family``. Idempotent.

    Prefers embedding a system TTF (Malgun Gothic) so CJK glyphs are guaranteed
    in every viewer. The font is registered under xhtml2pdf's own
    ``name_<bold><italic>`` naming so xhtml2pdf's ``@font-face`` loader sees the
    name as already present and SKIPS copying the font to a temp file — that copy
    fails on Windows (``PermissionError``) because the temp handle is still open
    when reportlab reopens it by name. Falls back to reportlab's bundled Korean
    CID font when no TTF is found (its glyphs are not embedded, so rendering then
    relies on the viewer's CJK support)."""
    global _PDF_FONT_READY
    if _PDF_FONT_READY:
        return _PDF_FONT_NAME

    import xhtml2pdf.default as xdefault  # noqa: PLC0415
    from reportlab.lib.fonts import addMapping  # noqa: PLC0415
    from reportlab.pdfbase import pdfmetrics  # noqa: PLC0415
    from reportlab.pdfbase.ttfonts import TTFont  # noqa: PLC0415

    name = _PDF_FONT_NAME
    regular = bold = None
    for reg_path, bold_path in _KOREAN_TTF_CANDIDATES:
        if Path(reg_path).is_file():
            regular = reg_path
            bold = bold_path if bold_path and Path(bold_path).is_file() else None
            break

    if regular is not None:
        pdfmetrics.registerFont(TTFont(f"{name}_00", regular))
        bold_alias = f"{name}_00"
        if bold is not None:
            pdfmetrics.registerFont(TTFont(f"{name}_10", bold))
            bold_alias = f"{name}_10"
        # (bold, italic) -> registered face. Malgun has no italic face, so italic
        # maps to the upright face of the matching weight.
        addMapping(name, 0, 0, f"{name}_00")
        addMapping(name, 0, 1, f"{name}_00")
        addMapping(name, 1, 0, bold_alias)
        addMapping(name, 1, 1, bold_alias)
    else:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # noqa: PLC0415

        cid = "HYSMyeongJo-Medium"  # reportlab-bundled Korean font
        pdfmetrics.registerFont(UnicodeCIDFont(cid))
        for b in (0, 1):
            for i in (0, 1):
                addMapping(name, b, i, cid)

    # Make the family resolvable by xhtml2pdf's CSS font lookup.
    xdefault.DEFAULT_FONT[name] = name
    _PDF_FONT_READY = True
    return name


def _export_pdf(body: str, out: Path) -> dict[str, object]:
    """Render markdown *body* to a PDF at *out* (markdown -> HTML -> xhtml2pdf).

    Raises a friendly :class:`HTTPException` on any failure.
    """
    try:
        import markdown  # noqa: PLC0415
        from xhtml2pdf import pisa  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=(
                "PDF 내보내기에 필요한 라이브러리를 불러올 수 없습니다. "
                "`pip install -r requirements.txt`로 의존성을 설치해 주세요."
            ),
        ) from e

    font = _register_korean_font()
    html_body = markdown.markdown(body, extensions=["extra", "sane_lists"])
    html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
        + _PDF_CSS.replace("__FONT__", font)
        + f"</style></head><body>{html_body}</body></html>"
    )

    try:
        with out.open("wb") as fh:
            status = pisa.CreatePDF(html, dest=fh, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"PDF 변환에 실패했습니다: {e}") from e

    if status.err:
        raise HTTPException(status_code=500, detail="PDF 변환에 실패했습니다.")

    return {"ok": True, "path": str(out), "format": "pdf", "engine": "xhtml2pdf"}
