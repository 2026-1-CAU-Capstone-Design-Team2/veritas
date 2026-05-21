"""Heuristic form-template extraction from uploaded document files.

The draft "양식 파일 사용" flow lets a user upload an existing document and reuse
its *structure* as a draft template. The file may be a blank form (headings
only) or a filled-out document (headings + body prose); we keep only the
structural skeleton — headings, bullet/numbered list items, and tables — and
drop plain body paragraphs, emitting a Markdown template the draft generator
then fills from the workspace knowledge base.

Format support (all best-effort; optional deps are import-guarded):

* ``.docx`` — python-docx. Heading level comes from the paragraph *style*
  ("Heading 1".."Heading 9" / "제목 1".. / "Title"), list items from list
  styles or numbering, tables from the grid. The most reliable path.
* ``.pdf``  — pypdf text, then the heuristic line classifier (no font metadata,
  so structure is inferred from text patterns).
* ``.hwpx`` — OWPML (zip + XML): paragraphs parsed in order, then the classifier.
* ``.hwp``  — olefile ``PrvText`` preview stream (body streams are
  compressed/record-encoded), then the classifier. Preview text can be
  truncated, so long .hwp forms may extract only partially.
* ``.doc``  — best-effort readable-text scrape + classifier (legacy binary Word
  has no clean structure reader here).

The classifier labels each line as a heading (with inferred level), list item,
table row, or body — and drops body. Headings come from explicit syntax
(markdown ``#``, decimal numbering ``1.2``, Korean enumerators 제1장 / 가. / ①,
brackets 【…】) plus a conservative "short title-like line" rule. The user
reviews/edits the resulting outline in the wizard, so erring slightly toward
keeping a candidate is fine.
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Any

try:  # PDF
    from pypdf import PdfReader
except Exception:  # pragma: no cover - optional dep
    PdfReader = None  # type: ignore[assignment]

try:  # DOCX
    from docx import Document
except Exception:  # pragma: no cover - optional dep
    Document = None  # type: ignore[assignment]

try:  # HWP (binary, OLE)
    import olefile
except Exception:  # pragma: no cover - optional dep
    olefile = None  # type: ignore[assignment]


_MAX_OUTLINE = 80
_MAX_TEMPLATE_LINES = 400


# ------------------------------------------------------------------ public API

def extract_form(filename: str, raw: bytes) -> dict[str, Any]:
    """Extract a Markdown form template + outline from an uploaded document.

    Returns ``{"markdown", "outline", "format", "note"}``. ``markdown`` is the
    structure-only template (headings / bullets / tables, body removed),
    ``outline`` the ordered list of heading texts for the wizard, ``note`` a
    short human-readable caveat (or "").
    """
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if not raw:
        return _result("", [], suffix, "빈 파일입니다.")

    if suffix == "docx":
        md, outline, note = _from_docx(raw)
    elif suffix == "pdf":
        md, outline, note = _from_pdf(raw)
    elif suffix == "hwpx":
        md, outline, note = _from_hwpx(raw)
    elif suffix == "hwp":
        md, outline, note = _from_hwp(raw)
    elif suffix == "doc":
        md, outline, note = _from_doc(raw)
    elif suffix in {"md", "markdown", "txt", "rst"}:
        md, outline = _lines_to_markdown(_decode_text(raw).splitlines())
        note = ""
    else:
        md, outline = _lines_to_markdown(_decode_text(raw).splitlines())
        note = f"지원 목록에 없는 형식(.{suffix})이라 평문 휴리스틱으로 처리했습니다."

    if not md.strip():
        note = (note + " " if note else "") + "구조(제목·목록·표)를 찾지 못했습니다. 목차를 직접 작성해 주세요."
    return _result(md, outline, suffix, note.strip())


def _result(markdown: str, outline: list[str], fmt: str, note: str) -> dict[str, Any]:
    return {"markdown": markdown, "outline": outline[:_MAX_OUTLINE], "format": fmt, "note": note}


# ---------------------------------------------------------------- DOCX (styled)

def _from_docx(raw: bytes) -> tuple[str, list[str], str]:
    if Document is None:
        md, outline = _lines_to_markdown(_decode_text(raw).splitlines())
        return md, outline, "python-docx 미설치 — 평문 휴리스틱으로 처리했습니다."
    try:
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        doc = Document(BytesIO(raw))
    except Exception:
        md, outline = _lines_to_markdown(_decode_text(raw).splitlines())
        return md, outline, "docx 파싱 실패 — 평문 휴리스틱으로 처리했습니다."

    blocks: list[str] = []
    outline: list[str] = []
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            para = Paragraph(child, doc)
            text = para.text.strip()
            if not text:
                continue
            kind, level = _docx_paragraph_kind(para)
            if kind == "heading":
                blocks.append(f"{'#' * min(max(level, 1), 6)} {text}")
                outline.append(text)
            elif kind == "bullet":
                blocks.append(f"- {text}")
            # body paragraphs are dropped
        elif child.tag == qn("w:tbl"):
            table_md = _docx_table_md(Table(child, doc))
            if table_md:
                blocks.append(table_md)

    return _spaced(blocks), outline, ""


def _docx_paragraph_kind(paragraph: Any) -> tuple[str, int]:
    style = str(getattr(getattr(paragraph, "style", None), "name", "") or "").strip()
    match = re.match(r"(?:Heading|제목|개요)\s*(\d+)", style, flags=re.IGNORECASE)
    if match:
        return "heading", int(match.group(1))
    if style in {"Title", "제목"}:
        return "heading", 1
    if style in {"Subtitle", "부제", "부제목"}:
        return "heading", 2
    if "List" in style or "목록" in style or _docx_has_numbering(paragraph):
        return "bullet", 0
    return "body", 0


def _docx_has_numbering(paragraph: Any) -> bool:
    try:
        ppr = paragraph._p.pPr
        return ppr is not None and ppr.numPr is not None
    except Exception:
        return False


def _docx_table_md(table: Any) -> str:
    rows: list[list[str]] = []
    try:
        for row in table.rows:
            rows.append([" ".join((cell.text or "").split()) for cell in row.cells])
    except Exception:
        return ""
    rows = [r for r in rows if any(c for c in r)] or rows
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    def fmt(cells: list[str]) -> str:
        return "| " + " | ".join(c if c else " " for c in cells) + " |"

    out = [fmt(rows[0]), "| " + " | ".join(["---"] * width) + " |"]
    out.extend(fmt(r) for r in rows[1:])
    return "\n".join(out)


# --------------------------------------------------------------- PDF / HWP / DOC

def _from_pdf(raw: bytes) -> tuple[str, list[str], str]:
    if PdfReader is None:
        return "", [], "pypdf 미설치 — PDF를 처리할 수 없습니다."
    try:
        reader = PdfReader(BytesIO(raw))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return "", [], "PDF 텍스트 추출에 실패했습니다."
    md, outline = _lines_to_markdown(text.splitlines())
    return md, outline, "PDF는 텍스트 기반 휴리스틱으로 추출되어 표·서식 인식이 제한적입니다."


def _from_hwpx(raw: bytes) -> tuple[str, list[str], str]:
    import zipfile
    from xml.etree import ElementTree as ET

    paragraphs: list[str] = []
    try:
        with zipfile.ZipFile(BytesIO(raw)) as zf:
            names = sorted(
                name
                for name in zf.namelist()
                if name.lower().endswith(".xml") and "section" in name.lower()
            )
            for name in names:
                try:
                    root = ET.fromstring(zf.read(name))
                except ET.ParseError:
                    continue
                _hwpx_collect_paragraphs(root, paragraphs)
    except Exception:
        return "", [], "HWPX 파싱에 실패했습니다."
    if not paragraphs:
        return "", [], "HWPX에서 문단을 찾지 못했습니다."
    md, outline = _lines_to_markdown(paragraphs)
    return md, outline, ""


def _hwpx_collect_paragraphs(element: Any, out: list[str]) -> None:
    """Append one line per ``<hp:p>`` paragraph; consume its whole subtree.

    Stopping the descent at each paragraph avoids double-counting text from
    paragraphs nested inside table cells while still preserving paragraph
    boundaries for the line classifier.
    """
    if _local(element.tag) == "p":
        text = "".join(
            (node.text or "") for node in element.iter() if _local(node.tag) == "t"
        ).strip()
        out.append(text)
        return
    for child in list(element):
        _hwpx_collect_paragraphs(child, out)


def _from_hwp(raw: bytes) -> tuple[str, list[str], str]:
    text = ""
    if olefile is not None:
        try:
            buffer = BytesIO(raw)
            if olefile.isOleFile(buffer):
                buffer.seek(0)
                ole = olefile.OleFileIO(buffer)
                try:
                    if ole.exists("PrvText"):
                        with ole.openstream("PrvText") as stream:
                            text = (
                                stream.read()
                                .decode("utf-16le", errors="ignore")
                                .replace("\x00", "")
                            )
                finally:
                    ole.close()
        except Exception:
            text = ""
    if not text.strip():
        return "", [], "HWP 본문 미리보기를 추출하지 못했습니다. .hwpx 로 저장 후 다시 시도해 주세요."
    md, outline = _lines_to_markdown(text.splitlines())
    return md, outline, "HWP는 미리보기(PrvText) 기반이라 긴 문서는 일부만 추출될 수 있습니다."


def _from_doc(raw: bytes) -> tuple[str, list[str], str]:
    text = _scrape_binary_text(raw)
    md, outline = _lines_to_markdown(text.splitlines())
    return md, outline, ".doc(구버전)는 구조 추출이 제한적입니다. 가능하면 .docx 로 변환해 주세요."


def _scrape_binary_text(raw: bytes) -> str:
    mixed = f"{raw.decode('utf-16le', errors='ignore')}\n{raw.decode('cp949', errors='ignore')}"
    seen: set[str] = set()
    lines: list[str] = []
    for candidate in re.findall(r"[A-Za-z0-9가-힣][^\x00-\x08\x0e-\x1f]{2,}", mixed):
        line = " ".join(candidate.split())
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return "\n".join(lines)


# ------------------------------------------------------------- heuristic core

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_BULLET = re.compile(r"^\s*[-*•·◦▪‣○※□■☐▶●∙o]\s+(\S.*)$")
_NUM_HEADING = re.compile(r"^(\d+(?:\.\d+){0,5})[.)]?\s+(\S.*)$")
_KO_CHAPTER = re.compile(r"^제?\s*\d+\s*([장절관조항편])\b\s*[.)]?\s*(.*)$")
_KO_ENUM = re.compile(
    r"^([가나다라마바사아자차카타파하]|[ㄱ-ㅎ]|[①-⑳]|[Ⓐ-Ⓩⓐ-ⓩ]|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+|[IVXivx]+|[A-Za-z])[.)]\s+(\S.*)$"
)
_PAREN_NUM = re.compile(r"^\(\s*(\d+|[가-힣]|[a-zA-Z])\s*\)\s+(\S.*)$")
_BRACKET = re.compile(r"^[\[【〔<({]\s*(.+?)\s*[\]】〕>)}]\s*[:：]?\s*$")
_LABEL = re.compile(r"^([^\s:：][^:：]{0,28})\s*[:：]\s*$")  # "사업명:" style label
_SENTENCE_END = re.compile(r"(?:[.?!。…]|니다|습니다|한다|이다|였다|된다|있다|없다|음|함|됨)$")


def _looks_like_sentence(text: str) -> bool:
    t = text.strip().rstrip(")]}】〕>")
    return bool(t) and bool(_SENTENCE_END.search(t))


def _short_enough_for_heading(rest: str) -> bool:
    r = (rest or "").strip()
    if len(r) > 50 or r.count(" ") > 8:
        return False
    return not _looks_like_sentence(r)


def _is_bare_title(text: str) -> bool:
    t = text.strip()
    if not t or len(t) > 30 or t.count(" ") > 5:
        return False
    if _looks_like_sentence(t):
        return False
    if t.endswith((",", "、", "·", "和")):
        return False
    # Reject lines that read like a fragment of prose (start with a closing
    # bracket / connective) — keep this light; the user prunes the outline.
    return True


def _classify(text: str) -> tuple[str, int, str]:
    match = _MD_HEADING.match(text)
    if match:
        return "heading", len(match.group(1)), match.group(2).strip()
    if _TABLE_ROW.match(text):
        return "table", 0, text.strip()

    match = _BULLET.match(text)
    if match:
        return "bullet", 0, match.group(1).strip()

    match = _KO_CHAPTER.match(text)
    if match:
        # Recognize Korean division markers as headings, but only as a coarse
        # major/minor split (편·장 → 1, else → 2). Mapping each legal marker to a
        # distinct depth doesn't generalize across document conventions; the
        # *general* level signals — markdown ``#`` depth and decimal numbering
        # (``1.2.3``) — do the real work, and the user edits the outline anyway.
        level = 1 if match.group(1) in ("편", "장") else 2
        return "heading", level, text

    match = _NUM_HEADING.match(text)
    if match and _short_enough_for_heading(match.group(2)):
        return "heading", min(match.group(1).count(".") + 1, 4), text

    match = _KO_ENUM.match(text)
    if match and _short_enough_for_heading(match.group(2)):
        return "heading", 3, text

    match = _PAREN_NUM.match(text)
    if match and _short_enough_for_heading(match.group(2)):
        return "heading", 3, text

    match = _BRACKET.match(text)
    if match:
        return "heading", 2, match.group(1).strip()

    if _LABEL.match(text):
        return "heading", 3, text.rstrip(":：").strip()

    if _is_bare_title(text):
        return "heading", 2, text

    return "body", 0, text


def _lines_to_markdown(lines: list[str]) -> tuple[str, list[str]]:
    blocks: list[str] = []
    outline: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        kind, level, text = _classify(stripped)
        if not text:
            continue
        if kind == "heading":
            blocks.append(f"{'#' * min(max(level, 1), 6)} {text}")
            outline.append(text)
        elif kind == "bullet":
            blocks.append(f"- {text}")
        elif kind == "table":
            blocks.append(text)
        if len(blocks) >= _MAX_TEMPLATE_LINES:
            break
    return _spaced(blocks), outline


def _spaced(blocks: list[str]) -> str:
    """Join blocks, inserting a blank line before each heading (after the first)
    so the template renders cleanly as Markdown."""
    out: list[str] = []
    for block in blocks:
        if out and block.startswith("#"):
            out.append("")
        out.append(block)
    return "\n".join(out).strip()


def _local(tag: Any) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1] if "}" in text else text


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8", "cp949", "utf-16le"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


__all__ = ["extract_form"]
