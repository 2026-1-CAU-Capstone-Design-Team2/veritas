from __future__ import annotations

import re

from .models import AppTextResult, FilteredScreenContext, OcrResult, UiAutomationResult, WindowContext


# "사용자가 지금 글을 쓰고 있는 영역"을 푸는 단일 규칙. ContentFilter(시나리오가
# 스캔할 cursor_scope_text 산출)와 InterventionDispatcher(LLM에 줄 writing context
# 앵커)가 공유한다. 우선순위: caret 문단 → 커서 위치(diff offset) 앞 윈도우 →
# 문서 꼬리. **문서 머리(서론)는 절대 앵커로 쓰지 않는다.**
CURSOR_SCOPE_CHANGE_MAX_RATIO = 0.6
_CURSOR_SCOPE_TAIL_CHARS = 800
# caret 미검출 시 커서(편집 끝) offset 앞으로 가져올 실제 텍스트 길이. 작은 diff
# 한 조각("로")이 아니라 그 주변 실제 문장을 컨텍스트로 준다.
_CURSOR_SCOPE_WINDOW_CHARS = 600


def resolve_cursor_scope(
    *, full: str, caret: str, changed: str, cursor_offset: int | None = None
) -> tuple[str, str]:
    """``(scope_text, focus_hint)`` 반환.

    - ``scope_text``: 위치 특정 시나리오가 스캔하고 LLM writing context가 앵커할 영역.
    - ``focus_hint``: 그 안에서의 최근 편집(있으면) — 커서가 있는 문장을 고를 때 쓴다.

    우선순위:
    1. caret 문단이 '진짜 문단'(전체 문서 fallback이 아님)이면 그걸.
    2. caret 없고 ``cursor_offset``(편집 끝 위치)을 알면 **그 앞 윈도우의 실제
       텍스트**(``full[offset-WINDOW:offset]``). UIA가 caret 문단을 못 줘도(예:
       notepad의 uia_full_text_fallback) 커서 주변 진짜 문장을 컨텍스트로 준다.
       작은 diff 한 조각을 scope로 쓰던 회귀를 막는다.
    3. 둘 다 없으면 문서 꼬리.

    ``focus_hint``는 bounded 편집(문서 전체 dump가 아닌)이면 크기와 무관하게 그대로
    — 커서가 있는 문장을 고르는 용도라 한두 글자여도 된다."""
    full = (full or "").strip()
    caret = (caret or "").strip()
    changed = (changed or "").strip()
    focus_hint = (
        changed
        if changed and len(changed) < max(1, len(full)) * CURSOR_SCOPE_CHANGE_MAX_RATIO
        else ""
    )
    if caret and caret != full:
        return caret, focus_hint
    if cursor_offset is not None and full:
        off = max(0, min(int(cursor_offset), len(full)))
        window = full[max(0, off - _CURSOR_SCOPE_WINDOW_CHARS):off].strip()
        if window:
            return window, focus_hint
    if full:
        return full[-_CURSOR_SCOPE_TAIL_CHARS:], focus_hint
    return caret or changed, focus_hint


class ContentFilter:
    def __init__(self, *, custom_document_tools: list[dict[str, str]] | None = None) -> None:
        # User-registered document apps from settings (documentTools.custom).
        # Each entry is {"name", "identifier"}; we treat a window as a
        # "document" editing app when its process name or title matches one of
        # these tokens, so an editor not in the hardcoded list below still
        # qualifies. This is explicit user configuration, not a vocabulary
        # keyword heuristic — it carries no domain terms.
        self._custom_doc_tokens: list[str] = []
        for tool in custom_document_tools or []:
            if not isinstance(tool, dict):
                continue
            for key in ("identifier", "name"):
                token = str(tool.get(key) or "").strip().lower()
                if token:
                    self._custom_doc_tokens.append(token)
        # 마지막으로 편집(diff)이 잡힌 커서 offset과 그때의 텍스트. caret을 못 주는
        # 앱(notepad의 uia_full_text_fallback)에서, 사용자가 멈춘(=현재 캡처에 diff
        # 없는) 동안에도 직전 편집 위치를 기억해 cursor_located를 유지한다. idle
        # (이어쓰기)는 *멈춤* 시 발화하므로 이 sticky가 없으면 그런 앱에서 영영 안 뜬다.
        self._last_cursor_offset: int | None = None
        self._last_cursor_text: str = ""

    @staticmethod
    def _nearest_heading(text: str) -> str:
        """문서 머리부터 커서까지(``text``)에서 커서에 가장 가까운(=마지막) 마크다운
        헤딩 제목. ``#`` 마커를 떼고 반환, 없으면 ``""``. native editor의
        ``ChatAgent._nearest_heading``과 동일한 규칙 — 커서가 속한 섹션 주제를
        이어쓰기에 명시 주입하기 위함."""
        heading = ""
        for line in (text or "").splitlines():
            match = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line.strip())
            if match:
                heading = match.group(2).strip()
        return heading

    def _sticky_cursor_offset(
        self, active_text: str, change_offset: int | None
    ) -> int | None:
        """현재 캡처에 변경이 있으면 그 offset을 기록·반환. 변경이 없어도 텍스트가
        직전 편집 시점과 동일하면(=사용자가 멈춘 것) 기억해 둔 offset을 그대로 쓴다.
        텍스트가 (편집 외 이유로) 달라졌으면 위치 불명 → None."""
        if change_offset is not None:
            self._last_cursor_offset = change_offset
            self._last_cursor_text = active_text
            return change_offset
        if self._last_cursor_offset is not None and active_text == self._last_cursor_text:
            return self._last_cursor_offset
        return None

    def build(
        self,
        *,
        window: WindowContext,
        ocr: OcrResult,
        app_text: AppTextResult | None = None,
        ui_automation: UiAutomationResult | None = None,
        previous_text: str = "",
    ) -> FilteredScreenContext:
        app_text_value = self._normalize(app_text.text) if app_text else ""
        ui_is_usable = self._is_usable_ui_source(ui_automation)
        ui_text = self._normalize(ui_automation.text) if ui_is_usable and ui_automation else ""
        ui_current_paragraph = (
            self._normalize(ui_automation.current_paragraph_text)
            if ui_is_usable and ui_automation
            else ""
        )
        ocr_text = self._normalize(ocr.text)

        active_text = app_text_value or ui_text or ocr_text
        current_paragraph = self._resolve_current_paragraph(
            app_text=app_text_value,
            ui_text=ui_text,
            ui_current_paragraph=ui_current_paragraph,
            ocr_text=ocr_text,
        )
        current_paragraph_source = self._resolve_current_paragraph_source(
            app_text=app_text_value,
            ui_text=ui_text,
            ui_current_paragraph=ui_current_paragraph,
            ocr_text=ocr_text,
            ui_automation=ui_automation,
        )
        changed_text, change_offset = self._diff_region(previous_text, active_text)
        # 멈춤(현재 캡처에 변경 없음) 동안에도 직전 편집 위치를 유지(sticky).
        effective_offset = self._sticky_cursor_offset(active_text, change_offset)
        cursor_scope_text, _focus = resolve_cursor_scope(
            full=active_text,
            caret=current_paragraph,
            changed=changed_text,
            cursor_offset=effective_offset,
        )
        # 커서를 신뢰있게 잡았는가: OCR-only가 아니고(화면 통째 read), 그리고 진짜
        # caret 문단(전체 문서 fallback이 아님) 또는 (sticky 포함) 편집 위치가 있을 때.
        # 둘 다 없으면(꼬리 추정) "지금 쓰는 곳"을 모르는 것.
        _para_norm = current_paragraph.strip()
        cursor_located = (
            not current_paragraph_source.startswith("ocr")
            and (
                (bool(_para_norm) and _para_norm != active_text.strip())
                or effective_offset is not None
            )
        )
        # 커서가 속한 섹션 제목(문서 머리부터 커서까지에서 가장 가까운 마크다운
        # 헤딩). native editor의 _nearest_heading과 동일 — "## 결론" 아래에서 쓰면
        # '결론' 섹션을 작성 중임을 LLM에 명시 주입한다. 헤딩이 커서 윈도우(600자)
        # 밖으로 밀려도 전체 prefix를 스캔하므로 유지된다.
        section_heading = ""
        if cursor_located:
            prefix_to_caret = (
                active_text[: effective_offset]
                if effective_offset is not None
                else cursor_scope_text
            )
            section_heading = self._nearest_heading(prefix_to_caret)

        return FilteredScreenContext(
            active_app_type=self._guess_app_type(window, ui_automation=ui_automation),
            active_editor_text=active_text,
            current_paragraph_text=current_paragraph,
            current_paragraph_source=current_paragraph_source,
            current_paragraph_rect=(
                ui_automation.current_paragraph_rect
                if ui_is_usable and ui_automation and current_paragraph_source.startswith("uia_")
                else None
            ),
            cursor_scope_text=cursor_scope_text,
            cursor_located=cursor_located,
            section_heading=section_heading,
            visible_context=active_text,
            changed_text=changed_text,
            confidence=self._score_confidence(
                app_text=app_text_value,
                ui_text=ui_text,
                ocr_text=ocr_text,
            ),
            noise_removed=[],
        )

    def _normalize(self, text: str) -> str:
        text = text.replace("\r\n", "\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _diff_region(self, previous: str, current: str) -> tuple[str, int | None]:
        """``(region_text, cursor_offset)`` — the text region that changed between
        two captures (i.e. *where the user is actively writing*) and the offset in
        ``current`` where that change ends (≈ the caret).

        Trims the common prefix AND the common suffix, so a mid-document edit
        yields just the changed span rather than the whole document. This is the
        most reliable, app-agnostic "current writing location" signal: it works
        even when UIA can't read the caret — the returned ``cursor_offset`` lets
        ``resolve_cursor_scope`` pull real text around the cursor instead of the
        tiny changed fragment.

        Returns ``("", None)`` on the first capture (no ``previous``) and when
        nothing changed, so anchoring falls back to the document tail, never the
        head."""
        if not current or not previous or previous == current:
            return "", None
        # Common prefix.
        limit = min(len(previous), len(current))
        start = 0
        while start < limit and previous[start] == current[start]:
            start += 1
        # Common suffix (not crossing into the already-matched prefix).
        end_prev, end_cur = len(previous), len(current)
        while (
            end_cur > start
            and end_prev > start
            and previous[end_prev - 1] == current[end_cur - 1]
        ):
            end_prev -= 1
            end_cur -= 1
        return current[start:end_cur].strip(), end_cur

    def _guess_app_type(
        self,
        window: WindowContext,
        *,
        ui_automation: UiAutomationResult | None = None,
    ) -> str:
        name = window.process_name.lower()
        title = window.window_title.lower()
        browser_url = (ui_automation.browser_url or "").lower() if ui_automation else ""
        web_editor_type = self._guess_web_editor_type(f"{title} {browser_url}")
        if web_editor_type:
            return web_editor_type

        if name in {"winword.exe", "hwp.exe", "notepad.exe", "notepad++.exe", "notion.exe", "docs.exe"} or any(
            ext in title for ext in (".doc", ".txt", ".md")
        ):
            return "document"
        if name in {"powerpnt.exe"} or ".ppt" in title:
            return "presentation"
        if name in {"excel.exe"} or ".xls" in title:
            return "spreadsheet"
        if name in {"chrome.exe", "msedge.exe", "firefox.exe"}:
            return "browser"
        if name in {"code.exe", "devenv.exe", "pycharm64.exe"}:
            return "code_editor"
        if self._matches_custom_document_tool(name, title):
            return "document"
        return "unknown"

    def _matches_custom_document_tool(self, process_name: str, title: str) -> bool:
        """True when the active window matches a user-registered document tool.

        ``process_name`` and ``title`` are already lowercased by the caller.
        A token matches as a substring of either, so the user can register an
        exe name ("obsidian.exe") or a title keyword ("Obsidian")."""
        for token in self._custom_doc_tokens:
            if token in process_name or token in title:
                return True
        return False

    def _guess_web_editor_type(self, title: str) -> str:
        title = title.lower()
        if self._has_any(
            title,
            (
                "docs.google.com/spreadsheets",
                "google sheets",
                "google 스프레드시트",
                "구글 스프레드시트",
                "한컴 시트",
                "hancom sheet",
            ),
        ):
            return "spreadsheet"
        if self._has_any(
            title,
            (
                "docs.google.com/presentation",
                "google slides",
                "google 프레젠테이션",
                "구글 프레젠테이션",
                "한컴 슬라이드",
                "hancom slide",
                "hancom show",
            ),
        ):
            return "presentation"
        if self._has_any(
            title,
            (
                "docs.google.com/document",
                "google docs",
                "google 문서",
                "구글 문서",
                "docs.google.com",
                "hancomdocs",
                "hancom docs",
                "docs.hancom.com",
                "webhwp",
                "web hwp",
                "한컴독스",
                "한컴 문서",
                "한글 문서",
            ),
        ):
            return "document"
        return ""

    def _has_any(self, text: str, needles: tuple[str, ...]) -> bool:
        return any(needle in text for needle in needles)

    def _score_confidence(self, *, app_text: str, ui_text: str, ocr_text: str) -> float:
        if app_text:
            return 0.95
        if ui_text:
            return 0.9
        if ocr_text:
            return 0.55
        return 0.0

    def _is_usable_ui_source(self, ui_automation: UiAutomationResult | None) -> bool:
        if ui_automation is None:
            return False
        return bool(
            ui_automation.source_quality in {"primary", "usable"}
            and not ui_automation.error
        )

    def _resolve_current_paragraph(
        self,
        *,
        app_text: str,
        ui_text: str,
        ui_current_paragraph: str,
        ocr_text: str,
    ) -> str:
        if ui_current_paragraph:
            return ui_current_paragraph
        if ui_text:
            return ui_text
        if ocr_text and not app_text and not ui_text:
            return ocr_text
        if app_text and not ui_text:
            return app_text
        return ""

    def _resolve_current_paragraph_source(
        self,
        *,
        app_text: str,
        ui_text: str,
        ui_current_paragraph: str,
        ocr_text: str,
        ui_automation: UiAutomationResult | None,
    ) -> str:
        if ui_current_paragraph and ui_automation:
            source = ui_automation.current_paragraph_source or "text_range"
            return f"uia_{source}"
        if ui_text:
            return "uia_full_text_fallback"
        if ocr_text and not app_text and not ui_text:
            return "ocr_same_as_full_text"
        if app_text and not ui_text:
            return "app_text_same_as_full_text"
        return ""
