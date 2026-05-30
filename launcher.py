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

from core.stdio_utf8 import force_utf8_stdio
from db.db import get_app_data_dir
from PySide6.QtCore import (
    QByteArray,
    QObject,
    QPoint,
    QRectF,
    QSize,
    Qt,
    QThread,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
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


_SCREEN_DEBUG: bool = False


def screen_debug_enabled() -> bool:
    return _SCREEN_DEBUG


def configure_screen_debug_from_argv() -> None:
    """``--screen-debug``: stream ONLY the screen pipeline's ``[screen_debug]``
    trace (candidate scenarios, the LLM prompt, the steps run) to the console —
    suppressing every other log and the per-capture noise.

    Forces console streaming on (we need the API child's stdout piped so it can
    be filtered) and sets ``VERITAS_SCREEN_TRACE=1`` so the child emits the
    focused trace. Must run after :func:`configure_console_logs_from_argv` so it
    can override ``_CONSOLE_LOGS`` even when ``--console-logs`` was not passed."""
    global _SCREEN_DEBUG, _CONSOLE_LOGS
    if "--screen-debug" in sys.argv:
        _SCREEN_DEBUG = True
        _CONSOLE_LOGS = True
        os.environ["VERITAS_SCREEN_TRACE"] = "1"
    while "--screen-debug" in sys.argv:
        sys.argv.remove("--screen-debug")


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


# ---------------------------------------------------------------------------
# Setup-dialog presentation (front-end only).
#
# Reproduces launcher_ref.html: a frameless, rounded "card" dialog with a dark
# gradient header, a rich model selector, info rows, a status/progress box and a
# footer. Only the *look* changed here — the model list, disk checks and the
# download flow further down behave exactly as before.
# ---------------------------------------------------------------------------

_ICON_RENDER_SCALE = 3  # render oversized then downscale for crisp edges


def _svg_pixmap(markup: str, size: int) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(markup.encode("utf-8")))
    scaled = size * _ICON_RENDER_SCALE
    pixmap = QPixmap(scaled, scaled)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, scaled, scaled))
    painter.end()
    pixmap.setDevicePixelRatio(_ICON_RENDER_SCALE)
    return pixmap


def _line_icon(body: str, color: str, *, width: float = 1.8) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="{width}" stroke-linecap="round" '
        f'stroke-linejoin="round">{body}</svg>'
    )


def _short_path(path: Path) -> str:
    """``C:\\Users\\me\\AppData\\Roaming\\VERITAS\\models`` -> ``C:\\…\\VERITAS\\models``."""
    parts = path.parts
    if len(parts) <= 4:
        return str(path)
    return f"{parts[0]}…{os.sep}{parts[-2]}{os.sep}{parts[-1]}"


_ICON_MODEL = (
    '<rect x="4" y="4" width="16" height="16" rx="3"></rect>'
    '<path d="M9 9h6v6H9z"></path>'
    '<path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"></path>'
)
_ICON_ATOM = (
    '<circle cx="12" cy="12" r="3"></circle>'
    '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1'
    'M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"></path>'
)
_ICON_DB = (
    '<ellipse cx="12" cy="6" rx="8" ry="3"></ellipse>'
    '<path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6'
    'M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"></path>'
)
_ICON_LOCK = (
    '<rect x="4" y="10" width="16" height="10" rx="2"></rect>'
    '<path d="M8 10V7a4 4 0 0 1 8 0v3"></path>'
)
_ICON_DOWNLOAD = '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"></path>'
_ICON_CARET = '<path d="M6 9l6 6 6-6"></path>'
_ICON_LOGO_V = '<path d="M4 4l8 16L20 4"></path>'


_DIALOG_QSS = """
* { font-family: "Pretendard Variable", "Pretendard", "Malgun Gothic",
    "Segoe UI", sans-serif; }

#dialog { background: #FFFFFF; border: 1px solid #D7DCE5; border-radius: 18px; }

#dhead {
    border-top-left-radius: 17px; border-top-right-radius: 17px;
    background: qlineargradient(x1:0, y1:0, x2:0.35, y2:1,
        stop:0 #1B2A49, stop:0.55 #121A2E, stop:1 #0B1120);
}
#logo {
    border-radius: 13px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2E5BFF, stop:1 #5B83FF);
}
#headTitle { color: #FFFFFF; font-size: 19px; font-weight: 800; }
#headSub { color: #9FB0CC; font-size: 13px; }
#closeBtn {
    color: #9FB0CC; font-size: 14px; font-weight: 700;
    background: rgba(255, 255, 255, 0.06); border: none; border-radius: 9px;
}
#closeBtn:hover { background: rgba(255, 255, 255, 0.16); color: #FFFFFF; }

#dbody { background: #FFFFFF; }
#lead { color: #475569; font-size: 14px; }
#fieldLabel { color: #8A94A6; font-size: 12px; font-weight: 700; }

#combo { background: #FBFCFE; border: 1px solid #D7DCE5; border-radius: 12px; }
#combo:hover { border: 1px solid #B9C5DE; }
#micon { background: #EEF3FF; border-radius: 9px; }
#comboName { color: #0E1726; font-size: 15px; font-weight: 700; }
#comboMeta { color: #8A94A6; font-size: 13px; }

#row { background: #FFFFFF; border: 1px solid #E6E9EF; border-radius: 12px; }
#riBlue { background: #EEF3FF; border-radius: 9px; }
#riSlate { background: #EEF1F6; border-radius: 9px; }
#rowLabel { color: #0E1726; font-size: 14px; font-weight: 600; }
#rowSub { color: #8A94A6; font-size: 12px; }
#rowValue { color: #475569; font-size: 14px; font-weight: 700; }

#statusbox { background: #FBFCFE; border: 1px solid #E6E9EF; border-radius: 12px; }
#statusText { color: #475569; font-size: 14px; font-weight: 600; }
#pleft, #pright { color: #8A94A6; font-size: 12px; }

QProgressBar#pbar {
    background: #E9EDF4; border: none; border-radius: 5px;
    min-height: 8px; max-height: 8px;
}
QProgressBar#pbar::chunk {
    border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2E5BFF, stop:1 #5B83FF);
}

#dfoot {
    border-top: 1px solid #E6E9EF;
    border-bottom-left-radius: 17px; border-bottom-right-radius: 17px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFFFFF, stop:1 #FBFCFE);
}
#hintText { color: #8A94A6; font-size: 12px; }

#cancelBtn {
    background: #FFFFFF; color: #475569; border: 1px solid #D7DCE5;
    border-radius: 11px; font-size: 14px; font-weight: 700; padding: 11px 20px;
}
#cancelBtn:hover { background: #F4F6FA; color: #0E1726; border: 1px solid #C7CFDC; }

#installBtn {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3463FF, stop:1 #2E5BFF);
    color: #FFFFFF; border: none; border-radius: 11px;
    font-size: 14px; font-weight: 700; padding: 11px 20px;
}
#installBtn:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3E6CFF, stop:1 #3463FF);
}
#installBtn:disabled { background: #A9BEFF; color: #EAF0FF; }

QMenu#modelMenu {
    background: #FFFFFF; border: 1px solid #D7DCE5; border-radius: 10px; padding: 6px;
}
QMenu#modelMenu::item {
    padding: 9px 14px; border-radius: 7px; color: #0E1726; font-size: 13px;
}
QMenu#modelMenu::item:selected { background: #EEF3FF; color: #1E40C8; }
"""


class _DragHeader(QFrame):
    """Header strip that drags the frameless dialog (mirrors the app windows)."""

    def __init__(self, window: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._window = window
        self._drag_start: QPoint | None = None

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._drag_start = (
                event.globalPosition().toPoint()
                - self._window.frameGeometry().topLeft()
            )
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


class _ClickableFrame(QFrame):
    """Framed row that opens the model menu when clicked."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class ModelSetupDialog(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VERITAS Model Setup")
        # Frameless, translucent rounded card — the same chrome convention the
        # app's main / editor / assist windows use.
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet(_DIALOG_QSS)

        self._settings = load_settings()
        self._selected_embedding_id = DEFAULT_EMBEDDING_MODEL_ID
        selected_llm = selected_model_from_settings(self._settings)
        selected_embedding = selected_embedding_from_settings(self._settings)
        self._selected_embedding_id = selected_embedding.id

        # Hidden data model behind the styled selector: keeps selected_llm_id()
        # and the currentIndexChanged -> refresh wiring identical to before.
        self.model_combo = QComboBox(self)
        for spec in llm_models():
            installed = "installed" if find_model_file(spec) else "not installed"
            self.model_combo.addItem(
                f"{spec.short_name} - {bytes_label(spec.size_bytes)} - {installed}",
                spec.id,
            )
        index = max(0, self.model_combo.findData(selected_llm.id))
        self.model_combo.setCurrentIndex(index)
        self.model_combo.hide()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 34, 48, 64)
        outer.setSpacing(0)

        card = QFrame()
        card.setObjectName("dialog")
        card.setFixedWidth(560)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(44)
        shadow.setXOffset(0)
        shadow.setYOffset(18)
        shadow.setColor(QColor(12, 18, 32, 80))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        card_layout.addWidget(self._build_header())
        card_layout.addWidget(self._build_body(selected_embedding))
        card_layout.addWidget(self._build_footer())

        # Re-wire the original refresh trigger now that the widgets exist.
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)

        self._thread: QThread | None = None
        self._worker: DownloadWorker | None = None
        self._sync_combo_display()
        self._refresh_status()

    # ----- construction helpers (presentation only) -----------------------
    def _build_header(self) -> QWidget:
        header = _DragHeader(self)
        header.setObjectName("dhead")
        lay = QHBoxLayout(header)
        lay.setContentsMargins(28, 22, 22, 22)
        lay.setSpacing(15)

        logo = QFrame()
        logo.setObjectName("logo")
        logo.setFixedSize(46, 46)
        logo_lay = QVBoxLayout(logo)
        logo_lay.setContentsMargins(0, 0, 0, 0)
        logo_icon = QLabel()
        logo_icon.setAlignment(Qt.AlignCenter)
        logo_icon.setPixmap(_svg_pixmap(_line_icon(_ICON_LOGO_V, "#FFFFFF", width=2.8), 25))
        logo_lay.addWidget(logo_icon)
        lay.addWidget(logo, 0, Qt.AlignVCenter)

        txt_w = QWidget()
        txt = QVBoxLayout(txt_w)
        txt.setContentsMargins(0, 0, 0, 0)
        txt.setSpacing(3)
        txt.addStretch(1)
        title = QLabel("모델 설정")
        title.setObjectName("headTitle")
        subtitle = QLabel("VERITAS · 로컬 AI 모델 준비")
        subtitle.setObjectName("headSub")
        txt.addWidget(title)
        txt.addWidget(subtitle)
        txt.addStretch(1)
        lay.addWidget(txt_w)

        lay.addStretch(1)

        close_btn = QPushButton("✕")
        close_btn.setObjectName("closeBtn")
        close_btn.setFixedSize(30, 30)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.reject)
        lay.addWidget(close_btn, 0, Qt.AlignVCenter)
        return header

    def _build_body(self, embedding_spec) -> QWidget:
        body = QFrame()
        body.setObjectName("dbody")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 24, 28, 12)
        lay.setSpacing(0)

        lead = QLabel(
            "VERITAS가 로드할 "
            '<span style="color:#0E1726; font-weight:700;">로컬 LLM 모델</span>'
            "을 선택하세요."
        )
        lead.setObjectName("lead")
        lead.setTextFormat(Qt.RichText)
        lead.setWordWrap(True)
        lay.addWidget(lead)
        lay.addSpacing(20)

        field = QLabel("AI 모델")
        field.setObjectName("fieldLabel")
        lay.addWidget(field)
        lay.addSpacing(8)

        lay.addWidget(self._build_combo())
        lay.addSpacing(18)

        lay.addWidget(self._build_rows(embedding_spec))
        lay.addSpacing(20)

        lay.addWidget(self._build_statusbox())
        return body

    def _build_combo(self) -> QWidget:
        combo = _ClickableFrame()
        combo.setObjectName("combo")
        combo.setCursor(Qt.PointingHandCursor)
        combo.clicked.connect(self._open_model_menu)
        self._combo_frame = combo
        lay = QHBoxLayout(combo)
        lay.setContentsMargins(15, 13, 15, 13)
        lay.setSpacing(12)

        icon = QFrame()
        icon.setObjectName("micon")
        icon.setFixedSize(34, 34)
        icon_lay = QVBoxLayout(icon)
        icon_lay.setContentsMargins(0, 0, 0, 0)
        icon_lbl = QLabel()
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setPixmap(_svg_pixmap(_line_icon(_ICON_MODEL, "#2E5BFF"), 18))
        icon_lay.addWidget(icon_lbl)
        lay.addWidget(icon, 0, Qt.AlignVCenter)

        info_w = QWidget()
        info = QVBoxLayout(info_w)
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(2)
        info.addStretch(1)
        self._combo_name = QLabel("—")
        self._combo_name.setObjectName("comboName")
        self._combo_meta = QLabel("—")
        self._combo_meta.setObjectName("comboMeta")
        info.addWidget(self._combo_name)
        info.addWidget(self._combo_meta)
        info.addStretch(1)
        lay.addWidget(info_w, 1)

        self._combo_badge = QLabel("미설치")
        self._combo_badge.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._combo_badge, 0, Qt.AlignVCenter)

        caret = QLabel()
        caret.setAlignment(Qt.AlignCenter)
        caret.setPixmap(_svg_pixmap(_line_icon(_ICON_CARET, "#8A94A6", width=2), 18))
        lay.addWidget(caret, 0, Qt.AlignVCenter)
        return combo

    def _build_rows(self, embedding_spec) -> QWidget:
        rows = QWidget()
        lay = QVBoxLayout(rows)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        embed_value = (
            f"{embedding_spec.short_name} · {bytes_label(embedding_spec.size_bytes)}"
        )
        embed_row, _, _ = self._make_row(
            "riBlue", _ICON_ATOM, "#2E5BFF",
            "임베딩 모델", "검색·검증에 사용 · 자동 포함", embed_value,
        )
        lay.addWidget(embed_row)

        storage_row, _, self._storage_value = self._make_row(
            "riSlate", _ICON_DB, "#5A6678",
            "저장 공간", _short_path(model_root()), "—",
        )
        storage_row.setToolTip(str(model_root()))
        lay.addWidget(storage_row)
        return rows

    def _make_row(self, icon_obj, icon_body, icon_color, label, sub, value):
        row = QFrame()
        row.setObjectName("row")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(15, 13, 15, 13)
        lay.setSpacing(13)

        icon = QFrame()
        icon.setObjectName(icon_obj)
        icon.setFixedSize(34, 34)
        icon_lay = QVBoxLayout(icon)
        icon_lay.setContentsMargins(0, 0, 0, 0)
        icon_lbl = QLabel()
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setPixmap(_svg_pixmap(_line_icon(icon_body, icon_color), 18))
        icon_lay.addWidget(icon_lbl)
        lay.addWidget(icon, 0, Qt.AlignVCenter)

        text_w = QWidget()
        text = QVBoxLayout(text_w)
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        text.addStretch(1)
        label_lbl = QLabel(label)
        label_lbl.setObjectName("rowLabel")
        sub_lbl = QLabel(sub)
        sub_lbl.setObjectName("rowSub")
        text.addWidget(label_lbl)
        text.addWidget(sub_lbl)
        text.addStretch(1)
        lay.addWidget(text_w, 1)

        value_lbl = QLabel(value)
        value_lbl.setObjectName("rowValue")
        value_lbl.setTextFormat(Qt.RichText)
        value_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(value_lbl, 0, Qt.AlignVCenter)
        return row, sub_lbl, value_lbl

    def _build_statusbox(self) -> QWidget:
        box = QFrame()
        box.setObjectName("statusbox")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(16, 15, 16, 15)
        lay.setSpacing(0)

        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(9)
        self._status_dot = QFrame()
        self._status_dot.setFixedSize(8, 8)
        self._set_dot("ready")
        line.addWidget(self._status_dot, 0, Qt.AlignVCenter)
        self.status_label = QLabel("…")
        self.status_label.setObjectName("statusText")
        self.status_label.setWordWrap(True)
        line.addWidget(self.status_label, 1)
        lay.addLayout(line)

        self._progress_area = QWidget()
        area = QVBoxLayout(self._progress_area)
        area.setContentsMargins(0, 12, 0, 0)
        area.setSpacing(9)
        self.progress = QProgressBar()
        self.progress.setObjectName("pbar")
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        area.addWidget(self.progress)
        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(0)
        self._pleft = QLabel("")
        self._pleft.setObjectName("pleft")
        self._pright = QLabel("")
        self._pright.setObjectName("pright")
        meta.addWidget(self._pleft)
        meta.addStretch(1)
        meta.addWidget(self._pright)
        area.addLayout(meta)
        lay.addWidget(self._progress_area)
        self._progress_area.setVisible(False)
        return box

    def _build_footer(self) -> QWidget:
        foot = QFrame()
        foot.setObjectName("dfoot")
        lay = QHBoxLayout(foot)
        lay.setContentsMargins(28, 18, 28, 24)
        lay.setSpacing(10)

        hint = QWidget()
        hint_lay = QHBoxLayout(hint)
        hint_lay.setContentsMargins(0, 0, 0, 0)
        hint_lay.setSpacing(6)
        hint_icon = QLabel()
        hint_icon.setAlignment(Qt.AlignCenter)
        hint_icon.setPixmap(_svg_pixmap(_line_icon(_ICON_LOCK, "#8A94A6"), 14))
        hint_text = QLabel("오프라인 · 데이터는 기기에만 저장")
        hint_text.setObjectName("hintText")
        hint_lay.addWidget(hint_icon)
        hint_lay.addWidget(hint_text)
        lay.addWidget(hint, 0, Qt.AlignVCenter)

        lay.addStretch(1)

        self.cancel_button = QPushButton("취소")
        self.cancel_button.setObjectName("cancelBtn")
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.clicked.connect(self.reject)
        lay.addWidget(self.cancel_button)

        self.install_button = QPushButton("설치하고 계속")
        self.install_button.setObjectName("installBtn")
        self.install_button.setCursor(Qt.PointingHandCursor)
        self.install_button.setIcon(
            QIcon(_svg_pixmap(_line_icon(_ICON_DOWNLOAD, "#FFFFFF", width=2), 16))
        )
        self.install_button.setIconSize(QSize(16, 16))
        self.install_button.clicked.connect(self._install_or_accept)
        lay.addWidget(self.install_button)
        return foot

    # ----- small UI-state helpers -----------------------------------------
    def _set_dot(self, state: str) -> None:
        color = "#2E5BFF" if state == "busy" else "#16A36A"
        self._status_dot.setStyleSheet(f"background: {color}; border-radius: 4px;")

    def _set_downloading(self, on: bool) -> None:
        self._progress_area.setVisible(on)
        self._set_dot("busy" if on else "ready")
        self.install_button.setText("설치 중…" if on else "설치하고 계속")

    def _set_combo_badge(self, installed: bool) -> None:
        if installed:
            self._combo_badge.setText("설치됨")
            self._combo_badge.setStyleSheet(
                "background: #E7F6EF; color: #16A36A; border-radius: 9px;"
                " padding: 3px 9px; font-size: 11px; font-weight: 700;"
            )
        else:
            self._combo_badge.setText("미설치")
            self._combo_badge.setStyleSheet(
                "background: #FFF1E8; color: #C2682B; border-radius: 9px;"
                " padding: 3px 9px; font-size: 11px; font-weight: 700;"
            )

    def _sync_combo_display(self) -> None:
        spec = get_model(self.selected_llm_id(), kind="llm")
        self._combo_name.setText(spec.name)
        self._combo_meta.setText(
            f"{spec.quantization} · {bytes_label(spec.size_bytes)}"
        )
        self._set_combo_badge(find_model_file(spec) is not None)

    def _on_model_changed(self) -> None:
        self._sync_combo_display()
        self._refresh_status()

    def _open_model_menu(self) -> None:
        menu = QMenu(self)
        menu.setObjectName("modelMenu")
        menu.setMinimumWidth(self._combo_frame.width())
        current = self.model_combo.currentIndex()
        for i in range(self.model_combo.count()):
            spec = get_model(str(self.model_combo.itemData(i)), kind="llm")
            tag = "설치됨" if find_model_file(spec) else "미설치"
            action = menu.addAction(
                f"{spec.name}    ·    {spec.quantization} · "
                f"{bytes_label(spec.size_bytes)}    ·    {tag}"
            )
            action.setData(i)
            action.setCheckable(True)
            action.setChecked(i == current)
        pos = self._combo_frame.mapToGlobal(QPoint(0, self._combo_frame.height() + 6))
        chosen = menu.exec(pos)
        if chosen is not None and chosen.data() is not None:
            self.model_combo.setCurrentIndex(int(chosen.data()))

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            geometry = self.frameGeometry()
            geometry.moveCenter(screen.availableGeometry().center())
            self.move(geometry.topLeft())

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
        self._storage_value.setText(
            f'<span style="color:#16A36A;">여유 {bytes_label(free)}</span>'
            f" · 필요 {bytes_label(required)}"
        )
        if missing:
            names = ", ".join(spec.short_name for spec in missing)
            self.status_label.setText(f"설치 필요: {names}")
        else:
            self.status_label.setText("필요한 모델 파일이 모두 설치되어 있어요.")
        self._set_dot("ready")

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
        self._set_downloading(True)
        self.status_label.setText("모델을 다운로드하는 중입니다…")
        active = get_model(self.selected_llm_id(), kind="llm")
        self._pleft.setText(f"{active.short_name} 다운로드 중…")
        self._pright.setText("")
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
            self._pright.setText("")
            return
        self.progress.setRange(0, 100)
        self.progress.setValue(min(100, int(done * 100 / total)))
        self._pright.setText(f"{bytes_label(done)} / {bytes_label(total)}")

    def _on_failed(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.install_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self._set_downloading(False)
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
    # Make spawned Python children (api, ui) use UTF-8 for stdout/stderr +
    # file I/O from byte zero. Without this, a child whose stdout is a pipe
    # falls back to the locale code page (cp949 on Korean Windows) and any
    # print of em-dashes / smart quotes (common in web-scraped text) raises
    # UnicodeEncodeError. Matches the parent's encoding="utf-8" pipe decode.
    process_env.setdefault("PYTHONUTF8", "1")
    process_env.setdefault("PYTHONIOENCODING", "utf-8")
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
        # --screen-debug: the full child output still goes to the log file, but
        # the console shows only the screen pipeline's [screen_debug] lines.
        screen_only = screen_debug_enabled()
        with path.open("a", encoding="utf-8", errors="replace") as log:
            for line in stream:
                log.write(line)
                log.flush()
                if screen_only:
                    if "[screen_debug]" in line:
                        print(line, end="", flush=True)
                    continue
                prefix = "" if line.startswith(("[llm]", "[api]")) else f"[{name}] "
                print(f"{prefix}{line}", end="", flush=True)

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
    # The log-relay thread re-prints child stdout (which carries web-scraped
    # text with em-dashes) to the launcher's own console; force UTF-8 so that
    # re-print can't crash on a cp949 console.
    force_utf8_stdio()
    configure_console_logs_from_argv()
    # --screen-debug must run after console-logs config: it overrides the console
    # mode (forcing streaming on) and flags the API child to emit the focused
    # screen trace that the relay then filters down to.
    configure_screen_debug_from_argv()
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
