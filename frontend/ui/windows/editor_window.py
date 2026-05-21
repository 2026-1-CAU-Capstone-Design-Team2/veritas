"""Standalone markdown editor window with inline AI ghost-writing.

Window pattern is cloned from :mod:`document_assist_window` (a frameless
top-level with a custom title bar, drag-move, and edge-resize). The editor is a
**markdown source** ``QTextEdit`` on the left and a live HTML preview on the
right (the preview reuses :func:`markdown_view.render_markdown_html`, never
Qt's ``setMarkdown`` — that mis-renders GFM tables).

All backend traffic goes through ``api_client`` (HTTP/SSE) only — the window is
never wired to the main app with Qt signals for data. The host (`MainWindow`)
merely owns the instance and calls :meth:`open_document`.

Design language follows the reference mockup: Pretendard, a Google-Docs-style
grey palette, a slim file/assist menu, a markdown-syntax-insert toolbar (not a
rich-text formatting toolbar), an export dropdown, and a status bar.
"""

from __future__ import annotations

import re

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFontMetrics,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizeGrip,
    QSplitter,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...api_common import api_client, current_workspace_id
from ...controllers import EditorSuggestWorker, JobCategory, get_job_manager
from ..markdown_view import render_markdown_html


GHOST_COLOR = "#9AA0A6"
# QTextCursor.selectedText() encodes paragraph / line breaks as U+2029 / U+2028
# rather than "\n"; the markdown-insert helper normalises them to newlines.
PARAGRAPH_SEP = chr(0x2029)
LINE_SEP = chr(0x2028)


class MarkdownSourceEdit(QTextEdit):
    """Plain markdown source editor with an inline ghost-writing overlay.

    The ghost suggestion is painted as grey text after the caret without being
    inserted into the document, so it never leaks into the preview or the
    auto-saved file. Tab accepts it (commits the text), Esc rejects it, and any
    other keystroke dismisses it. Korean IME composition suppresses ghosts —
    a suggestion must never fire mid-composition.
    """

    ghostAccepted = Signal()
    ghostDismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EditorSource")
        self.setAcceptRichText(False)
        self.setTabChangesFocus(False)
        self._ghost_text = ""
        self._composing = False

    # -- ghost state ----------------------------------------------------------

    def has_ghost(self) -> bool:
        return bool(self._ghost_text)

    def is_composing(self) -> bool:
        return self._composing

    def set_ghost(self, text: str) -> None:
        self._ghost_text = text or ""
        self.viewport().update()

    def clear_ghost(self) -> None:
        """Programmatic clear (e.g. blocked by AutoSurvey) — no user signal."""
        if self._ghost_text:
            self._ghost_text = ""
            self.viewport().update()

    def _dismiss_ghost(self) -> None:
        had = bool(self._ghost_text)
        self._ghost_text = ""
        self.viewport().update()
        if had:
            self.ghostDismissed.emit()

    def accept_ghost(self) -> None:
        text = self._ghost_text
        self._ghost_text = ""
        if text:
            cursor = self.textCursor()
            cursor.insertText(text)
            self.setTextCursor(cursor)
        self.viewport().update()
        self.ghostAccepted.emit()

    # -- input handling -------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        if self._ghost_text:
            key = event.key()
            if key == Qt.Key_Tab:
                event.accept()
                self.accept_ghost()
                return
            if key == Qt.Key_Escape:
                event.accept()
                self._dismiss_ghost()
                return
            # Enter / typing / navigation all dismiss the ghost, then proceed
            # with normal handling (Enter still inserts a newline).
            self._dismiss_ghost()
        super().keyPressEvent(event)

    def inputMethodEvent(self, event) -> None:  # type: ignore[override]
        self._composing = bool(event.preeditString())
        if self._composing and self._ghost_text:
            self._dismiss_ghost()
        super().inputMethodEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if self._ghost_text:
            self._dismiss_ghost()
        super().mousePressEvent(event)

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        # Clear without emitting the user-dismiss signal: losing focus is not a
        # rejection, just a reason to stop drawing the overlay.
        self.clear_ghost()
        super().focusOutEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if not self._ghost_text or self._composing:
            return
        self._paint_ghost()

    def _paint_ghost(self) -> None:
        painter = QPainter(self.viewport())
        painter.setFont(self.font())
        painter.setPen(QColor(GHOST_COLOR))
        metrics = QFontMetrics(self.font())
        rect = self.cursorRect(self.textCursor())
        left_margin = int(self.document().documentMargin())
        right_limit = max(left_margin + 20, self.viewport().width() - left_margin)
        line_height = metrics.height()

        x = rect.left()
        y = rect.top()
        # Tokenise so wrapping happens on whitespace; explicit newlines in the
        # suggestion start a fresh line at the left margin.
        for token in re.split(r"(\s+)", self._ghost_text):
            if not token:
                continue
            if "\n" in token:
                for index, segment in enumerate(token.split("\n")):
                    if index > 0:
                        x = left_margin
                        y += line_height
                    if segment:
                        width = metrics.horizontalAdvance(segment)
                        if x + width > right_limit and x > left_margin:
                            x = left_margin
                            y += line_height
                        painter.drawText(x, y + metrics.ascent(), segment)
                        x += width
                continue
            width = metrics.horizontalAdvance(token)
            if x + width > right_limit and x > left_margin:
                x = left_margin
                y += line_height
            painter.drawText(x, y + metrics.ascent(), token)
            x += width
        painter.end()


class EditorTitleBar(QFrame):
    """Custom frameless title bar: drag to move, min/max/close, doc title."""

    def __init__(self, window: "EditorWindow", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._window = window
        self._drag_start: QPoint | None = None
        self.setObjectName("EditorTitleBar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 10, 8)
        layout.setSpacing(8)

        self.title = QLabel("제목 없음")
        self.title.setObjectName("EditorDocTitle")
        self.save_status = QLabel("새 문서")
        self.save_status.setObjectName("EditorSaveStatus")

        self.minimize_button = QPushButton("－")
        self.minimize_button.setObjectName("EditorWinButton")
        self.maximize_button = QPushButton("▢")
        self.maximize_button.setObjectName("EditorWinButton")
        self.close_button = QPushButton("×")
        self.close_button.setObjectName("EditorCloseButton")
        for button in (self.minimize_button, self.maximize_button, self.close_button):
            button.setFixedSize(30, 28)
            button.setCursor(Qt.PointingHandCursor)
        self.minimize_button.clicked.connect(window.showMinimized)
        self.maximize_button.clicked.connect(window.toggle_max_restore)
        self.close_button.clicked.connect(window.close)

        layout.addWidget(self.title)
        layout.addSpacing(8)
        layout.addWidget(self.save_status)
        layout.addStretch(1)
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        self._window.toggle_max_restore()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and not self._window.isMaximized():
            self._drag_start = event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._drag_start is not None and event.buttons() & Qt.LeftButton:
            self._window.move(event.globalPosition().toPoint() - self._drag_start)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        self._drag_start = None
        super().mouseReleaseEvent(event)


class EditorWindow(QWidget):
    EXPORT_FORMATS = [
        ("Microsoft Word (.docx)", "docx", "Word 문서 (*.docx)", ".docx"),
        ("PDF (.pdf)", "pdf", "PDF 문서 (*.pdf)", ".pdf"),
        ("HTML (.html)", "html", "HTML 문서 (*.html)", ".html"),
        ("Markdown (.md)", "md", "Markdown 문서 (*.md)", ".md"),
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Veritas 문서 작성")
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(1120, 760)
        self.setMinimumSize(760, 520)
        self.setMouseTracking(True)

        self._workspace_id = current_workspace_id()
        self._doc_id: str | None = None
        self._dirty = False
        self._loading = False
        self._autocomplete_enabled = True
        self._suggest_token = 0
        self._load_token = 0
        self._suggest_anchor = 0
        self._suggest_worker: EditorSuggestWorker | None = None

        # Frameless-window manual resize state (ported from DocumentAssistWindow).
        self._resize_margin = 7
        self._resize_edges: set[str] = set()
        self._resize_origin: QPoint | None = None
        self._resize_geometry = None

        self._build_ui()
        self._apply_stylesheet()
        self._wire_timers()
        self._install_shortcuts()

        get_job_manager().busy_changed.connect(self._sync_autocomplete_state)
        self._sync_autocomplete_state()

    # ------------------------------------------------------------------ build

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(0)

        self.panel = QFrame()
        self.panel.setObjectName("EditorPanel")
        shadow = QGraphicsDropShadowEffect(self.panel)
        shadow.setBlurRadius(26)
        shadow.setXOffset(0)
        shadow.setYOffset(8)
        shadow.setColor(QColor(15, 23, 42, 40))
        self.panel.setGraphicsEffect(shadow)

        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)

        self.title_bar = EditorTitleBar(self)
        panel_layout.addWidget(self.title_bar)
        panel_layout.addWidget(self._build_menu_row())
        panel_layout.addWidget(self._build_markdown_toolbar())
        panel_layout.addWidget(self._build_body(), 1)
        panel_layout.addWidget(self._build_status_bar())

        root.addWidget(self.panel)

    def _build_menu_row(self) -> QWidget:
        row = QFrame()
        row.setObjectName("EditorMenuRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(4)

        file_button = QToolButton()
        file_button.setText("파일")
        file_button.setObjectName("EditorMenuButton")
        file_button.setPopupMode(QToolButton.InstantPopup)
        file_menu = QMenu(file_button)
        file_menu.addAction("새 문서", self.new_document)
        file_menu.addAction("자료조사 결과 불러오기", self.load_final_report)
        file_menu.addAction("저장된 초안 열기…", self.open_draft_dialog)
        file_menu.addSeparator()
        file_menu.addAction("저장", self.save_now)
        file_button.setMenu(file_menu)

        assist_button = QToolButton()
        assist_button.setText("도우미")
        assist_button.setObjectName("EditorMenuButton")
        assist_button.setPopupMode(QToolButton.InstantPopup)
        assist_menu = QMenu(assist_button)
        self.autocomplete_action = QAction("자동완성 (고스트라이팅)", self)
        self.autocomplete_action.setCheckable(True)
        self.autocomplete_action.setChecked(True)
        self.autocomplete_action.toggled.connect(self._on_autocomplete_toggled)
        assist_menu.addAction(self.autocomplete_action)
        assist_menu.addAction("자료조사 결과 불러오기", self.load_final_report)
        assist_button.setMenu(assist_menu)

        layout.addWidget(file_button)
        layout.addWidget(assist_button)
        layout.addStretch(1)

        # 내보내기 dropdown (top-right per spec)
        export_button = QToolButton()
        export_button.setText("내보내기  ⌄")
        export_button.setObjectName("EditorExportButton")
        export_button.setCursor(Qt.PointingHandCursor)
        export_button.setPopupMode(QToolButton.InstantPopup)
        export_menu = QMenu(export_button)
        for label, fmt, _filter, _ext in self.EXPORT_FORMATS:
            export_menu.addAction(label, lambda checked=False, f=fmt: self.export_as(f))
        export_button.setMenu(export_menu)
        layout.addWidget(export_button)

        return row

    def _build_markdown_toolbar(self) -> QWidget:
        row = QFrame()
        row.setObjectName("EditorToolbar")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(4)

        def add_button(label: str, tooltip: str, kind: str) -> None:
            button = QPushButton(label)
            button.setObjectName("EditorToolButton")
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(tooltip)
            button.setFixedHeight(28)
            button.clicked.connect(lambda checked=False, k=kind: self.insert_markdown(k))
            layout.addWidget(button)

        def add_sep() -> None:
            sep = QFrame()
            sep.setObjectName("EditorToolSep")
            sep.setFixedWidth(1)
            sep.setFixedHeight(18)
            layout.addWidget(sep)

        add_button("H1", "제목 1", "h1")
        add_button("H2", "제목 2", "h2")
        add_button("H3", "제목 3", "h3")
        add_sep()
        add_button("B", "굵게", "bold")
        add_button("I", "기울임", "italic")
        add_button("〃", "인용", "quote")
        add_sep()
        add_button("•", "글머리 목록", "ul")
        add_button("1.", "번호 목록", "ol")
        add_button("☑", "체크리스트", "task")
        add_sep()
        add_button("<>", "인라인 코드", "code")
        add_button("🔗", "링크", "link")
        add_button("⊞", "표 삽입", "table")
        add_button("―", "구분선", "hr")
        layout.addStretch(1)
        return row

    def _build_body(self) -> QWidget:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setObjectName("EditorSplitter")
        splitter.setHandleWidth(6)

        self.editor = MarkdownSourceEdit()
        self.editor.setPlaceholderText("여기에 마크다운으로 작성하세요. 타이핑을 멈추면 회색 제안이 나타납니다 (Tab 수락 · Esc 거부).")
        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.ghostAccepted.connect(self._on_ghost_resolved)
        self.editor.ghostDismissed.connect(self._on_ghost_resolved)

        self.preview = QTextBrowser()
        self.preview.setObjectName("EditorPreview")
        self.preview.setOpenExternalLinks(True)

        left = QFrame()
        left.setObjectName("EditorPane")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.editor)

        right = QFrame()
        right.setObjectName("EditorPane")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.preview)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([560, 540])
        return splitter

    def _build_status_bar(self) -> QWidget:
        row = QFrame()
        row.setObjectName("EditorStatusBar")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(12, 5, 12, 5)
        layout.setSpacing(14)

        self.status_save = QLabel("● 새 문서")
        self.status_save.setObjectName("EditorStatusItem")
        self.status_count = QLabel("0자")
        self.status_count.setObjectName("EditorStatusItem")
        self.status_autocomplete = QLabel("자동완성: 켜짐")
        self.status_autocomplete.setObjectName("EditorStatusItem")

        layout.addWidget(self.status_save)
        layout.addWidget(self.status_count)
        layout.addStretch(1)
        layout.addWidget(self.status_autocomplete)

        grip = QSizeGrip(self)
        layout.addWidget(grip, 0, Qt.AlignRight | Qt.AlignBottom)
        return row

    def _wire_timers(self) -> None:
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(180)
        self._preview_timer.timeout.connect(self._render_preview)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(5000)
        self._autosave_timer.timeout.connect(self._autosave)

        self._suggest_timer = QTimer(self)
        self._suggest_timer.setSingleShot(True)
        self._suggest_timer.setInterval(300)
        self._suggest_timer.timeout.connect(self._fire_suggestion)

    def _install_shortcuts(self) -> None:
        self._sc_save = QShortcut(QKeySequence.Save, self)
        self._sc_save.activated.connect(self.save_now)
        self._sc_new = QShortcut(QKeySequence.New, self)
        self._sc_new.activated.connect(self.new_document)

    # ----------------------------------------------------------- public open

    def open_document(self, workspace_id: str | None = None, source: str = "new", doc_id: str | None = None) -> None:
        self._workspace_id = workspace_id or current_workspace_id()
        self._invalidate_suggestion()
        self.editor.clear_ghost()
        self._load_token += 1
        token = self._load_token
        params = {"workspaceId": self._workspace_id, "source": source, "docId": doc_id}

        def _load() -> dict:
            return api_client.get("/api/v1/editor/document", params)

        def _ok(data: object) -> None:
            if token != self._load_token:
                return
            self._apply_loaded_document(data if isinstance(data, dict) else {})

        def _fail(message: str) -> None:
            if token != self._load_token:
                return
            QMessageBox.warning(self, "불러오기 실패", f"문서를 불러오지 못했습니다.\n{message}")

        self.status_save.setText("● 불러오는 중…")
        get_job_manager().run_detached(_load, on_success=_ok, on_error=_fail)
        self.show()
        self.raise_()
        self.activateWindow()

    def _apply_loaded_document(self, data: dict) -> None:
        self._doc_id = str(data.get("docId") or "") or None
        title = str(data.get("title") or "제목 없음")
        content = str(data.get("content") or "")
        self._loading = True
        self.editor.setPlainText(content)
        self._loading = False
        self.title_bar.title.setText(title)
        self._dirty = False
        self._render_preview()
        self._update_char_count()
        source = str(data.get("source") or "")
        self._set_save_status("새 문서" if source == "new" else "불러옴")

    # --------------------------------------------------------- text reactions

    def _on_text_changed(self) -> None:
        if self._loading:
            return
        self._dirty = True
        self._update_char_count()
        self._preview_timer.start()
        self._autosave_timer.start()
        if self._autocomplete_enabled:
            self._suggest_timer.start()

    def _on_ghost_resolved(self) -> None:
        # A ghost was accepted or dismissed by the user → invalidate any
        # in-flight suggestion so a late stream chunk can't re-show it.
        self._invalidate_suggestion()

    def _update_char_count(self) -> None:
        self.status_count.setText(f"{len(self.editor.toPlainText()):,}자")

    def _render_preview(self) -> None:
        text = self.editor.toPlainText()
        html = render_markdown_html(text, font_size="15px")
        if html:
            self.preview.setHtml(html)
        else:
            # markdown package unavailable — show raw source rather than Qt's
            # buggy setMarkdown table rendering.
            self.preview.setPlainText(text)

    # -------------------------------------------------------------- ghosting

    def _invalidate_suggestion(self) -> None:
        self._suggest_token += 1

    def _fire_suggestion(self) -> None:
        if not self._autocomplete_enabled:
            return
        if self.editor.is_composing():
            return
        if get_job_manager().is_blocked(JobCategory.EDITOR):
            return
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            return
        text = self.editor.toPlainText()
        pos = cursor.position()
        prefix = text[:pos][-500:]
        suffix = text[pos:][:200]
        if not prefix.strip():
            return

        self._suggest_token += 1
        token = self._suggest_token
        self._suggest_anchor = pos
        partial: list[str] = []

        worker = EditorSuggestWorker(self._workspace_id, prefix, suffix, 64, self)

        def on_start(_sid: str) -> None:
            if token == self._suggest_token:
                self.editor.clear_ghost()

        def on_delta(chunk: str) -> None:
            if token != self._suggest_token:
                return
            if self.editor.textCursor().position() != self._suggest_anchor:
                return
            partial.append(chunk)
            self.editor.set_ghost("".join(partial).lstrip("\n"))

        def on_completed(full: str) -> None:
            if token != self._suggest_token:
                return
            if self.editor.textCursor().position() != self._suggest_anchor:
                self.editor.clear_ghost()
                return
            cleaned = (full or "").strip("\n")
            if cleaned.strip():
                self.editor.set_ghost(cleaned)
            else:
                self.editor.clear_ghost()

        def on_failed(_err: str) -> None:
            if token == self._suggest_token:
                self.editor.clear_ghost()

        worker.started_stream.connect(on_start)
        worker.delta.connect(on_delta)
        worker.completed.connect(on_completed)
        worker.failed.connect(on_failed)
        worker.finished.connect(worker.deleteLater)
        self._suggest_worker = worker
        worker.start()

    # ------------------------------------------------------------- autosave

    def _autosave(self) -> None:
        if not self._dirty or self._doc_id is None:
            return
        self._save(silent=True)

    def save_now(self) -> None:
        if self._doc_id is None:
            return
        self._save(silent=False)

    def _save(self, *, silent: bool) -> None:
        if self._doc_id is None:
            return
        workspace_id = self._workspace_id
        doc_id = self._doc_id
        content = self.editor.toPlainText()
        self.status_save.setText("● 저장 중…")

        def _do() -> dict:
            return api_client.post(
                "/api/v1/editor/document",
                {"workspaceId": workspace_id, "docId": doc_id, "content": content},
            )

        def _ok(data: object) -> None:
            self._dirty = False
            info = data if isinstance(data, dict) else {}
            title = str(info.get("title") or "")
            if title:
                self.title_bar.title.setText(title)
            self._set_save_status("저장됨")

        def _fail(message: str) -> None:
            self._set_save_status("저장 실패")
            if not silent:
                QMessageBox.warning(self, "저장 실패", f"문서를 저장하지 못했습니다.\n{message}")

        get_job_manager().run_detached(_do, on_success=_ok, on_error=_fail)

    def _set_save_status(self, text: str) -> None:
        self.status_save.setText(f"● {text}")
        self.title_bar.save_status.setText(text)

    # --------------------------------------------------------------- export

    def export_as(self, fmt: str) -> None:
        if self.editor.toPlainText().strip() == "":
            QMessageBox.information(self, "내보내기", "내보낼 내용이 없습니다.")
            return
        spec = next((item for item in self.EXPORT_FORMATS if item[1] == fmt), None)
        if spec is None:
            return
        _label, _fmt, file_filter, ext = spec
        base_title = self.title_bar.title.text().strip() or "document"
        safe_title = re.sub(r"[^\w가-힣 .-]+", "_", base_title).strip() or "document"
        target, _ = QFileDialog.getSaveFileName(self, "내보내기", f"{safe_title}{ext}", file_filter)
        if not target:
            return
        workspace_id = self._workspace_id
        content = self.editor.toPlainText()

        def _do() -> dict:
            return api_client.post(
                "/api/v1/editor/export",
                {"workspaceId": workspace_id, "content": content, "format": fmt, "outputPath": target},
            )

        def _ok(data: object) -> None:
            path = (data or {}).get("path", target) if isinstance(data, dict) else target
            QMessageBox.information(self, "내보내기 완료", f"저장되었습니다:\n{path}")

        def _fail(message: str) -> None:
            QMessageBox.warning(self, "내보내기 실패", message)

        get_job_manager().run_detached(_do, on_success=_ok, on_error=_fail)

    # ----------------------------------------------------------- file menu

    def new_document(self) -> None:
        self.open_document(self._workspace_id, source="new")

    def load_final_report(self) -> None:
        self.open_document(self._workspace_id, source="final")

    def open_draft_dialog(self) -> None:
        workspace_id = self._workspace_id

        def _load() -> dict:
            return api_client.get("/api/v1/editor/documents", {"workspaceId": workspace_id})

        def _ok(data: object) -> None:
            items = (data or {}).get("items", []) if isinstance(data, dict) else []
            self._show_draft_picker(items if isinstance(items, list) else [])

        def _fail(message: str) -> None:
            QMessageBox.warning(self, "초안 목록 실패", f"초안 목록을 불러오지 못했습니다.\n{message}")

        get_job_manager().run_detached(_load, on_success=_ok, on_error=_fail)

    def _show_draft_picker(self, items: list) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("저장된 초안 열기")
        dialog.setModal(True)
        dialog.resize(460, 320)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        label = QLabel("열 초안을 선택하세요")
        label.setObjectName("CardPrimary")
        layout.addWidget(label)

        listing = QListWidget()
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("docId") or "제목 없음")
            updated = str(item.get("updatedAt") or "")
            entry = QListWidgetItem(f"{title}    ·    {updated[:19]}")
            entry.setData(Qt.UserRole, str(item.get("docId") or ""))
            listing.addItem(entry)
        layout.addWidget(listing, 1)
        if not items:
            label.setText("저장된 초안이 없습니다.")

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        open_btn = buttons.addButton("열기", QDialogButtonBox.AcceptRole)
        open_btn.setObjectName("PrimaryButton")
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.Accepted:
            current = listing.currentItem()
            if current is not None:
                doc_id = current.data(Qt.UserRole)
                if doc_id:
                    self.open_document(self._workspace_id, source="draft", doc_id=doc_id)

    # --------------------------------------------------- markdown insertion

    def insert_markdown(self, kind: str) -> None:
        cursor = self.editor.textCursor()
        # selectedText() encodes line breaks as U+2029 / U+2028; normalise to
        # "\n" so multi-line prefixing splits on real newlines.
        selected = cursor.selectedText().replace(PARAGRAPH_SEP, "\n").replace(LINE_SEP, "\n")

        line_prefixes = {"h1": "# ", "h2": "## ", "h3": "### ", "quote": "> ", "ul": "- ", "ol": "1. ", "task": "- [ ] "}
        wraps = {"bold": "**", "italic": "*", "code": "`"}

        if kind in line_prefixes:
            prefix = line_prefixes[kind]
            if selected:
                cursor.insertText("\n".join(prefix + line for line in selected.split("\n")))
            else:
                cursor.insertText(prefix)
        elif kind in wraps:
            mark = wraps[kind]
            body = selected or {"bold": "굵게", "italic": "기울임", "code": "코드"}[kind]
            cursor.insertText(f"{mark}{body}{mark}")
        elif kind == "link":
            label = selected or "텍스트"
            cursor.insertText(f"[{label}](https://)")
        elif kind == "table":
            cursor.insertText("\n| 항목 | 설명 |\n| --- | --- |\n| 1 | 내용 |\n| 2 | 내용 |\n\n")
        elif kind == "hr":
            cursor.insertText("\n\n---\n\n")

        self.editor.setTextCursor(cursor)
        self.editor.setFocus()

    # ----------------------------------------------------- autocomplete state

    def _on_autocomplete_toggled(self, enabled: bool) -> None:
        self._autocomplete_enabled = enabled
        if not enabled:
            self._invalidate_suggestion()
            self.editor.clear_ghost()
        self._sync_autocomplete_state()

    def _sync_autocomplete_state(self) -> None:
        blocked = get_job_manager().is_blocked(JobCategory.EDITOR)
        if blocked:
            self._invalidate_suggestion()
            self.editor.clear_ghost()
            self.status_autocomplete.setText("자동완성: AutoSurvey 중 비활성")
        elif not self._autocomplete_enabled:
            self.status_autocomplete.setText("자동완성: 꺼짐")
        else:
            self.status_autocomplete.setText("자동완성: 켜짐")

    # --------------------------------------------------------- window chrome

    def toggle_max_restore(self) -> None:
        if self.isMaximized():
            self.showNormal()
            self.title_bar.maximize_button.setText("▢")
        else:
            self.showMaximized()
            self.title_bar.maximize_button.setText("❐")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        # Best-effort flush of unsaved edits, then hide (the host keeps the
        # instance so reopening is instant and the draft persists).
        if self._dirty and self._doc_id is not None:
            self._save(silent=True)
        super().closeEvent(event)

    # -- frameless edge resize (ported from DocumentAssistWindow) -------------

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and not self.isMaximized():
            edges = self._hit_resize_edges(event.position().toPoint())
            if edges:
                self._resize_edges = edges
                self._resize_origin = event.globalPosition().toPoint()
                self._resize_geometry = self.geometry()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._resize_origin is not None and self._resize_geometry is not None:
            self._resize_to(event.globalPosition().toPoint())
            event.accept()
            return
        self._update_resize_cursor(event.position().toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        self._resize_edges = set()
        self._resize_origin = None
        self._resize_geometry = None
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def _hit_resize_edges(self, pos: QPoint) -> set[str]:
        margin = self._resize_margin
        edges: set[str] = set()
        if pos.x() <= margin:
            edges.add("left")
        elif pos.x() >= self.width() - margin:
            edges.add("right")
        if pos.y() <= margin:
            edges.add("top")
        elif pos.y() >= self.height() - margin:
            edges.add("bottom")
        return edges

    def _update_resize_cursor(self, pos: QPoint) -> None:
        edges = self._hit_resize_edges(pos)
        if {"left", "top"} <= edges or {"right", "bottom"} <= edges:
            self.setCursor(Qt.SizeFDiagCursor)
        elif {"right", "top"} <= edges or {"left", "bottom"} <= edges:
            self.setCursor(Qt.SizeBDiagCursor)
        elif "left" in edges or "right" in edges:
            self.setCursor(Qt.SizeHorCursor)
        elif "top" in edges or "bottom" in edges:
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.unsetCursor()

    def _resize_to(self, global_pos: QPoint) -> None:
        if self._resize_origin is None or self._resize_geometry is None:
            return
        delta = global_pos - self._resize_origin
        geometry = self._resize_geometry
        x, y, width, height = geometry.x(), geometry.y(), geometry.width(), geometry.height()
        min_width, min_height = self.minimumWidth(), self.minimumHeight()
        if "left" in self._resize_edges:
            new_width = max(min_width, width - delta.x())
            x = geometry.right() - new_width + 1
            width = new_width
        if "right" in self._resize_edges:
            width = max(min_width, width + delta.x())
        if "top" in self._resize_edges:
            new_height = max(min_height, height - delta.y())
            y = geometry.bottom() - new_height + 1
            height = new_height
        if "bottom" in self._resize_edges:
            height = max(min_height, height + delta.y())
        self.setGeometry(x, y, width, height)

    # ------------------------------------------------------------ stylesheet

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                font-family: 'Pretendard', 'Segoe UI Variable', 'Segoe UI', 'Malgun Gothic', 'Noto Sans KR', sans-serif;
                font-size: 13px;
                color: #202124;
            }
            QFrame#EditorPanel {
                background-color: #ffffff;
                border: 1px solid #dadce0;
                border-radius: 12px;
            }
            QFrame#EditorTitleBar {
                background-color: #f8f9fa;
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
                border-bottom: 1px solid #e8eaed;
            }
            QLabel#EditorDocTitle { font-size: 14px; font-weight: 700; color: #202124; }
            QLabel#EditorSaveStatus { font-size: 11px; font-weight: 600; color: #5f6368; }
            QPushButton#EditorWinButton, QPushButton#EditorCloseButton {
                background-color: transparent; color: #5f6368; border: none;
                border-radius: 6px; font-size: 14px; font-weight: 700;
            }
            QPushButton#EditorWinButton:hover { background-color: #e8eaed; color: #202124; }
            QPushButton#EditorCloseButton:hover { background-color: #fce8e6; color: #d93025; }
            QFrame#EditorMenuRow {
                background-color: #ffffff;
                border-bottom: 1px solid #f1f3f4;
            }
            QToolButton#EditorMenuButton {
                background-color: transparent; color: #3c4043; border: none;
                border-radius: 6px; padding: 5px 10px; font-weight: 600;
            }
            QToolButton#EditorMenuButton:hover { background-color: #f1f3f4; }
            QToolButton#EditorExportButton {
                background-color: #0b57d0; color: #ffffff; border: none;
                border-radius: 8px; padding: 6px 14px; font-weight: 700;
            }
            QToolButton#EditorExportButton:hover { background-color: #1967d2; }
            QToolButton#EditorExportButton::menu-indicator { image: none; width: 0; }
            QFrame#EditorToolbar {
                background-color: #ffffff;
                border-bottom: 1px solid #e8eaed;
            }
            QPushButton#EditorToolButton {
                background-color: #ffffff; color: #3c4043; border: 1px solid #e8eaed;
                border-radius: 6px; padding: 0px 9px; font-weight: 700; min-width: 22px;
            }
            QPushButton#EditorToolButton:hover { background-color: #f1f3f4; border-color: #dadce0; }
            QFrame#EditorToolSep { background-color: #e8eaed; }
            QSplitter#EditorSplitter { background-color: #f6f7f9; }
            QSplitter#EditorSplitter::handle { background-color: #e8eaed; }
            QFrame#EditorPane { background-color: #f6f7f9; }
            QTextEdit#EditorSource {
                background-color: #ffffff; color: #202124;
                border: none; border-right: 1px solid #e8eaed;
                padding: 18px 22px; font-size: 15px;
                selection-background-color: #d3e3fd; selection-color: #202124;
            }
            QTextBrowser#EditorPreview {
                background-color: #ffffff; color: #202124;
                border: none; padding: 14px 20px;
            }
            QFrame#EditorStatusBar {
                background-color: #f8f9fa;
                border-top: 1px solid #e8eaed;
                border-bottom-left-radius: 12px;
                border-bottom-right-radius: 12px;
            }
            QLabel#EditorStatusItem { font-size: 11px; font-weight: 600; color: #5f6368; }
            QMenu { background-color: #ffffff; border: 1px solid #dadce0; border-radius: 8px; padding: 6px; }
            QMenu::item { color: #202124; padding: 7px 26px 7px 12px; border-radius: 6px; }
            QMenu::item:selected { background-color: #e8f0fe; color: #0b57d0; }
            QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
            QScrollBar::handle:vertical { background: #bdc1c6; border-radius: 4px; min-height: 28px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
            """
        )


if __name__ == "__main__":
    app = QApplication([])
    window = EditorWindow()
    window.open_document(source="new")
    window.show()
    app.exec()
