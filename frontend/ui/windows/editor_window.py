"""Standalone markdown editor window with inline AI ghost-writing.

A three-column layout modelled on the reference design (test/ mockup): a left
**document outline**, a centre **page** (markdown source on a Google-Docs-style
grey canvas, with an 편집/미리보기/분할 view toggle), and a right **AI 도우미**
panel (대화 / 빠른 작업 / 기록 tabs + connected-sources count). A full menu bar,
a markdown-syntax-insert toolbar, and a status bar complete the chrome.

Tech constraints (deliberate): the editor is a markdown **source** ``QTextEdit``
(not WYSIWYG), the preview reuses :func:`markdown_view.render_markdown_html`
(never Qt's buggy ``setMarkdown``), no new external deps, and all backend
traffic goes through ``api_client`` (HTTP/SSE) only — the window is never wired
to the main app with Qt signals for data; the host owns the instance and calls
:meth:`open_document`.
"""

from __future__ import annotations

import re

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
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
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...api_common import api_client, current_workspace_id
from ...controllers import (
    AgentController,
    EditorAssistWorker,
    EditorSuggestWorker,
    JobCategory,
    get_chat_bus,
    get_job_manager,
)
from ..markdown_view import render_markdown_html
from .document_assist_window import (
    CHAT_QSS,
    VERITAS_TITLEBAR_QSS,
    ChatInputBar,
    ChatPanel,
    TypingIndicator,
    VeritasTitleBar,
    render_history_html,
)


GHOST_COLOR = "#9AA0A6"

# Window-control glyphs. The previous plain-text symbols (－ ▢ ×) render as tofu
# in the UI font; these are the standard Private-Use codepoints in Windows'
# built-in icon fonts (Win11: "Segoe Fluent Icons", Win10: "Segoe MDL2 Assets"),
# which draw crisp minimise / maximise / restore / close marks.
WIN_GLYPH_MINIMIZE = ""
WIN_GLYPH_MAXIMIZE = ""
WIN_GLYPH_RESTORE = ""
WIN_GLYPH_CLOSE = ""


# QTextCursor.selectedText() encodes paragraph / line breaks as U+2029 / U+2028
# rather than "\n"; helpers normalise them to newlines.
PARAGRAPH_SEP = chr(0x2029)
LINE_SEP = chr(0x2028)

# Quick-action definitions: id → (button label, backend action, needs selection).
QUICK_ACTIONS = [
    ("다음 문장 제안", "continue", False),
    ("선택 영역 다시 쓰기", "rewrite", True),
    ("이 단락 요약", "summarize", True),
    ("문장 다듬기", "polish", True),
    ("문법 점검", "grammar", True),
]


def _normalise_selection(text: str) -> str:
    return text.replace(PARAGRAPH_SEP, "\n").replace(LINE_SEP, "\n")


def editor_tool_icon(kind: str, size: int = 18, color: str = "#3c4043") -> QIcon:
    """Crisp painter-drawn line icons for the editor toolbar — no asset files or
    font glyphs (which rendered as tofu / inconsistently before)."""
    scale = 2
    phys = size * scale
    pixmap = QPixmap(phys, phys)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    qcolor = QColor(color)
    pen = QPen(qcolor)
    pen.setWidthF(1.7 * scale)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    def pt(fx: float, fy: float) -> QPointF:
        return QPointF(fx * phys, fy * phys)

    def line(x1, y1, x2, y2) -> None:
        painter.drawLine(pt(x1, y1), pt(x2, y2))

    def poly(points) -> None:
        painter.drawPolyline([pt(x, y) for x, y in points])

    def dot(cx, cy, r=0.06) -> None:
        painter.save()
        painter.setBrush(qcolor)
        painter.drawEllipse(pt(cx, cy), r * phys, r * phys)
        painter.restore()

    if kind == "undo":
        path = QPainterPath()
        path.moveTo(pt(0.40, 0.30))
        path.cubicTo(pt(0.78, 0.30), pt(0.80, 0.72), pt(0.50, 0.74))
        painter.drawPath(path)
        poly([(0.40, 0.18), (0.26, 0.30), (0.40, 0.42)])
    elif kind == "redo":
        path = QPainterPath()
        path.moveTo(pt(0.60, 0.30))
        path.cubicTo(pt(0.22, 0.30), pt(0.20, 0.72), pt(0.50, 0.74))
        painter.drawPath(path)
        poly([(0.60, 0.18), (0.74, 0.30), (0.60, 0.42)])
    elif kind == "quote":
        painter.fillRect(QRectF(pt(0.24, 0.26), pt(0.31, 0.74)), qcolor)
        line(0.42, 0.36, 0.78, 0.36)
        line(0.42, 0.52, 0.70, 0.52)
        line(0.42, 0.68, 0.78, 0.68)
    elif kind == "ul":
        for yy in (0.30, 0.50, 0.70):
            dot(0.24, yy)
            line(0.40, yy, 0.80, yy)
    elif kind == "ol":
        painter.save()
        num_font = QFont(painter.font())
        num_font.setPixelSize(int(0.30 * phys))
        num_font.setBold(True)
        painter.setFont(num_font)
        for digit, yy in (("1", 0.30), ("2", 0.50), ("3", 0.70)):
            painter.drawText(QRectF(pt(0.12, yy - 0.13), pt(0.32, yy + 0.13)), Qt.AlignCenter, digit)
        painter.restore()
        for yy in (0.30, 0.50, 0.70):
            line(0.40, yy, 0.80, yy)
    elif kind == "task":
        painter.drawRoundedRect(QRectF(pt(0.18, 0.20), pt(0.40, 0.42)), 2 * scale, 2 * scale)
        poly([(0.22, 0.31), (0.28, 0.38), (0.38, 0.24)])
        line(0.50, 0.31, 0.82, 0.31)
        for yy in (0.58, 0.78):
            painter.drawRoundedRect(QRectF(pt(0.18, yy - 0.11), pt(0.40, yy + 0.11)), 2 * scale, 2 * scale)
            line(0.50, yy, 0.82, yy)
    elif kind == "link":
        painter.save()
        painter.translate(pt(0.50, 0.50))
        painter.rotate(-45)
        rw, rh = 0.30 * phys, 0.20 * phys
        painter.drawRoundedRect(QRectF(-rw, -rh / 2, rw, rh), rh / 2, rh / 2)
        painter.drawRoundedRect(QRectF(0.0, -rh / 2, rw, rh), rh / 2, rh / 2)
        painter.restore()
        line(0.40, 0.50, 0.60, 0.50)
    elif kind == "image":
        painter.drawRoundedRect(QRectF(pt(0.18, 0.24), pt(0.82, 0.76)), 3 * scale, 3 * scale)
        dot(0.36, 0.40, 0.05)
        poly([(0.24, 0.70), (0.42, 0.52), (0.56, 0.64), (0.66, 0.54), (0.78, 0.70)])
    elif kind == "table":
        painter.drawRoundedRect(QRectF(pt(0.18, 0.24), pt(0.82, 0.76)), 2 * scale, 2 * scale)
        line(0.18, 0.42, 0.82, 0.42)
        line(0.18, 0.59, 0.82, 0.59)
        line(0.44, 0.24, 0.44, 0.76)
        line(0.62, 0.24, 0.62, 0.76)
    elif kind == "code":
        poly([(0.38, 0.30), (0.22, 0.50), (0.38, 0.70)])
        poly([(0.62, 0.30), (0.78, 0.50), (0.62, 0.70)])
    elif kind == "hr":
        line(0.18, 0.50, 0.82, 0.50)
    elif kind == "footnote":
        path = QPainterPath()
        path.moveTo(pt(0.28, 0.16))
        path.lineTo(pt(0.56, 0.16))
        path.lineTo(pt(0.70, 0.30))
        path.lineTo(pt(0.70, 0.84))
        path.lineTo(pt(0.28, 0.84))
        path.closeSubpath()
        painter.drawPath(path)
        poly([(0.56, 0.16), (0.56, 0.30), (0.70, 0.30)])
        line(0.36, 0.52, 0.56, 0.52)
        line(0.36, 0.66, 0.62, 0.66)

    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return QIcon(pixmap)


class MarkdownSourceEdit(QTextEdit):
    """Markdown source editor with an inline ghost-writing suggestion + chip.

    The ghost suggestion is inserted into the document as real, grey-coloured
    text *after* the caret, so the text that follows the caret is pushed down and
    flows naturally after it (rather than being painted over). It is kept off the
    undo stack's user history by always being the topmost edit and removed with
    ``document().undo()``; ``document_text()`` returns the document without it so
    it never leaks into the preview / auto-save / counts. Tab accepts it (the
    grey text is re-inserted as normal text), Esc rejects it, any other keystroke
    dismisses it. While the model is still generating, a "작성 중" indicator shows
    at the caret; once text streams in, an accept/reject chip sits just below the
    suggestion's last character. Korean IME composition suppresses ghosts.
    """

    ghostAccepted = Signal()
    ghostDismissed = Signal()
    ghostRetryRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EditorSource")
        self.setAcceptRichText(False)
        self.setTabChangesFocus(False)
        self._ghost_text = ""
        self._ghost_anchor = -1  # caret position the suggestion is anchored at
        self._ghost_final = False  # True once streaming is done → show accept chip
        self._composing = False
        self._mutating = False  # guards self-inflicted ghost edits from reactions
        self._generating = False
        self._chip = self._build_chip()
        self._gen_chip = self._build_gen_chip()

    def _build_chip(self) -> QFrame:
        chip = QFrame(self.viewport())
        chip.setObjectName("GhostChip")
        chip.hide()
        layout = QHBoxLayout(chip)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(4)
        for label, slot in (
            ("Tab 수락", self.accept_ghost),
            ("Esc 거부", self._dismiss_ghost),
            ("다시", self._request_retry),
        ):
            button = QPushButton(label)
            button.setObjectName("GhostChipButton")
            button.setCursor(Qt.PointingHandCursor)
            # NoFocus so clicking the chip doesn't blur the editor (which would
            # otherwise clear the ghost via focusOutEvent before the click runs).
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(slot)
            layout.addWidget(button)
        return chip

    def _build_gen_chip(self) -> QFrame:
        chip = QFrame(self.viewport())
        chip.setObjectName("GhostGenChip")
        chip.hide()
        layout = QHBoxLayout(chip)
        layout.setContentsMargins(6, 2, 10, 2)
        layout.setSpacing(4)
        self._gen_indicator = TypingIndicator(chip)  # breathing Veritas logo
        label = QLabel("작성 중")
        label.setObjectName("GhostGenLabel")
        layout.addWidget(self._gen_indicator, 0, Qt.AlignVCenter)
        layout.addWidget(label, 0, Qt.AlignVCenter)
        return chip

    def _hide_gen_chip(self) -> None:
        self._gen_indicator.stop()
        self._gen_chip.hide()

    @staticmethod
    def _ghost_format() -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(GHOST_COLOR))
        return fmt

    # -- ghost state ----------------------------------------------------------

    def has_ghost(self) -> bool:
        return bool(self._ghost_text)

    def is_composing(self) -> bool:
        return self._composing

    def document_text(self) -> str:
        """The document's plain text *without* the pending grey ghost run, for
        preview / save / counts / outline / suggestion context."""
        full = self.toPlainText()
        if self._ghost_text and 0 <= self._ghost_anchor <= len(full):
            end = self._ghost_anchor + len(self._ghost_text)
            return full[: self._ghost_anchor] + full[end:]
        return full

    def _remove_ghost_run(self) -> None:
        """Drop the inserted grey run. It is always the topmost edit, so a single
        ``undo()`` removes it without disturbing the user's own undo history."""
        if not self._ghost_text:
            return
        self._mutating = True
        self.blockSignals(True)
        try:
            self.document().undo()
        finally:
            self.blockSignals(False)
            self._mutating = False
        self._ghost_text = ""

    def set_generating(self) -> None:
        """Show the "작성 중" indicator at the caret while the model streams its
        first tokens. Anchors the suggestion at the current caret."""
        self._remove_ghost_run()
        self._generating = True
        self._ghost_final = False
        self._ghost_anchor = self.textCursor().position()
        self._chip.hide()
        self._position_gen_chip()

    def set_ghost(self, text: str, *, final: bool = False) -> None:
        """Insert / grow the grey suggestion. While streaming (``final=False``)
        only the "작성 중" indicator trails the text; the Tab 수락 / Esc 거부 chip
        is shown only once the suggestion is complete (``final=True``)."""
        text = text or ""
        if not text:
            self.clear_ghost()
            return
        anchor = self._ghost_anchor if self._ghost_anchor >= 0 else self.textCursor().position()
        self._remove_ghost_run()
        anchor = max(0, min(anchor, len(self.toPlainText())))
        self._mutating = True
        self.blockSignals(True)
        try:
            cursor = self.textCursor()
            cursor.setPosition(anchor)
            # Own edit block → a separate undo command that never merges with the
            # user's preceding keystrokes, so document().undo() pops exactly this.
            cursor.beginEditBlock()
            cursor.insertText(text, self._ghost_format())
            cursor.endEditBlock()
            # Keep the caret *before* the suggestion so typing/Esc act there.
            caret = self.textCursor()
            caret.setPosition(anchor)
            self.setTextCursor(caret)
        finally:
            self.blockSignals(False)
            self._mutating = False
        self._ghost_text = text
        self._ghost_anchor = anchor
        self._ghost_final = final
        if final:
            self._generating = False
            self._hide_gen_chip()
            self._position_chip()  # accept/reject chip at the end
        else:
            # Still streaming: keep the trailing "작성 중" indicator, no chip yet.
            self._chip.hide()
            self._position_gen_chip(at_end=True)

    def _reset_ghost(self) -> None:
        self._remove_ghost_run()
        self._ghost_text = ""
        self._ghost_anchor = -1
        self._ghost_final = False
        self._generating = False
        self._chip.hide()
        self._hide_gen_chip()

    def clear_ghost(self) -> None:
        self._reset_ghost()

    def _dismiss_ghost(self) -> None:
        had = bool(self._ghost_text) or self._generating
        self._reset_ghost()
        if had:
            self.ghostDismissed.emit()

    def _request_retry(self) -> None:
        self._reset_ghost()
        self.ghostRetryRequested.emit()

    def accept_ghost(self) -> None:
        if not self._ghost_text:
            self._reset_ghost()
            return
        text = self._ghost_text
        anchor = self._ghost_anchor
        # Remove the grey run, then re-insert as normal text as a single, real
        # edit (signals on → the window marks dirty / refreshes preview).
        self._remove_ghost_run()
        self._ghost_text = ""
        self._ghost_anchor = -1
        self._ghost_final = False
        self._generating = False
        self._chip.hide()
        self._hide_gen_chip()
        cursor = self.textCursor()
        cursor.setPosition(max(0, min(anchor, len(self.toPlainText()))))
        cursor.beginEditBlock()
        cursor.insertText(text)
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.ghostAccepted.emit()

    def _position_chip(self) -> None:
        if not self._ghost_text:
            self._chip.hide()
            return
        # Sit just below the suggestion's last character.
        cursor = self.textCursor()
        cursor.setPosition(min(self._ghost_anchor + len(self._ghost_text), len(self.toPlainText())))
        rect = self.cursorRect(cursor)
        self._chip.adjustSize()
        x = min(rect.left(), max(0, self.viewport().width() - self._chip.width() - 4))
        y = rect.bottom() + 4
        if y + self._chip.height() > self.viewport().height():
            y = max(0, rect.top() - self._chip.height() - 4)
        self._chip.move(x, y)
        self._chip.show()
        self._chip.raise_()

    def _position_gen_chip(self, at_end: bool = False) -> None:
        if at_end and self._ghost_text:
            cursor = self.textCursor()
            cursor.setPosition(min(self._ghost_anchor + len(self._ghost_text), len(self.toPlainText())))
            rect = self.cursorRect(cursor)
        else:
            rect = self.cursorRect(self.textCursor())
        self._gen_chip.adjustSize()
        x = min(rect.left(), max(0, self.viewport().width() - self._gen_chip.width() - 4))
        y = rect.bottom() + 4
        if y + self._gen_chip.height() > self.viewport().height():
            y = max(0, rect.top() - self._gen_chip.height() - 4)
        self._gen_chip.move(x, y)
        self._gen_chip.show()
        self._gen_chip.raise_()
        self._gen_indicator.start()

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
            self._dismiss_ghost()
        elif self._generating:
            # A keystroke while still generating cancels the pending indicator;
            # the in-flight worker bails via its caret/token guard.
            self._reset_ghost()
        super().keyPressEvent(event)

    def inputMethodEvent(self, event) -> None:  # type: ignore[override]
        self._composing = bool(event.preeditString())
        if self._composing and (self._ghost_text or self._generating):
            self._dismiss_ghost()
        super().inputMethodEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if self._ghost_text or self._generating:
            self._dismiss_ghost()
        super().mousePressEvent(event)

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        self.clear_ghost()
        super().focusOutEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._ghost_text and self._ghost_final:
            self._position_chip()
        elif self._ghost_text or self._generating:
            self._position_gen_chip(at_end=bool(self._ghost_text))


class OutlinePanel(QFrame):
    """Left panel: an auto-extracted heading outline. Clicking jumps the caret."""

    headingClicked = Signal(int)  # block number
    closeRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("OutlinePanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("문서 개요")
        title.setObjectName("PanelHeaderTitle")
        close_button = QPushButton("✕")
        close_button.setObjectName("PanelHeaderClose")
        close_button.setFixedSize(22, 22)
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.clicked.connect(self.closeRequested.emit)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(close_button)
        layout.addLayout(header)

        self.list = QListWidget()
        self.list.setObjectName("OutlineList")
        self.list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.list, 1)

        self.empty = QLabel("# 제목을 추가하면\n여기에 목차가 표시됩니다.")
        self.empty.setObjectName("PanelEmpty")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        self.list.hide()

    def set_headings(self, headings: list[tuple[int, str, int]]) -> None:
        self.list.clear()
        if not headings:
            self.list.hide()
            self.empty.show()
            return
        self.empty.hide()
        self.list.show()
        for level, text, block in headings:
            item = QListWidgetItem(("    " * (level - 1)) + (text or "(제목 없음)"))
            item.setData(Qt.UserRole, block)
            self.list.addItem(item)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        block = item.data(Qt.UserRole)
        if isinstance(block, int):
            self.headingClicked.emit(block)


class QuickActionsTab(QWidget):
    """빠른 작업: one-click LLM transforms with an apply/copy/retry result card."""

    actionRequested = Signal(str)  # backend action id
    applyRequested = Signal()
    copyRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        hint = QLabel("한 번 클릭으로 자주 쓰는 작업을 실행합니다. 선택 영역이 없으면 현재 단락에 적용됩니다.")
        hint.setObjectName("PanelHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        for label, action, _needs in QUICK_ACTIONS:
            button = QPushButton(label)
            button.setObjectName("QuickActionButton")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda checked=False, a=action: self.actionRequested.emit(a))
            layout.addWidget(button)

        self.result = QTextEdit()
        self.result.setObjectName("AssistResult")
        self.result.setReadOnly(True)
        self.result.setPlaceholderText("결과가 여기에 표시됩니다.")
        layout.addWidget(self.result, 1)

        self.action_row = QFrame()
        row = QHBoxLayout(self.action_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.apply_button = QPushButton("본문에 대치")
        self.apply_button.setObjectName("PrimaryButton")
        self.copy_button = QPushButton("복사")
        self.copy_button.setObjectName("GhostButton")
        self.retry_button = QPushButton("다시")
        self.retry_button.setObjectName("GhostButton")
        for button in (self.apply_button, self.copy_button, self.retry_button):
            button.setCursor(Qt.PointingHandCursor)
        self.apply_button.clicked.connect(self.applyRequested.emit)
        self.copy_button.clicked.connect(self.copyRequested.emit)
        self.retry_button.clicked.connect(lambda: self.actionRequested.emit(self._last_action))
        row.addWidget(self.apply_button)
        row.addWidget(self.copy_button)
        row.addStretch(1)
        row.addWidget(self.retry_button)
        layout.addWidget(self.action_row)
        self.action_row.setVisible(False)
        self._last_action = "rewrite"

    def begin(self, action: str) -> None:
        self._last_action = action
        self.result.clear()
        self.result.setPlaceholderText("생성 중…")
        self.action_row.setVisible(False)

    def append(self, chunk: str) -> None:
        cursor = self.result.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(chunk)

    def end(self) -> None:
        self.action_row.setVisible(bool(self.result.toPlainText().strip()))

    def fail(self, message: str) -> None:
        self.result.setPlainText(f"[오류] {message}")
        self.action_row.setVisible(False)

    def result_text(self) -> str:
        return self.result.toPlainText().strip()


class ChatTab(QWidget):
    """대화: a shared chat surface. Reuses the assist window's ChatPanel +
    ChatInputBar so the editor's 문서 대화 is the *same* conversation as the main
    채팅 page — routed through the app-wide ChatBus (mode toggle included), while
    the open document rides along as additive context.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Mirror WritePage exactly: an AssistPagePanel card wrapping the shared
        # ChatPanel + ChatInputBar, so the editor 대화 looks like the main 채팅.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        wrapper = QFrame()
        wrapper.setObjectName("AssistPagePanel")
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(12, 12, 12, 12)
        wrapper_layout.setSpacing(10)

        self.panel = ChatPanel("문서 대화")
        self.input_bar = ChatInputBar()
        wrapper_layout.addWidget(self.panel, 1)
        wrapper_layout.addWidget(self.input_bar)
        layout.addWidget(wrapper, 1)


class AssistPanel(QFrame):
    """Right panel: 자료 count header + 대화 / 빠른 작업 / 기록 tabs."""

    closeRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AssistPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("AI 도우미")
        title.setObjectName("PanelHeaderTitle")
        self.sources_label = QLabel("자료 0건 연결됨")
        self.sources_label.setObjectName("PanelHint")
        close_button = QPushButton("✕")
        close_button.setObjectName("PanelHeaderClose")
        close_button.setFixedSize(22, 22)
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.clicked.connect(self.closeRequested.emit)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.sources_label)
        header.addWidget(close_button)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("AssistTabs")
        self.chat_tab = ChatTab()
        self.quick_tab = QuickActionsTab()
        self.history_list = QListWidget()
        self.history_list.setObjectName("HistoryList")
        history_wrap = QWidget()
        history_layout = QVBoxLayout(history_wrap)
        history_layout.setContentsMargins(10, 10, 10, 10)
        history_hint = QLabel("최근 실행한 작업이 여기에 표시됩니다.")
        history_hint.setObjectName("PanelHint")
        history_layout.addWidget(history_hint)
        history_layout.addWidget(self.history_list, 1)

        self.tabs.addTab(self.chat_tab, "대화")
        self.tabs.addTab(self.quick_tab, "빠른 작업")
        self.tabs.addTab(history_wrap, "기록")
        layout.addWidget(self.tabs, 1)

    def set_sources(self, count: int) -> None:
        self.sources_label.setText(f"자료 {count}건 연결됨")

    def add_history(self, label: str) -> None:
        self.history_list.insertItem(0, label)


class EditorWindow(QWidget):
    EXPORT_FORMATS = [
        ("Microsoft Word (.docx)", "docx", "Word 문서 (*.docx)", ".docx"),
        ("PDF (.pdf)", "pdf", "PDF 문서 (*.pdf)", ".pdf"),
        ("HTML (.html)", "html", "HTML 문서 (*.html)", ".html"),
        ("Markdown (.md)", "md", "Markdown 문서 (*.md)", ".md"),
    ]
    VIEW_MODES = [("편집", "edit"), ("미리보기", "preview"), ("분할", "split")]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Veritas 문서 작성")
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(1280, 820)
        self.setMinimumSize(900, 560)
        self.setMouseTracking(True)

        self._workspace_id = current_workspace_id()
        self._doc_id: str | None = None
        self._doc_title = "제목 없음"
        self._dirty = False
        self._loading = False
        self._autocomplete_enabled = True
        self._use_workspace_rag = True
        self._view_mode = "split"
        self._suggest_token = 0
        self._load_token = 0
        self._suggest_anchor = 0
        self._suggest_worker: EditorSuggestWorker | None = None
        self._assist_worker: EditorAssistWorker | None = None
        # Quick-action target: (start, end, is_insert) — where 본문에 대치 writes.
        self._assist_target: tuple[int, int, bool] | None = None

        # 문서 대화 shares the main chat's conversation through the app-wide
        # ChatBus + the persisted workspace chat history; mode mirrors the 채팅
        # page (rag/research). The open document is sent as additive context.
        self._chat_bus = get_chat_bus()
        self._chat_controller = AgentController()
        self._chat_mode = "rag"
        self._chat_streaming = False
        self._chat_history_token = 0

        self._resize_margin = 7
        self._resize_edges: set[str] = set()
        self._resize_origin: QPoint | None = None
        self._resize_geometry = None

        self._build_ui()
        self._apply_stylesheet()
        self._wire_timers()
        self._install_shortcuts()
        self._set_view_mode("split")

        get_job_manager().busy_changed.connect(self._sync_autocomplete_state)
        self._sync_autocomplete_state()

        self._connect_chat_bus()

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

        self.title_bar = VeritasTitleBar(self, subtitle=True)
        panel_layout.addWidget(self.title_bar)
        panel_layout.addWidget(self._build_menu_row())
        panel_layout.addWidget(self._build_toolbar())
        panel_layout.addWidget(self._build_body(), 1)
        panel_layout.addWidget(self._build_status_bar())

        root.addWidget(self.panel)

    def _menu_button(self, text: str) -> tuple[QToolButton, QMenu]:
        button = QToolButton()
        button.setText(text)
        button.setObjectName("EditorMenuButton")
        button.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(button)
        button.setMenu(menu)
        return button, menu

    def _build_menu_row(self) -> QWidget:
        row = QFrame()
        row.setObjectName("EditorMenuRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 3, 10, 3)
        layout.setSpacing(2)

        file_btn, file_menu = self._menu_button("파일")
        file_menu.addAction("새 문서", self.new_document)
        file_menu.addAction("자료조사 결과 불러오기", self.load_final_report)
        file_menu.addAction("저장된 초안 열기…", self.open_draft_dialog)
        file_menu.addSeparator()
        file_menu.addAction("저장", self.save_now)
        export_sub = file_menu.addMenu("내보내기")
        for label, fmt, _f, _e in self.EXPORT_FORMATS:
            export_sub.addAction(label, lambda checked=False, f=fmt: self.export_as(f))

        edit_btn, edit_menu = self._menu_button("편집")
        edit_menu.addAction("실행 취소", lambda: self.editor.undo())
        edit_menu.addAction("다시 실행", lambda: self.editor.redo())
        edit_menu.addSeparator()
        edit_menu.addAction("잘라내기", lambda: self.editor.cut())
        edit_menu.addAction("복사", lambda: self.editor.copy())
        edit_menu.addAction("붙여넣기", lambda: self.editor.paste())
        edit_menu.addAction("모두 선택", lambda: self.editor.selectAll())

        view_btn, view_menu = self._menu_button("보기")
        view_group = QActionGroup(self)
        self._view_actions = {}
        for label, mode in self.VIEW_MODES:
            action = QAction(label, self, checkable=True)
            action.triggered.connect(lambda checked=False, m=mode: self._set_view_mode(m))
            view_group.addAction(action)
            view_menu.addAction(action)
            self._view_actions[mode] = action
        view_menu.addSeparator()
        self._outline_action = QAction("문서 개요 표시", self, checkable=True, checked=True)
        self._outline_action.triggered.connect(self._toggle_outline)
        view_menu.addAction(self._outline_action)
        self._assist_action = QAction("도우미 패널 표시", self, checkable=True, checked=True)
        self._assist_action.triggered.connect(self._toggle_assist)
        view_menu.addAction(self._assist_action)

        insert_btn, insert_menu = self._menu_button("삽입")
        insert_menu.addAction("이미지", lambda: self.insert_markdown("image"))
        insert_menu.addAction("링크", lambda: self.insert_markdown("link"))
        insert_menu.addAction("표", lambda: self.insert_markdown("table"))
        insert_menu.addAction("구분선", lambda: self.insert_markdown("hr"))
        insert_menu.addAction("각주", lambda: self.insert_markdown("footnote"))

        format_btn, format_menu = self._menu_button("서식")
        for label, kind in (("제목 1", "h1"), ("제목 2", "h2"), ("제목 3", "h3")):
            format_menu.addAction(label, lambda checked=False, k=kind: self.insert_markdown(k))
        format_menu.addSeparator()
        for label, kind in (("굵게", "bold"), ("기울임", "italic"), ("취소선", "strike"), ("인용", "quote"), ("인라인 코드", "code")):
            format_menu.addAction(label, lambda checked=False, k=kind: self.insert_markdown(k))

        tools_btn, tools_menu = self._menu_button("도구")
        tools_menu.addAction("단어 수 보기", self._show_word_count)

        assist_btn, assist_menu = self._menu_button("도우미")
        self.autocomplete_action = QAction("자동완성 (고스트라이팅)", self, checkable=True, checked=True)
        self.autocomplete_action.toggled.connect(self._on_autocomplete_toggled)
        assist_menu.addAction(self.autocomplete_action)
        self.rag_action = QAction("자료 기반 제안 (RAG)", self, checkable=True, checked=True)
        self.rag_action.toggled.connect(self._on_rag_toggled)
        assist_menu.addAction(self.rag_action)
        assist_menu.addSeparator()
        for label, action, _needs in QUICK_ACTIONS:
            assist_menu.addAction(label, lambda checked=False, a=action: self.run_quick_action(a))
        assist_menu.addSeparator()
        assist_menu.addAction("자료조사 결과 불러오기", self.load_final_report)

        help_btn, help_menu = self._menu_button("도움말")
        help_menu.addAction("키보드 단축키", self._show_shortcuts)
        help_menu.addAction("정보", self._show_about)

        for button in (file_btn, edit_btn, view_btn, insert_btn, format_btn, tools_btn, assist_btn, help_btn):
            layout.addWidget(button)
        layout.addStretch(1)

        export_button = QToolButton()
        export_button.setText("내보내기  ⌄")
        export_button.setObjectName("EditorExportButton")
        export_button.setCursor(Qt.PointingHandCursor)
        export_button.setPopupMode(QToolButton.InstantPopup)
        export_menu = QMenu(export_button)
        for label, fmt, _f, _e in self.EXPORT_FORMATS:
            export_menu.addAction(label, lambda checked=False, f=fmt: self.export_as(f))
        export_button.setMenu(export_menu)
        layout.addWidget(export_button)
        return row

    def _build_toolbar(self) -> QWidget:
        row = QFrame()
        row.setObjectName("EditorToolbar")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(4)

        def add_sep() -> None:
            sep = QFrame()
            sep.setObjectName("EditorToolSep")
            sep.setFixedWidth(1)
            sep.setFixedHeight(18)
            layout.addWidget(sep)

        def add_text_button(label: str, tooltip: str, kind: str) -> None:
            button = QPushButton(label)
            button.setObjectName("EditorToolButton")
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(tooltip)
            button.setFixedSize(30, 28)
            button.clicked.connect(lambda checked=False, k=kind: self.insert_markdown(k))
            layout.addWidget(button)

        def add_icon_button(icon_kind: str, tooltip: str, handler) -> None:
            button = QPushButton()
            button.setObjectName("EditorIconButton")
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(tooltip)
            button.setFixedSize(30, 28)
            button.setIcon(editor_tool_icon(icon_kind))
            button.setIconSize(QSize(18, 18))
            button.clicked.connect(lambda checked=False, h=handler: h())
            layout.addWidget(button)

        # Left-panel (문서 개요) show/hide toggle, mirroring its position.
        self.outline_toggle_btn = QPushButton("◧ 개요")
        self.outline_toggle_btn.setObjectName("PanelToggleButton")
        self.outline_toggle_btn.setCheckable(True)
        self.outline_toggle_btn.setChecked(True)
        self.outline_toggle_btn.setCursor(Qt.PointingHandCursor)
        self.outline_toggle_btn.setToolTip("문서 개요 패널 열기/닫기")
        self.outline_toggle_btn.setFixedHeight(28)
        self.outline_toggle_btn.clicked.connect(lambda checked: self._toggle_outline(checked))
        layout.addWidget(self.outline_toggle_btn)
        add_sep()

        # 실행 취소 / 다시 실행 (self.editor is built later → call lazily)
        add_icon_button("undo", "실행 취소 (Ctrl+Z)", lambda: self.editor.undo())
        add_icon_button("redo", "다시 실행 (Ctrl+Y)", lambda: self.editor.redo())
        add_sep()

        # 단락 스타일 드롭다운
        style_button = QToolButton()
        style_button.setText("본문  ⌄")
        style_button.setObjectName("EditorStyleButton")
        style_button.setCursor(Qt.PointingHandCursor)
        style_button.setPopupMode(QToolButton.InstantPopup)
        style_menu = QMenu(style_button)
        for label, kind in (("제목 1", "h1"), ("제목 2", "h2"), ("제목 3", "h3"), ("인용구", "quote")):
            style_menu.addAction(label, lambda checked=False, k=kind: self.insert_markdown(k))
        style_button.setMenu(style_menu)
        layout.addWidget(style_button)
        add_sep()

        for label, tip, kind in (("H1", "제목 1", "h1"), ("H2", "제목 2", "h2"), ("H3", "제목 3", "h3")):
            add_text_button(label, tip, kind)
        add_sep()
        for label, tip, kind in (("B", "굵게", "bold"), ("I", "기울임", "italic"), ("S", "취소선", "strike")):
            add_text_button(label, tip, kind)
        add_sep()
        add_icon_button("quote", "인용", lambda: self.insert_markdown("quote"))
        add_icon_button("ul", "글머리 목록", lambda: self.insert_markdown("ul"))
        add_icon_button("ol", "번호 목록", lambda: self.insert_markdown("ol"))
        add_icon_button("task", "체크리스트", lambda: self.insert_markdown("task"))
        add_sep()
        add_icon_button("link", "링크", lambda: self.insert_markdown("link"))
        add_icon_button("image", "이미지", lambda: self.insert_markdown("image"))
        add_icon_button("table", "표", lambda: self.insert_markdown("table"))
        add_icon_button("code", "인라인 코드", lambda: self.insert_markdown("code"))
        add_icon_button("hr", "구분선", lambda: self.insert_markdown("hr"))
        add_icon_button("footnote", "각주", lambda: self.insert_markdown("footnote"))

        layout.addStretch(1)

        # View toggle: 편집 / 미리보기 / 분할
        self._view_buttons: dict[str, QPushButton] = {}
        self._view_group = QButtonGroup(self)
        self._view_group.setExclusive(True)
        for label, mode in self.VIEW_MODES:
            button = QPushButton(label)
            button.setObjectName("ViewToggleButton")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda checked=False, m=mode: self._set_view_mode(m))
            self._view_group.addButton(button)
            self._view_buttons[mode] = button
            layout.addWidget(button)

        add_sep()
        # Right-panel (AI 도우미) show/hide toggle, mirroring its position.
        self.assist_toggle_btn = QPushButton("도우미 ◨")
        self.assist_toggle_btn.setObjectName("PanelToggleButton")
        self.assist_toggle_btn.setCheckable(True)
        self.assist_toggle_btn.setChecked(True)
        self.assist_toggle_btn.setCursor(Qt.PointingHandCursor)
        self.assist_toggle_btn.setToolTip("AI 도우미 패널 열기/닫기")
        self.assist_toggle_btn.setFixedHeight(28)
        self.assist_toggle_btn.clicked.connect(lambda checked: self._toggle_assist(checked))
        layout.addWidget(self.assist_toggle_btn)
        return row

    def _build_body(self) -> QWidget:
        self.main_split = QSplitter(Qt.Horizontal)
        self.main_split.setObjectName("EditorMainSplit")
        self.main_split.setHandleWidth(1)

        self.outline_panel = OutlinePanel()
        self.outline_panel.headingClicked.connect(self._goto_block)
        self.outline_panel.closeRequested.connect(lambda: self._toggle_outline(False))

        # Centre canvas (grey) holding the page split.
        canvas = QFrame()
        canvas.setObjectName("EditorCanvas")
        canvas_layout = QVBoxLayout(canvas)
        canvas_layout.setContentsMargins(28, 22, 28, 22)
        canvas_layout.setSpacing(0)

        self.center_split = QSplitter(Qt.Horizontal)
        self.center_split.setObjectName("CenterSplit")
        self.center_split.setHandleWidth(18)

        self.editor = MarkdownSourceEdit()
        self.editor.setPlaceholderText("여기에 마크다운으로 작성하세요. 멈추면 회색 제안이 나타납니다 (Tab 수락 · Esc 거부).")
        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.ghostAccepted.connect(self._on_ghost_accepted)
        self.editor.ghostDismissed.connect(self._on_ghost_resolved)
        self.editor.ghostRetryRequested.connect(self._fire_suggestion)

        self.preview = QTextBrowser()
        self.preview.setObjectName("EditorPreview")
        self.preview.setOpenExternalLinks(True)

        self.edit_page = self._make_page(self.editor)
        self.preview_page = self._make_page(self.preview)
        self.center_split.addWidget(self.edit_page)
        self.center_split.addWidget(self.preview_page)
        self.center_split.setSizes([600, 560])
        canvas_layout.addWidget(self.center_split)

        self.assist_panel = AssistPanel()
        self.assist_panel.closeRequested.connect(lambda: self._toggle_assist(False))
        self.assist_panel.quick_tab.actionRequested.connect(self.run_quick_action)
        self.assist_panel.quick_tab.applyRequested.connect(self._apply_assist_result)
        self.assist_panel.quick_tab.copyRequested.connect(self._copy_assist_result)
        self.assist_panel.chat_tab.input_bar.sendRequested.connect(self._send_chat)
        self.assist_panel.chat_tab.input_bar.modeChanged.connect(self._set_chat_mode)

        self.main_split.addWidget(self.outline_panel)
        self.main_split.addWidget(canvas)
        self.main_split.addWidget(self.assist_panel)
        self.main_split.setStretchFactor(0, 0)
        self.main_split.setStretchFactor(1, 1)
        self.main_split.setStretchFactor(2, 0)
        self.main_split.setSizes([230, 740, 320])
        return self.main_split

    def _make_page(self, inner: QWidget) -> QFrame:
        page = QFrame()
        page.setObjectName("EditorPage")
        shadow = QGraphicsDropShadowEffect(page)
        shadow.setBlurRadius(18)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(15, 23, 42, 28))
        page.setGraphicsEffect(shadow)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(inner)
        return page

    def _build_status_bar(self) -> QWidget:
        row = QFrame()
        row.setObjectName("EditorStatusBar")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(12, 5, 12, 5)
        layout.setSpacing(16)

        self.status_save = QLabel("● 새 문서")
        self.status_save.setObjectName("EditorStatusItem")
        self.status_words = QLabel("단어 0")
        self.status_words.setObjectName("EditorStatusItem")
        self.status_count = QLabel("글자 0")
        self.status_count.setObjectName("EditorStatusItem")
        self.status_autocomplete = QLabel("자동완성: 켜짐")
        self.status_autocomplete.setObjectName("EditorStatusItem")
        lang = QLabel("한국어")
        lang.setObjectName("EditorStatusItem")

        layout.addWidget(self.status_save)
        layout.addWidget(self.status_words)
        layout.addWidget(self.status_count)
        layout.addStretch(1)
        layout.addWidget(self.status_autocomplete)
        layout.addWidget(lang)
        grip = QSizeGrip(self)
        layout.addWidget(grip, 0, Qt.AlignRight | Qt.AlignBottom)
        return row

    def _wire_timers(self) -> None:
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(180)
        self._preview_timer.timeout.connect(self._render_preview)

        self._outline_timer = QTimer(self)
        self._outline_timer.setSingleShot(True)
        self._outline_timer.setInterval(400)
        self._outline_timer.timeout.connect(self._refresh_outline)

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

    def open_document(
        self,
        workspace_id: str | None = None,
        source: str = "new",
        doc_id: str | None = None,
        seed_markdown: str | None = None,
    ) -> None:
        self._workspace_id = workspace_id or current_workspace_id()
        self._invalidate_suggestion()
        self.editor.clear_ghost()
        self._load_token += 1
        token = self._load_token

        # 초안 페이지의 "에디터로 보내기" — 백엔드 호출 없이 전달받은 마크다운으로 바로 시드.
        if seed_markdown is not None:
            title = self._title_from_markdown(seed_markdown)
            self._apply_loaded_document({"content": seed_markdown, "title": title, "source": "draft"})
            self._refresh_sources()
            self._refresh_chat_history()
            self.show()
            self.raise_()
            self.activateWindow()
            return

        params = {"workspaceId": self._workspace_id, "source": source, "docId": doc_id}

        def _load() -> dict:
            return api_client.get("/api/v1/editor/document", params)

        def _ok(data: object) -> None:
            if token == self._load_token:
                self._apply_loaded_document(data if isinstance(data, dict) else {})

        def _fail(message: str) -> None:
            if token == self._load_token:
                QMessageBox.warning(self, "불러오기 실패", f"문서를 불러오지 못했습니다.\n{message}")

        self.status_save.setText("● 불러오는 중…")
        get_job_manager().run_detached(_load, on_success=_ok, on_error=_fail)
        self._refresh_sources()
        self._refresh_chat_history()
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
        self.title_bar.set_subtitle(title)
        self._doc_title = title
        self._dirty = False
        self._render_preview()
        self._refresh_outline()
        self._update_counts()
        source = str(data.get("source") or "")
        self._set_save_status("새 문서" if source == "new" else "불러옴")

    @staticmethod
    def _title_from_markdown(markdown: str) -> str:
        for line in markdown.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:80]
        return "초안"

    def _refresh_sources(self) -> None:
        workspace_id = self._workspace_id

        def _load() -> dict:
            return api_client.get("/api/v1/editor/sources", {"workspaceId": workspace_id})

        def _ok(data: object) -> None:
            count = int((data or {}).get("count", 0)) if isinstance(data, dict) else 0
            self.assist_panel.set_sources(count)

        get_job_manager().run_detached(_load, on_success=_ok, on_error=lambda _m: None)

    # --------------------------------------------------------- text reactions

    def _on_text_changed(self) -> None:
        if self._loading:
            return
        self._dirty = True
        self._update_counts()
        self._preview_timer.start()
        self._outline_timer.start()
        self._autosave_timer.start()
        if self._autocomplete_enabled:
            self._suggest_timer.start()

    def _on_ghost_resolved(self) -> None:
        self._invalidate_suggestion()

    def _on_ghost_accepted(self) -> None:
        self._invalidate_suggestion()
        self.assist_panel.add_history("고스트 제안 수락")

    def _update_counts(self) -> None:
        text = self.editor.document_text()
        self.status_words.setText(f"단어 {len(text.split()):,}")
        self.status_count.setText(f"글자 {len(text):,}")

    def _render_preview(self) -> None:
        text = self.editor.document_text()
        html = render_markdown_html(text, font_size="15px")
        if html:
            self.preview.setHtml(html)
        else:
            self.preview.setPlainText(text)

    def _refresh_outline(self) -> None:
        headings: list[tuple[int, str, int]] = []
        for index, line in enumerate(self.editor.document_text().split("\n")):
            match = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
            if match:
                headings.append((len(match.group(1)), match.group(2).strip(), index))
        self.outline_panel.set_headings(headings)

    def _goto_block(self, block_number: int) -> None:
        doc = self.editor.document()
        block = doc.findBlockByNumber(block_number)
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        self.editor.setTextCursor(cursor)
        self.editor.centerCursor()
        self.editor.setFocus()

    # -------------------------------------------------------------- ghosting

    def _invalidate_suggestion(self) -> None:
        self._suggest_token += 1

    def _fire_suggestion(self) -> None:
        if not self._autocomplete_enabled or self.editor.is_composing():
            return
        if get_job_manager().is_blocked(JobCategory.EDITOR):
            return
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            return
        if self.editor.has_ghost():
            return  # a suggestion is already showing — don't stack another
        text = self.editor.document_text()
        pos = cursor.position()
        prefix = text[:pos][-500:]
        suffix = text[pos:][:200]
        if not prefix.strip():
            return

        self._suggest_token += 1
        token = self._suggest_token
        self._suggest_anchor = pos
        partial: list[str] = []
        worker = EditorSuggestWorker(self._workspace_id, prefix, suffix, 64, self._use_workspace_rag, self)

        def on_start(_sid: str) -> None:
            if token == self._suggest_token:
                # Show the "작성 중" indicator at the caret until the first token.
                self.editor.set_generating()

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
                self.editor.set_ghost(cleaned, final=True)
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

    # ----------------------------------------------------- quick actions (AI)

    def run_quick_action(self, action: str) -> None:
        if get_job_manager().is_blocked(JobCategory.EDITOR):
            QMessageBox.information(self, "사용 불가", "AutoSurvey가 진행 중일 때는 AI 작업을 사용할 수 없습니다.")
            return
        self.editor.clear_ghost()  # don't let a pending suggestion skew positions
        cursor = self.editor.textCursor()
        if action == "continue":
            pos = cursor.position()
            text = self.editor.document_text()[:pos][-800:]
            self._assist_target = (pos, pos, True)
        else:
            if cursor.hasSelection():
                text = _normalise_selection(cursor.selectedText())
                start, end = cursor.selectionStart(), cursor.selectionEnd()
            else:
                block = cursor.block()
                text = block.text()
                start, end = block.position(), block.position() + block.length() - 1
            self._assist_target = (start, end, False)
            if not text.strip():
                QMessageBox.information(self, "선택 필요", "변환할 텍스트를 선택하거나 단락에 커서를 두세요.")
                return

        self.assist_panel.tabs.setCurrentWidget(self.assist_panel.quick_tab)
        label = next((lbl for lbl, act, _ in QUICK_ACTIONS if act == action), action)
        self.assist_panel.quick_tab.begin(action)

        worker = EditorAssistWorker(self._workspace_id, action, text, 400, self._use_workspace_rag, self)
        worker.delta.connect(self.assist_panel.quick_tab.append)
        worker.completed.connect(lambda _full, lbl=label: self._on_assist_done(lbl))
        worker.failed.connect(self.assist_panel.quick_tab.fail)
        worker.finished.connect(worker.deleteLater)
        self._assist_worker = worker
        worker.start()

    def _on_assist_done(self, label: str) -> None:
        self.assist_panel.quick_tab.end()
        self.assist_panel.add_history(label)

    def _apply_assist_result(self) -> None:
        result = self.assist_panel.quick_tab.result_text()
        if not result or self._assist_target is None:
            return
        start, end, is_insert = self._assist_target
        cursor = self.editor.textCursor()
        if is_insert:
            cursor.setPosition(min(end, len(self.editor.toPlainText())))
            cursor.insertText(("\n" if result and not result.startswith("\n") else "") + result)
        else:
            cursor.setPosition(start)
            cursor.setPosition(min(end, len(self.editor.toPlainText())), QTextCursor.KeepAnchor)
            cursor.insertText(result)
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()

    def _copy_assist_result(self) -> None:
        result = self.assist_panel.quick_tab.result_text()
        if result:
            QApplication.clipboard().setText(result)

    # ------------------------------------------------------------- doc chat

    def _connect_chat_bus(self) -> None:
        bus = self._chat_bus
        bus.userMessageQueued.connect(self._on_chat_user_queued)
        bus.assistantStreamStarted.connect(self._on_chat_stream_started)
        bus.assistantChunk.connect(self._on_chat_stream_chunk)
        bus.assistantCompleted.connect(self._on_chat_stream_completed)
        bus.assistantFailed.connect(self._on_chat_stream_failed)
        get_job_manager().busy_changed.connect(self._sync_chat_busy_state)
        self._sync_chat_busy_state()

    def _sync_chat_busy_state(self) -> None:
        blocked = get_job_manager().is_blocked(JobCategory.CHAT)
        bar = self.assist_panel.chat_tab.input_bar
        bar.setEnabled(not blocked)
        if blocked:
            bar.input.setPlaceholderText("다른 작업이 진행 중입니다. 잠시만 기다려 주세요...")
        else:
            bar.set_mode(self._chat_mode, emit=False)

    def _set_chat_mode(self, mode: str) -> None:
        self._chat_mode = "rag" if mode == "rag" else "research"

    def _send_chat(self, message: str) -> None:
        text = (message or "").rstrip("\n").strip()
        if not text:
            return
        # The conversation is workspace-scoped and shared with the main 채팅
        # page; send through the ChatBus with the open document as additive
        # context and tag the turn as coming from the editor surface.
        self._workspace_id = current_workspace_id()
        doc_text = self.editor.document_text()
        if not self._chat_bus.send(
            self._workspace_id, text, self._chat_mode, doc_text=doc_text, source="editor"
        ):
            self.assist_panel.chat_tab.panel.add_message(
                "VERITAS", "이미 답변을 생성하고 있어요. 잠시만 기다려 주세요.", False
            )

    def _on_chat_user_queued(self, _workspace_id: str, text: str) -> None:
        self.assist_panel.chat_tab.panel.add_message("사용자", text, True)

    def _on_chat_stream_started(self) -> None:
        self._chat_streaming = True
        self.assist_panel.chat_tab.panel.start_streaming_assistant("VERITAS")

    def _on_chat_stream_chunk(self, chunk: str) -> None:
        if not self._chat_streaming:
            return
        self.assist_panel.chat_tab.panel.append_streaming_chunk(chunk)

    def _on_chat_stream_completed(self, text: str) -> None:
        if not self._chat_streaming:
            return
        self._chat_streaming = False
        self.assist_panel.chat_tab.panel.finalize_streaming_assistant(text)

    def _on_chat_stream_failed(self, error: str) -> None:
        if not self._chat_streaming:
            return
        self._chat_streaming = False
        self.assist_panel.chat_tab.panel.cancel_streaming_assistant(error)

    def _refresh_chat_history(self) -> None:
        panel = self.assist_panel.chat_tab.panel
        self._chat_streaming = False
        panel.clear_messages()
        panel.add_message("VERITAS", "채팅 기록을 불러오는 중입니다...", False)
        self._chat_history_token += 1
        token = self._chat_history_token
        workspace_id = self._workspace_id
        controller = self._chat_controller

        def _load() -> list:
            history = controller.get_chat_history(workspace_id)
            return render_history_html(history if isinstance(history, list) else [])

        def _apply(prepared: object) -> None:
            if token != self._chat_history_token:
                return
            self._render_chat_history(prepared if isinstance(prepared, list) else [])

        def _failed(_message: str) -> None:
            if token != self._chat_history_token:
                return
            self._render_chat_history([])

        get_job_manager().run_detached(_load, on_success=_apply, on_error=_failed)

    def _render_chat_history(self, prepared: list) -> None:
        panel = self.assist_panel.chat_tab.panel
        panel.clear_messages()
        if not prepared:
            panel.add_message(
                "VERITAS",
                "메시지를 입력하면 현재 워크스페이스의 대화가 메인 채팅과 공유됩니다.",
                False,
            )
            return
        for spec in prepared:
            if not isinstance(spec, dict):
                continue
            text = str(spec.get("text") or "")
            if not text:
                continue
            is_user = bool(spec.get("is_user"))
            panel.add_message(
                "사용자" if is_user else "VERITAS",
                text,
                is_user,
                rendered_html=spec.get("html") or None,
            )
        # Land on the most recent turn even if the 대화 tab is hidden right now
        # (its viewport can't measure heights until shown — force_scroll_bottom
        # also re-pins on the panel's showEvent).
        panel.force_scroll_bottom()

    # ------------------------------------------------------------- autosave

    def _autosave(self) -> None:
        if self._dirty and self._doc_id is not None:
            self._save(silent=True)

    def save_now(self) -> None:
        if self._doc_id is not None:
            self._save(silent=False)

    def _save(self, *, silent: bool) -> None:
        if self._doc_id is None:
            return
        workspace_id = self._workspace_id
        doc_id = self._doc_id
        content = self.editor.document_text()
        self.status_save.setText("● 저장 중…")

        def _do() -> dict:
            return api_client.post(
                "/api/v1/editor/document",
                {"workspaceId": workspace_id, "docId": doc_id, "content": content},
            )

        def _ok(data: object) -> None:
            self._dirty = False
            info = data if isinstance(data, dict) else {}
            if info.get("title"):
                self._doc_title = str(info.get("title"))
                self.title_bar.set_subtitle(self._doc_title)
            self._set_save_status("저장됨")

        def _fail(message: str) -> None:
            self._set_save_status("저장 실패")
            if not silent:
                QMessageBox.warning(self, "저장 실패", f"문서를 저장하지 못했습니다.\n{message}")

        get_job_manager().run_detached(_do, on_success=_ok, on_error=_fail)

    def _set_save_status(self, text: str) -> None:
        self.status_save.setText(f"● {text}")

    # --------------------------------------------------------------- export

    def export_as(self, fmt: str) -> None:
        if self.editor.document_text().strip() == "":
            QMessageBox.information(self, "내보내기", "내보낼 내용이 없습니다.")
            return
        spec = next((item for item in self.EXPORT_FORMATS if item[1] == fmt), None)
        if spec is None:
            return
        _label, _fmt, file_filter, ext = spec
        base_title = (getattr(self, "_doc_title", "") or "").strip() or "document"
        safe_title = re.sub(r"[^\w가-힣 .-]+", "_", base_title).strip() or "document"
        target, _ = QFileDialog.getSaveFileName(self, "내보내기", f"{safe_title}{ext}", file_filter)
        if not target:
            return
        workspace_id = self._workspace_id
        content = self.editor.document_text()

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
            if current is not None and current.data(Qt.UserRole):
                self.open_document(self._workspace_id, source="draft", doc_id=current.data(Qt.UserRole))

    # --------------------------------------------------- markdown insertion

    def insert_markdown(self, kind: str) -> None:
        cursor = self.editor.textCursor()
        selected = _normalise_selection(cursor.selectedText())

        line_prefixes = {"h1": "# ", "h2": "## ", "h3": "### ", "quote": "> ", "ul": "- ", "ol": "1. ", "task": "- [ ] "}
        wraps = {"bold": "**", "italic": "*", "code": "`", "strike": "~~"}

        if kind in line_prefixes:
            prefix = line_prefixes[kind]
            if selected:
                cursor.insertText("\n".join(prefix + line for line in selected.split("\n")))
            else:
                cursor.insertText(prefix)
        elif kind in wraps:
            mark = wraps[kind]
            body = selected or {"bold": "굵게", "italic": "기울임", "code": "코드", "strike": "취소선"}[kind]
            cursor.insertText(f"{mark}{body}{mark}")
        elif kind == "link":
            cursor.insertText(f"[{selected or '텍스트'}](https://)")
        elif kind == "image":
            cursor.insertText(f"![{selected or '설명'}](https://)")
        elif kind == "footnote":
            cursor.insertText("[^1]")
        elif kind == "table":
            cursor.insertText("\n| 항목 | 설명 |\n| --- | --- |\n| 1 | 내용 |\n| 2 | 내용 |\n\n")
        elif kind == "hr":
            cursor.insertText("\n\n---\n\n")

        self.editor.setTextCursor(cursor)
        self.editor.setFocus()

    # ----------------------------------------------------- view / panels

    def _set_view_mode(self, mode: str) -> None:
        self._view_mode = mode
        self.edit_page.setVisible(mode in ("edit", "split"))
        self.preview_page.setVisible(mode in ("preview", "split"))
        if mode in ("preview", "split"):
            self._render_preview()
        button = self._view_buttons.get(mode)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        action = self._view_actions.get(mode)
        if action is not None and not action.isChecked():
            action.setChecked(True)

    def _toggle_outline(self, checked: bool | None = None) -> None:
        visible = (not self.outline_panel.isVisible()) if checked is None else checked
        self.outline_panel.setVisible(visible)
        self._outline_action.setChecked(visible)
        self.outline_toggle_btn.setChecked(visible)

    def _toggle_assist(self, checked: bool | None = None) -> None:
        visible = (not self.assist_panel.isVisible()) if checked is None else checked
        self.assist_panel.setVisible(visible)
        self._assist_action.setChecked(visible)
        self.assist_toggle_btn.setChecked(visible)

    # ----------------------------------------------------- tools / help

    def _show_word_count(self) -> None:
        text = self.editor.document_text()
        QMessageBox.information(
            self, "단어 수",
            f"단어: {len(text.split()):,}\n글자(공백 포함): {len(text):,}\n글자(공백 제외): {len(text.replace(chr(32), '').replace(chr(10), '')):,}",
        )

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self, "키보드 단축키",
            "Ctrl+N 새 문서\nCtrl+S 저장\nTab 고스트 제안 수락\nEsc 고스트 제안 거부\nCtrl+Z/Y 실행취소/다시실행",
        )

    def _show_about(self) -> None:
        QMessageBox.information(self, "정보", "Veritas 문서 작성 — 마크다운 에디터 + AI 고스트라이팅")

    # ----------------------------------------------------- autocomplete state

    def _on_autocomplete_toggled(self, enabled: bool) -> None:
        self._autocomplete_enabled = enabled
        if not enabled:
            self._invalidate_suggestion()
            self.editor.clear_ghost()
        self._sync_autocomplete_state()

    def _on_rag_toggled(self, enabled: bool) -> None:
        # Workspace grounding for inline ghost-writing / 문서 대화. When off, the
        # editor's text is the only context sent to the model. Quick actions do
        # NOT follow this toggle: 다시 쓰기 / 다음 문장 제안 always force grounding
        # (and refuse without it), while 요약 / 다듬기 / 문법 never ground.
        self._use_workspace_rag = enabled

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

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._dirty and self._doc_id is not None:
            self._save(silent=True)
        super().closeEvent(event)

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
            QFrame#EditorPanel { background-color: #ffffff; border: 1px solid #dadce0; border-radius: 12px; }
            QFrame#EditorTitleBar {
                background-color: #f8f9fa; border-top-left-radius: 12px; border-top-right-radius: 12px;
                border-bottom: 1px solid #e8eaed;
            }
            QLabel#EditorDocTitle { font-size: 14px; font-weight: 700; color: #202124; }
            QLabel#EditorSaveStatus { font-size: 11px; font-weight: 600; color: #5f6368; }
            QPushButton#EditorWinButton, QPushButton#EditorCloseButton {
                background-color: transparent; color: #5f6368; border: none; border-radius: 6px;
                font-size: 14px; font-weight: 700;
            }
            QPushButton#EditorWinButton:hover { background-color: #e8eaed; color: #202124; }
            QPushButton#EditorCloseButton:hover { background-color: #fce8e6; color: #d93025; }
            QFrame#EditorMenuRow { background-color: #ffffff; border-bottom: 1px solid #f1f3f4; }
            QToolButton#EditorMenuButton {
                background-color: transparent; color: #3c4043; border: none; border-radius: 6px;
                padding: 5px 9px; font-weight: 600;
            }
            QToolButton#EditorMenuButton:hover { background-color: #f1f3f4; }
            QToolButton#EditorMenuButton::menu-indicator { image: none; width: 0; }
            QToolButton#EditorExportButton {
                background-color: #0b57d0; color: #ffffff; border: none; border-radius: 8px;
                padding: 6px 14px; font-weight: 700;
            }
            QToolButton#EditorExportButton:hover { background-color: #1967d2; }
            QToolButton#EditorExportButton::menu-indicator { image: none; width: 0; }
            QFrame#EditorToolbar { background-color: #ffffff; border-bottom: 1px solid #e8eaed; }
            QPushButton#EditorToolButton {
                background-color: #ffffff; color: #3c4043; border: 1px solid #e8eaed; border-radius: 6px;
                padding: 0px 9px; font-weight: 700; min-width: 22px;
            }
            QPushButton#EditorToolButton:hover { background-color: #f1f3f4; border-color: #dadce0; }
            QPushButton#EditorIconButton {
                background-color: #ffffff; border: 1px solid #e8eaed; border-radius: 6px; padding: 0px;
            }
            QPushButton#EditorIconButton:hover { background-color: #f1f3f4; border-color: #dadce0; }
            QToolButton#EditorStyleButton {
                background-color: #ffffff; color: #3c4043; border: 1px solid #e8eaed; border-radius: 6px;
                padding: 3px 10px; font-weight: 600;
            }
            QToolButton#EditorStyleButton:hover { background-color: #f1f3f4; border-color: #dadce0; }
            QToolButton#EditorStyleButton::menu-indicator { image: none; width: 0; }
            QFrame#EditorToolSep { background-color: #e8eaed; }
            QPushButton#ViewToggleButton {
                background-color: #ffffff; color: #5f6368; border: 1px solid #dadce0; border-radius: 6px;
                padding: 3px 12px; font-weight: 600;
            }
            QPushButton#ViewToggleButton:checked { background-color: #e8f0fe; color: #0b57d0; border-color: #0b57d0; }
            QPushButton#PanelToggleButton {
                background-color: #ffffff; color: #5f6368; border: 1px solid #dadce0; border-radius: 6px;
                padding: 3px 10px; font-weight: 600;
            }
            QPushButton#PanelToggleButton:checked { background-color: #e8f0fe; color: #0b57d0; border-color: #0b57d0; }
            QPushButton#PanelToggleButton:hover { background-color: #f1f3f4; }
            QSplitter#EditorMainSplit::handle { background-color: #e8eaed; }
            QSplitter#CenterSplit { background-color: #f6f7f9; }
            QSplitter#CenterSplit::handle { background-color: #f6f7f9; }
            QFrame#EditorCanvas { background-color: #f6f7f9; }
            QFrame#EditorPage { background-color: #ffffff; border: 1px solid #e8eaed; border-radius: 4px; }
            QTextEdit#EditorSource {
                background-color: #ffffff; color: #202124; border: none; border-radius: 4px;
                padding: 30px 40px; font-size: 15px;
                selection-background-color: #d3e3fd; selection-color: #202124;
            }
            QTextBrowser#EditorPreview {
                background-color: #ffffff; color: #202124; border: none; border-radius: 4px; padding: 24px 32px;
            }
            QFrame#OutlinePanel, QFrame#AssistPanel { background-color: #ffffff; }
            QFrame#OutlinePanel { border-right: 1px solid #e8eaed; }
            QFrame#AssistPanel { border-left: 1px solid #e8eaed; }
            QLabel#PanelHeaderTitle { font-size: 13px; font-weight: 800; color: #202124; }
            QLabel#PanelHint { font-size: 11px; color: #80868b; }
            QLabel#PanelEmpty { font-size: 12px; color: #9aa0a6; padding: 18px; }
            QPushButton#PanelHeaderClose {
                background-color: transparent; color: #5f6368; border: none; border-radius: 6px; font-weight: 700;
            }
            QPushButton#PanelHeaderClose:hover { background-color: #e8eaed; color: #202124; }
            QListWidget#OutlineList, QListWidget#HistoryList {
                background-color: #ffffff; border: 1px solid #e8eaed; border-radius: 8px; padding: 4px;
            }
            QListWidget#OutlineList::item, QListWidget#HistoryList::item { padding: 6px 8px; border-radius: 6px; color: #3c4043; }
            QListWidget#OutlineList::item:hover, QListWidget#HistoryList::item:hover { background-color: #f1f3f4; }
            QListWidget#OutlineList::item:selected, QListWidget#HistoryList::item:selected { background-color: #e8f0fe; color: #0b57d0; }
            QTabWidget#AssistTabs::pane { border: 1px solid #e8eaed; border-radius: 8px; top: -1px; }
            QTabBar::tab {
                background-color: #f1f3f4; color: #5f6368; padding: 6px 14px; border-top-left-radius: 8px;
                border-top-right-radius: 8px; font-weight: 600; margin-right: 2px;
            }
            QTabBar::tab:selected { background-color: #ffffff; color: #0b57d0; border: 1px solid #e8eaed; border-bottom: none; }
            QPushButton#QuickActionButton {
                background-color: #ffffff; color: #3c4043; border: 1px solid #dadce0; border-radius: 8px;
                padding: 9px 12px; font-weight: 600; text-align: left;
            }
            QPushButton#QuickActionButton:hover { background-color: #f8f9fa; border-color: #0b57d0; color: #0b57d0; }
            QTextEdit#AssistResult {
                background-color: #f8f9fa; color: #202124; border: 1px solid #e8eaed; border-radius: 8px; padding: 10px;
            }
            QTextEdit#AssistChatInput {
                background-color: #f8f9fa; border: 1px solid #dadce0; border-radius: 10px; padding: 8px 10px; color: #202124;
            }
            QTextEdit#AssistChatInput:focus { background-color: #ffffff; border-color: #0b57d0; }
            QPushButton#PrimaryButton {
                background-color: #0b57d0; color: #ffffff; border: none; border-radius: 8px; padding: 8px 14px; font-weight: 700;
            }
            QPushButton#PrimaryButton:hover { background-color: #1967d2; }
            QPushButton#GhostButton {
                background-color: #ffffff; color: #3c4043; border: 1px solid #dadce0; border-radius: 8px; padding: 8px 12px; font-weight: 600;
            }
            QPushButton#GhostButton:hover { background-color: #f1f3f4; }
            QFrame#GhostChip { background-color: #202124; border-radius: 8px; }
            QPushButton#GhostChipButton {
                background-color: transparent; color: #e8eaed; border: none; padding: 2px 6px; font-size: 11px; font-weight: 600;
            }
            QPushButton#GhostChipButton:hover { color: #ffffff; }
            QFrame#GhostGenChip { background-color: #f1f3f4; border: 1px solid #e0e3e7; border-radius: 10px; }
            QLabel#GhostGenLabel { color: #5f6368; font-size: 11px; font-weight: 600; background: transparent; }
            QFrame#EditorStatusBar {
                background-color: #f8f9fa; border-top: 1px solid #e8eaed;
                border-bottom-left-radius: 12px; border-bottom-right-radius: 12px;
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
            + VERITAS_TITLEBAR_QSS
            + CHAT_QSS
        )


if __name__ == "__main__":
    app = QApplication([])
    window = EditorWindow()
    window.open_document(source="new")
    window.show()
    app.exec()
