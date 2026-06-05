from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QPointF, QThread, Qt, QUrl, Signal
from PySide6.QtGui import (
	QColor,
	QDesktopServices,
	QIntValidator,
	QMouseEvent,
	QPainter,
	QPen,
)
from PySide6.QtWidgets import (
	QFrame,
	QHBoxLayout,
	QLabel,
	QLineEdit,
	QMessageBox,
	QPushButton,
	QSizePolicy,
	QToolButton,
	QTextEdit,
	QVBoxLayout,
	QWidget,
)

from ...api_common import STATE, current_workspace_id, load_bootstrap_state
from ...components.buttons import AppButton
from ...components.cards import CardWidget
from ...components.progress import ResearchProgressBar
from ...controllers import AgentController, JobCategory, get_job_manager
from ...theme import theme

# Bounds for the user-configurable "max documents to research" control.
# The default mirrors the backend VERITAS_MAX_DOCS fallback (15).
MIN_RESEARCH_DOCS = 1
MAX_RESEARCH_DOCS = 50
DEFAULT_RESEARCH_DOCS = 15


def _soft_break_long_tokens(text: str, chunk: int = 24) -> str:
	"""Insert zero-width break opportunities into long unbreakable tokens.

	A word-wrap QLabel only breaks on whitespace, so a long token — a URL or a
	filesystem path — reports its full width as the widget's minimum. Inside
	the research page's scroll area that inflates the page's minimumSizeHint
	and stretches the whole 조사 결과 pane (and its progress bar) horizontally.
	Zero-width spaces give the label legal break points without changing the
	visible text.
	"""
	def _break(match: re.Match[str]) -> str:
		token = match.group(0)
		return "​".join(token[i : i + chunk] for i in range(0, len(token), chunk))

	return re.sub(rf"\S{{{chunk + 1},}}", _break, str(text or ""))


class CircleGlyphButton(QToolButton):
	"""Round button whose glyph (＋ / − / ✕) is painted as centered strokes.

	Qt centers button *text* by its line box, so a '+' or 'x' character ends
	up visibly above/below the true center of a circular button. Stroking the
	glyph through the widget's exact center fixes that and stays crisp at any
	size; the circle background, border and hover state still come from the
	stylesheet via the base ``paintEvent``.
	"""

	def __init__(
		self,
		glyph: str,
		color: str,
		hover_color: str,
		diameter: int,
		parent: QWidget | None = None,
	) -> None:
		super().__init__(parent)
		self._glyph = glyph  # "plus" | "minus" | "cross"
		# Glyph colours are stored as theme *token* names and resolved at paint
		# time, so the stroke follows a live light/dark toggle. The circle
		# background/border + hover state still come from the stylesheet.
		self._color_token = color
		self._hover_color_token = hover_color
		self._disabled_color_token = "border.strong"
		self.setFixedSize(diameter, diameter)
		self.setCursor(Qt.PointingHandCursor)
		self.setFocusPolicy(Qt.NoFocus)
		theme.themeChanged.connect(self._on_theme_changed)

	def _on_theme_changed(self, *args) -> None:
		self.update()

	def enterEvent(self, event) -> None:  # type: ignore[override]
		super().enterEvent(event)
		self.update()

	def leaveEvent(self, event) -> None:  # type: ignore[override]
		super().leaveEvent(event)
		self.update()

	def paintEvent(self, event) -> None:  # type: ignore[override]
		super().paintEvent(event)  # stylesheet-driven circle + hover background
		painter = QPainter(self)
		painter.setRenderHint(QPainter.Antialiasing, True)
		if not self.isEnabled():
			color = QColor(theme.color(self._disabled_color_token))
		elif self.underMouse():
			color = QColor(theme.color(self._hover_color_token))
		else:
			color = QColor(theme.color(self._color_token))
		pen = QPen(color)
		pen.setWidthF(max(1.7, self.width() * 0.075))
		pen.setCapStyle(Qt.RoundCap)
		painter.setPen(pen)
		cx = self.width() / 2.0
		cy = self.height() / 2.0
		arm = min(self.width(), self.height()) * 0.23
		if self._glyph == "plus":
			painter.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))
			painter.drawLine(QPointF(cx, cy - arm), QPointF(cx, cy + arm))
		elif self._glyph == "minus":
			painter.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))
		else:  # cross
			painter.drawLine(QPointF(cx - arm, cy - arm), QPointF(cx + arm, cy + arm))
			painter.drawLine(QPointF(cx - arm, cy + arm), QPointF(cx + arm, cy - arm))


class DocCountStepper(QFrame):
	"""Rounded −/＋ stepper for the '최대 조사 문서 수' value.

	A polished stand-in for a bare QSpinBox: large round hit targets and a
	center value the user can also type directly into. The typed value is
	range-validated per keystroke (QIntValidator) and clamped to [min, max]
	on commit. Exposes the small slice of the QSpinBox API the page relies on
	(:meth:`value` / :meth:`setValue`).

	The widget keeps a fixed width so a larger value can never stretch the
	surrounding layout — and therefore the 조사 결과 progress bar.
	"""

	valueChanged = Signal(int)

	def __init__(
		self,
		minimum: int,
		maximum: int,
		value: int,
		parent: QWidget | None = None,
	) -> None:
		super().__init__(parent)
		self.setObjectName("DocCountStepper")
		self._min = minimum
		self._max = maximum
		self._value = max(minimum, min(maximum, value))
		# Fixed footprint: the stepper must never grow with the value.
		self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

		layout = QHBoxLayout(self)
		layout.setContentsMargins(6, 6, 6, 6)
		layout.setSpacing(6)

		self._minus = CircleGlyphButton("minus", "accent", "accent.hover", 32)
		self._minus.setObjectName("StepperButton")
		self._minus.setToolTip("문서 수 줄이기")
		self._minus.clicked.connect(lambda: self._step(-1))

		# Editable value field: type a number directly. The validator blocks
		# out-of-range keystrokes; editingFinished clamps whatever remains.
		self._value_edit = QLineEdit()
		self._value_edit.setObjectName("StepperValue")
		self._value_edit.setAlignment(Qt.AlignCenter)
		self._value_edit.setFixedWidth(40)
		self._value_edit.setValidator(QIntValidator(self._min, self._max, self))
		self._value_edit.setToolTip(
			f"{self._min}~{self._max} 사이의 값을 직접 입력할 수 있습니다."
		)
		self._value_edit.editingFinished.connect(self._commit_typed_value)

		self._unit_label = QLabel("개")
		self._unit_label.setObjectName("StepperUnit")
		self._unit_label.setAlignment(Qt.AlignCenter)

		self._plus = CircleGlyphButton("plus", "accent", "accent.hover", 32)
		self._plus.setObjectName("StepperButton")
		self._plus.setToolTip("문서 수 늘리기")
		self._plus.clicked.connect(lambda: self._step(1))

		layout.addWidget(self._minus)
		layout.addWidget(self._value_edit)
		layout.addWidget(self._unit_label)
		layout.addWidget(self._plus)

		self._sync()

	def value(self) -> int:
		return self._value

	def setValue(self, value: int) -> None:
		clamped = max(self._min, min(self._max, int(value)))
		changed = clamped != self._value
		self._value = clamped
		self._sync()
		if changed:
			self.valueChanged.emit(self._value)

	def setRange(self, minimum: int, maximum: int) -> None:
		minimum = int(minimum)
		maximum = max(minimum, int(maximum))
		if minimum == self._min and maximum == self._max:
			return
		self._min = minimum
		self._max = maximum
		self._value_edit.setValidator(QIntValidator(self._min, self._max, self))
		self._value_edit.setToolTip(
			f"{self._min}~{self._max} 사이의 값을 직접 입력할 수 있습니다."
		)
		self.setValue(self._value)

	def setMaximum(self, maximum: int) -> None:
		self.setRange(self._min, maximum)

	def _step(self, delta: int) -> None:
		self.setValue(self._value + delta)

	def _commit_typed_value(self) -> None:
		"""Apply a directly-typed value, clamped to [min, max].

		The validator already rejects clearly out-of-range keystrokes; this
		catches the rest (empty field, an intermediate value like ``0``) and
		snaps the field back to the canonical value via ``setValue``.
		"""
		text = self._value_edit.text().strip()
		try:
			typed = int(text)
		except ValueError:
			typed = self._value
		self.setValue(typed)

	def _sync(self) -> None:
		text = str(self._value)
		if self._value_edit.text() != text:
			self._value_edit.setText(text)
		self._minus.setEnabled(self._value > self._min)
		self._plus.setEnabled(self._value < self._max)


class InfoTile(QFrame):
	"""Small label/value tile shown in the top info row of the result card."""

	def __init__(self, label: str, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("ResearchInfoTile")
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(2)
		self._label = QLabel(label)
		self._value = QLabel("-")
		self._value.setWordWrap(True)
		self._value.setTextInteractionFlags(Qt.TextSelectableByMouse)
		layout.addWidget(self._label)
		layout.addWidget(self._value)
		self._apply_theme()
		theme.themeChanged.connect(self._apply_theme)

	def _apply_theme(self, *args) -> None:
		self.setStyleSheet(
			f"QFrame#ResearchInfoTile {{ background-color: {theme.color('surface.muted')}; "
			f"border: 1px solid {theme.color('border')}; "
			"border-radius: 10px; padding: 8px 12px; }"
		)
		self._label.setStyleSheet(
			f"color: {theme.color('text.secondary.gray')}; font-size: 10px; "
			"font-weight: 700; letter-spacing: 0.4px;"
		)
		self._value.setStyleSheet(
			f"color: {theme.color('text.primary')}; font-size: 13px; font-weight: 700;"
		)

	def set_value(self, value: str) -> None:
		# Break long unbreakable values (e.g. a workspace path) so the tile can
		# shrink with the layout instead of stretching the result pane.
		self._value.setText(_soft_break_long_tokens(value) if value else "-")
		self._value.setToolTip(value if value else "")


class LinkLabel(QLabel):
	"""QLabel that opens its URL on left-click anywhere inside the widget.

	Qt's `linkActivated` + `setOpenExternalLinks` mechanism is unreliable on
	Windows under PySide6 — the cursor changes to a hand on hover but clicks
	are sometimes never delivered to the link handler. Handling
	`mousePressEvent` directly removes that ambiguity: the whole label is the
	hit target, and `QDesktopServices.openUrl` is invoked unconditionally.
	"""

	def __init__(self, url: str, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._url = (url or "").strip()
		# Break the URL into wrappable chunks: a word-wrap QLabel cannot break a
		# spaceless URL, so without this its minimum width is the full string
		# and it stretches the document row (and the result pane) horizontally.
		self._display = html.escape(_soft_break_long_tokens(self._url), quote=False)
		self.setTextFormat(Qt.RichText)
		self.setTextInteractionFlags(Qt.NoTextInteraction)
		self.setWordWrap(True)
		self.setCursor(Qt.PointingHandCursor)
		self.setToolTip(self._url)
		self.setStyleSheet("font-size: 11px;")
		self._apply_theme()
		theme.themeChanged.connect(self._apply_theme)

	def _apply_theme(self, *args) -> None:
		# Rich text is only used for the underline + color; the click handler
		# does not rely on Qt parsing an <a> tag. Re-set on a theme toggle so
		# the link colour follows the active palette.
		self.setText(
			f'<span style="color:{theme.color("link")}; text-decoration:underline;">'
			f'{self._display}</span>'
		)

	def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
		if event.button() == Qt.LeftButton and self._url:
			url = QUrl(self._url)
			# Inputs like "example.com/page" lack a scheme and would silently
			# fail with QDesktopServices.openUrl; default to https in that case.
			if not url.scheme():
				url = QUrl(f"https://{self._url}")
			QDesktopServices.openUrl(url)
			event.accept()
			return
		super().mousePressEvent(event)


class DocumentBar(QFrame):
	"""One collected-document row with a title, hyperlink URL, and an
	"open doc_*.md" button on the right.

	The widget is a dumb view: it is built in a "pending" state with the
	open-summary button greyed out, and mutated exactly once via
	:meth:`set_summary_ready` when the controller learns the corresponding
	`summary/doc_NNN.md` has been written. Live state changes during a run
	flow through this single method.
	"""

	def __init__(
		self,
		index: int,
		doc_id: str,
		title: str,
		url: str,
		summary_path: Path | None = None,
		parent: QWidget | None = None,
	) -> None:
		super().__init__(parent)
		self.setObjectName("ResearchDocumentBar")
		self.doc_id = str(doc_id or "")
		self._summary_path: Path | None = None
		self._failed = False

		layout = QHBoxLayout(self)
		layout.setContentsMargins(12, 10, 12, 10)
		layout.setSpacing(10)

		text_column = QVBoxLayout()
		text_column.setContentsMargins(0, 0, 0, 0)
		text_column.setSpacing(2)

		safe_title = title if title else "Untitled"
		title_text = _soft_break_long_tokens(f"{index}. {safe_title}")
		self._title_label = QLabel(title_text)
		self._title_label.setWordWrap(True)
		self._title_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
		text_column.addWidget(self._title_label)

		if url:
			text_column.addWidget(LinkLabel(url))

		layout.addLayout(text_column, 1)

		self._open_button = QPushButton(self)
		self._open_button.setCursor(Qt.PointingHandCursor)
		self._open_button.clicked.connect(self._on_open_clicked)
		layout.addWidget(self._open_button, 0, Qt.AlignTop)

		self._apply_theme()
		theme.themeChanged.connect(self._apply_theme)

		# Render initial state. If a path was passed (e.g. when reconstructing
		# from a completed job), we honor it immediately; otherwise the button
		# starts in pending/disabled state.
		if summary_path is not None and summary_path.exists():
			self.set_summary_ready(summary_path)
		else:
			self._apply_pending_state()

	def _apply_theme(self, *args) -> None:
		"""(Re)apply every palette-derived style so the row follows a theme
		toggle. Dispatches to the failed-state styling once the document's
		summarization has been marked failed."""
		self._title_label.setStyleSheet(
			f"color: {theme.color('text.primary')}; font-size: 13px; font-weight: 700;"
		)
		if self._failed:
			self._apply_failed_styles()
		else:
			self._apply_default_styles()

	def _apply_default_styles(self) -> None:
		self.setStyleSheet(
			f"QFrame#ResearchDocumentBar {{ background-color: {theme.color('surface')}; "
			f"border: 1px solid {theme.color('border')}; border-radius: 10px; }}"
			f"QFrame#ResearchDocumentBar:hover {{ border-color: {theme.color('accent.subtle.border')}; }}"
		)
		self._open_button.setStyleSheet(
			f"QPushButton {{ background-color: {theme.color('accent.subtle.bg')}; "
			f"color: {theme.color('accent.text')}; "
			f"border: 1px solid {theme.color('accent.subtle.border')}; border-radius: 8px; "
			"padding: 6px 10px; font-size: 11px; font-weight: 800; }"
			f"QPushButton:hover {{ background-color: {theme.color('accent.subtle.bg.hover')}; "
			f"border-color: {theme.color('accent.border.checked')}; }}"
			f"QPushButton:disabled {{ background-color: {theme.color('surface.muted2')}; "
			f"color: {theme.color('text.muted.gray')}; "
			f"border-color: {theme.color('border.gray')}; }}"
		)

	def _apply_failed_styles(self) -> None:
		self._open_button.setStyleSheet(
			f"QPushButton {{ background-color: {theme.color('danger.bg2')}; "
			f"color: {theme.color('danger.fg')}; "
			f"border: 1px solid {theme.color('danger.border2')}; border-radius: 8px; "
			"padding: 6px 10px; font-size: 11px; font-weight: 800; }"
			f"QPushButton:disabled {{ background-color: {theme.color('danger.bg2')}; "
			f"color: {theme.color('danger.fg')}; "
			f"border-color: {theme.color('danger.border2')}; }}"
		)
		self.setStyleSheet(
			f"QFrame#ResearchDocumentBar {{ background-color: {theme.color('danger.bg')}; "
			f"border: 1px solid {theme.color('danger.border2')}; border-radius: 10px; }}"
		)

	def set_summary_ready(self, summary_path: Path) -> None:
		"""Mark this document's summary as available and enable the open button."""
		self._summary_path = summary_path
		self._failed = False
		self._open_button.setEnabled(True)
		self._open_button.setText(f"{summary_path.name} ↗")
		self._open_button.setToolTip(str(summary_path))

	def set_failed(self, reason: str = "") -> None:
		"""Mark this document's summarization as failed after all retries.

		The run continues for the other documents; this row just turns red so
		the failure is visible without making the whole run look failed.
		"""
		self._summary_path = None
		self._failed = True
		self._open_button.setEnabled(False)
		self._open_button.setText("요약 실패")
		self._open_button.setToolTip(reason or "요약에 실패했습니다.")
		self._apply_failed_styles()

	def is_summary_ready(self) -> bool:
		"""True once this document has been summarized (doc_summarized event)."""
		return self._summary_path is not None

	def is_failed(self) -> bool:
		"""True once this document's summarization has been marked failed."""
		return self._failed

	def _apply_pending_state(self) -> None:
		self._summary_path = None
		self._open_button.setEnabled(False)
		self._open_button.setText("요약 대기 중")
		self._open_button.setToolTip("요약이 완료되면 열 수 있습니다.")

	def _on_open_clicked(self) -> None:
		if self._summary_path is None or not self._summary_path.exists():
			return
		QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._summary_path)))


class ResearchProgressPoller(QThread):
	"""Polls /api/v1/research/progress on a background thread.

	Emits the full list of new events each tick so the controller can drive
	per-document UI lifecycle (doc_fetched/doc_summarized) in addition to the
	single-line gray status display. Cursor advances strictly forward, so a
	long-running job that survives an API hiccup will still see every event
	exactly once.
	"""

	events = Signal(list)
	# Emitted when the backend's progress sequence rewinds — i.e. a new run
	# started and reset its counter. The page drops any stale view state so the
	# live caption / percentage track the new run instead of the previous one.
	reset_detected = Signal()

	def __init__(self, parent: QObject | None = None) -> None:
		super().__init__(parent)
		self._cursor = 0
		self._stop = False
		self._sleep_ms = 800
		# The backend keeps a process-wide progress buffer that is only reset
		# once the new run actually starts on its worker thread. The first poll
		# therefore just baselines the cursor at the current tail; the real
		# run's events arrive afterwards and are caught by the rewind check.
		self._primed = False

	def request_stop(self) -> None:
		self._stop = True

	def reset(self) -> None:
		self._cursor = 0
		self._primed = False

	def run(self) -> None:  # type: ignore[override]
		while not self._stop:
			try:
				response = AgentController().get_research_progress(
					since=self._cursor, limit=100
				)
				if isinstance(response, dict):
					latest_seq = int(response.get("latestSeq") or 0)
					if not self._primed:
						# Skip whatever is already buffered (previous run) and
						# start tracking from the current tail.
						self._primed = True
						self._cursor = latest_seq
					else:
						if latest_seq < self._cursor:
							# Sequence went backwards -> a fresh run began and
							# reset the backend counter. Rewind and replay it.
							self._cursor = 0
							self.reset_detected.emit()
							response = AgentController().get_research_progress(
								since=0, limit=100
							)
						items = (
							response.get("items", [])
							if isinstance(response, dict)
							else []
						)
						if isinstance(items, list) and items:
							self._cursor = int(
								response.get("nextCursor") or self._cursor
							)
							valid = [it for it in items if isinstance(it, dict)]
							if valid:
								self.events.emit(valid)
			except Exception:
				pass
			elapsed = 0
			while not self._stop and elapsed < self._sleep_ms:
				self.msleep(100)
				elapsed += 100


class ResearchPage(QWidget):
	workspaceChanged = Signal(str)
	# Fired when AutoSurvey reserves a new `runs/<id>/` directory mid-run.
	# Carries (workspaceId, displayName). Unlike `workspaceChanged` (emitted
	# at completion to drive full page refreshes), this is a *light* event:
	# subscribers should update their workspace pointer + clear ephemeral
	# views (sidebar footer, chat bubbles) without re-running heavy page
	# refresh logic that would clobber the in-progress research display.
	workspaceCreated = Signal(str, str)
	# Fired by the "이 보고서로 글쓰기" button. Carries the workspace id; the host
	# (MainWindow) opens the editor window seeded from that workspace's final.md.
	openEditorRequested = Signal(str)

	# Progress is driven by *document count* against the user-requested total
	# (`_max_docs`), not by backend stage names. The collection loop owns the
	# `_COUNT_BAND_START`..`_COUNT_BAND_END` slice of the bar — within it the
	# percentage is purely "docs done / docs requested". Stages before the
	# first document get a small floor so the bar is not pinned at 0 while the
	# agent plans; the post-collection stages fill the remaining tail.
	_PRE_FLOOR = {
		"term_grounding": 3.0,
		"query_plan": 6.0,
		"web_search": 9.0,
		"fetch_webpage": 9.0,
	}
	_TAIL_FLOOR = {
		"final_report": 92.0,
		"indexing": 97.0,
		"completed": 100.0,
	}
	_COUNT_BAND_START = 10.0
	_COUNT_BAND_END = 90.0

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._url_rows: list[tuple[QFrame, QLineEdit]] = []
		self._workspace_id = current_workspace_id()
		self._progress_poller: ResearchProgressPoller | None = None
		# How many documents the next run should collect. Mirrors the spin box
		# below; also used as the denominator for the count-based progress bar.
		self._max_docs = DEFAULT_RESEARCH_DOCS
		# Controller-side document model keyed by doc_id. Bars are created on
		# `doc_fetched` events and activated on `doc_summarized`. The final
		# response reconciles anything that polling may have missed.
		self._doc_bars: dict[str, DocumentBar] = {}
		# Monotonic guard for the background "load persisted result" fetch, so a
		# slow `list_research_jobs` completion can't clobber a newer workspace
		# load — or a research run the user started in the meantime.
		self._result_load_token = 0

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(12)

		header_card = CardWidget("조사")
		subtitle = QLabel("조사 주제와 참고 URL을 입력하고 실행하면 backend AutoSurvey workflow가 동작합니다.")
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		header_card.layout.addWidget(subtitle)
		root.addWidget(header_card)

		content_card = CardWidget("조사 정보 입력")

		research_label = QLabel("조사 내용 입력")
		research_label.setObjectName("CardPrimary")
		self.research_input = QTextEdit()
		self.research_input.setObjectName("ResearchInput")
		self.research_input.setPlaceholderText("예: 2026년 AI 규제 동향을 산업별로 조사하고 핵심 리스크와 대응 전략을 정리해줘.")
		self.research_input.setMinimumHeight(140)

		reference_header = QHBoxLayout()
		reference_header.setContentsMargins(0, 0, 0, 0)
		reference_header.setSpacing(8)

		reference_label = QLabel("레퍼런스 사이트")
		reference_label.setObjectName("CardPrimary")

		add_url_btn = CircleGlyphButton("plus", "text.on_accent", "text.on_accent", 30)
		add_url_btn.setObjectName("RoundAddButton")
		add_url_btn.setToolTip("레퍼런스 URL 추가")
		add_url_btn.clicked.connect(lambda: self.add_reference_url())

		reference_header.addWidget(reference_label)
		reference_header.addStretch(1)
		reference_header.addWidget(add_url_btn)

		self.url_list = QVBoxLayout()
		self.url_list.setContentsMargins(0, 0, 0, 0)
		self.url_list.setSpacing(8)

		guide = QLabel("필요한 URL을 추가한 뒤 조사 실행을 누르세요.")
		guide.setObjectName("CardSecondary")
		guide.setWordWrap(True)

		# User-configurable cap on how many documents AutoSurvey collects,
		# presented as a self-contained settings row so it reads as a
		# deliberate control rather than a stray field.
		count_card = QFrame()
		count_card.setObjectName("ResearchCountCard")
		count_card_layout = QHBoxLayout(count_card)
		count_card_layout.setContentsMargins(16, 13, 16, 13)
		count_card_layout.setSpacing(14)

		count_text_col = QVBoxLayout()
		count_text_col.setContentsMargins(0, 0, 0, 0)
		count_text_col.setSpacing(3)
		count_title = QLabel("최대 조사 문서 수")
		count_title.setObjectName("ResearchCountTitle")
		count_hint = QLabel(
			f"AutoSurvey가 수집할 문서 수입니다. 기본 {DEFAULT_RESEARCH_DOCS}개이며 "
			f"{MIN_RESEARCH_DOCS}~{MAX_RESEARCH_DOCS}개 범위에서 조절할 수 있습니다."
		)
		count_hint.setObjectName("ResearchCountHint")
		count_hint.setWordWrap(True)
		count_text_col.addWidget(count_title)
		count_text_col.addWidget(count_hint)

		self.doc_count_stepper = DocCountStepper(
			MIN_RESEARCH_DOCS, MAX_RESEARCH_DOCS, DEFAULT_RESEARCH_DOCS
		)
		self.doc_count_stepper.setToolTip(
			f"조사할 문서 수를 {MIN_RESEARCH_DOCS}~{MAX_RESEARCH_DOCS}개 범위에서 설정합니다."
		)

		count_card_layout.addLayout(count_text_col, 1)
		count_card_layout.addWidget(self.doc_count_stepper, 0, Qt.AlignVCenter)

		action_row = QHBoxLayout()
		action_row.addStretch(1)
		self.run_button = AppButton("조사 실행")
		self.run_button.clicked.connect(self._run_research)
		action_row.addWidget(self.run_button)

		content_card.layout.addWidget(research_label)
		content_card.layout.addWidget(self.research_input)
		content_card.layout.addLayout(reference_header)
		content_card.layout.addLayout(self.url_list)
		content_card.layout.addWidget(count_card)
		content_card.layout.addWidget(guide)
		content_card.layout.addLayout(action_row)
		root.addWidget(content_card)

		result_card = CardWidget("조사 결과")
		self._final_path: Path | None = None
		self._progress_estimate = 0.0
		self._research_error_message = ""
		# Per-document summarization failures for the current/last run, used to
		# drive the "일부 오류 발생" progress-bar state and its detail popup.
		self._research_failed_documents: list[dict[str, Any]] = []

		self.progress_bar = ResearchProgressBar()
		self.progress_bar.errorClicked.connect(self._show_research_error)
		result_card.layout.addWidget(self.progress_bar)

		info_row = QHBoxLayout()
		info_row.setContentsMargins(0, 0, 0, 0)
		info_row.setSpacing(8)
		self.info_job_name = InfoTile("작업 이름")
		self.info_save_path = InfoTile("저장 경로")
		self.info_doc_count = InfoTile("수집된 문서 수")
		info_row.addWidget(self.info_job_name, 1)
		info_row.addWidget(self.info_save_path, 2)
		info_row.addWidget(self.info_doc_count, 1)
		# "이 보고서로 글쓰기" — opens the editor seeded from this workspace's
		# final.md. Lives inside info_row_widget so it shows/hides together with
		# the result metadata (i.e. only once a workspace result exists).
		self.write_button = QPushButton("이 보고서로 글쓰기")
		self.write_button.setObjectName("PrimaryButton")
		self.write_button.setCursor(Qt.PointingHandCursor)
		self.write_button.clicked.connect(
			lambda: self.openEditorRequested.emit(self._workspace_id)
		)
		info_row.addWidget(self.write_button, 0, Qt.AlignVCenter)
		self.info_row_widget = QFrame()
		self.info_row_widget.setLayout(info_row)
		self.info_row_widget.setVisible(False)
		result_card.layout.addWidget(self.info_row_widget)

		documents_header = QLabel("수집된 문서")
		documents_header.setObjectName("CardPrimary")
		documents_header.setVisible(False)
		self.documents_header = documents_header
		result_card.layout.addWidget(documents_header)

		self.documents_container = QVBoxLayout()
		self.documents_container.setContentsMargins(0, 0, 0, 0)
		self.documents_container.setSpacing(6)
		result_card.layout.addLayout(self.documents_container)

		self.result_empty = QLabel("조사 실행 후 agent 결과가 여기에 표시됩니다.")
		self._apply_result_empty_theme()
		theme.themeChanged.connect(self._apply_result_empty_theme)
		self.result_empty.setAlignment(Qt.AlignCenter)
		self.result_empty.setWordWrap(True)
		result_card.layout.addWidget(self.result_empty)

		result_card.layout.addStretch(1)
		root.addWidget(result_card, 1)

		self.add_reference_url()
		self._load_existing_result()

		# Subscribe to global busy state so the run button reflects whether
		# AutoSurvey can start right now (e.g. another research is in flight
		# or a chat is mid-stream).
		get_job_manager().busy_changed.connect(self._sync_busy_state)
		self._sync_busy_state()

	def add_reference_url(self, url: str = "") -> None:
		row = QFrame()
		row.setObjectName("ReferenceUrlRow")
		row_layout = QHBoxLayout(row)
		row_layout.setContentsMargins(10, 8, 8, 8)
		row_layout.setSpacing(8)

		url_input = QLineEdit()
		url_input.setObjectName("ReferenceUrlInput")
		url_input.setPlaceholderText("https://example.com/report")
		url_input.setText(url)

		remove_btn = CircleGlyphButton("cross", "text.secondary", "danger.fg", 26)
		remove_btn.setObjectName("UrlRemoveButton")
		remove_btn.setToolTip("URL 삭제")
		remove_btn.clicked.connect(lambda _checked=False, target=row: self._remove_reference_url(target))

		row_layout.addWidget(url_input, 1)
		row_layout.addWidget(remove_btn)

		self._url_rows.append((row, url_input))
		self.url_list.addWidget(row)
		url_input.setFocus()

	def get_reference_urls(self) -> list[str]:
		return [url_input.text().strip() for _row, url_input in self._url_rows if url_input.text().strip()]

	def _run_research(self) -> None:
		instruction = self.research_input.toPlainText().strip()
		if not instruction:
			self._show_message("조사할 내용을 입력하세요.")
			return

		self._workspace_id = current_workspace_id()
		reference_urls = self.get_reference_urls()
		self._max_docs = self.doc_count_stepper.value()
		# AutoSurvey pacing from 설정 > 고급 설정 > 조사 진행 방식. The settings
		# page writes these into the shared in-process STATE; read them fresh at
		# run time so the workflow honors the user's configured values. When
		# unset, None is sent and the backend falls back to its env defaults.
		research_settings = STATE.get("settings", {}).get("research", {})
		scout_docs = research_settings.get("sampleCount")
		collect_batch_size = research_settings.get("planCount")
		started = get_job_manager().submit(
			JobCategory.RESEARCH,
			AgentController().run_research,
			self._workspace_id,
			instruction,
			reference_urls,
			self._max_docs,
			scout_docs,
			collect_batch_size,
			on_success=self._on_research_finished,
			on_error=self._on_research_failed,
		)
		if not started:
			# Defensive: the button should already be disabled via busy
			# state, but if it slipped through (e.g. quick double-click)
			# we still bail out cleanly.
			return

		# Invalidate any in-flight "load persisted result" fetch so its late
		# completion can't overwrite this run's live progress display.
		self._result_load_token += 1
		self._clear_documents()
		self.info_row_widget.setVisible(False)
		self.documents_header.setVisible(False)
		self.result_empty.setVisible(False)
		self._progress_estimate = 0.0
		self._research_error_message = ""
		self._research_failed_documents = []
		self.progress_bar.start()
		self._start_progress_poller()

	def _sync_busy_state(self) -> None:
		"""Reflect the global JobManager state on this page's controls."""
		manager = get_job_manager()
		can_run = not manager.is_blocked(JobCategory.RESEARCH)
		self.run_button.setEnabled(can_run)
		# The doc-count cap belongs to the *next* run; lock it while one is
		# already in flight so it can't look like it affects the live job.
		self.doc_count_stepper.setEnabled(can_run)

	def _start_progress_poller(self) -> None:
		self._stop_progress_poller()
		poller = ResearchProgressPoller(self)
		poller.events.connect(self._on_progress_events)
		poller.reset_detected.connect(self._on_progress_reset)
		self._progress_poller = poller
		poller.start()

	def _stop_progress_poller(self) -> None:
		poller = self._progress_poller
		self._progress_poller = None
		if poller is not None:
			poller.request_stop()
			poller.wait(1500)
			poller.deleteLater()

	def _on_progress_events(self, items: list) -> None:
		"""Route each backend progress event to the right view update.

		Stage handlers are intentionally small and side-effect-only:
		- `workspace_created` → swap to the new workspace (info tiles +
		  emit `workspaceCreated` so sidebar/chat reset live)
		- `doc_fetched`       → add a pending DocumentBar
		- `doc_summarized`    → activate the matching bar
		- `doc_failed`        → mark the matching bar as failed

		The latest message becomes the progress-bar caption, so the area under
		the bar shows the agent's current activity in real time.
		"""
		# A final batch can still be queued cross-thread after the poller is
		# stopped and the result already rendered; dropping it here keeps a
		# stale event from flipping the completed bar back to "running".
		if self._progress_poller is None:
			return
		latest_message = ""
		for event in items:
			if not isinstance(event, dict):
				continue
			stage = str(event.get("stage") or "").strip()
			detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
			message = str(event.get("message") or "")
			if stage == "workspace_created":
				self._adopt_new_workspace(detail)
			elif stage == "doc_fetched":
				self._add_pending_document_bar(detail)
			elif stage == "doc_summarized":
				self._activate_document_bar(detail)
			elif stage == "doc_failed":
				self._fail_document_bar(detail)
			self._bump_progress_estimate(stage)
			if message:
				latest_message = message
		# Always push the running estimate and the latest backend message so the
		# bar keeps inching forward and the caption shows the agent's current
		# activity (검색 중 / 수집 중 / 요약 중 ...) in real time.
		self.progress_bar.set_progress(self._progress_estimate)
		if latest_message:
			self.progress_bar.set_caption(latest_message)

	def _on_progress_reset(self) -> None:
		"""The poller saw the backend rewind its progress counter — a new run
		started. Drop any stale bars/estimate the poller picked up from the
		previous run before its cursor caught the reset."""
		if self._progress_poller is None:
			return
		self._clear_documents()
		self._progress_estimate = 0.0
		self._research_error_message = ""
		self._research_failed_documents = []
		self.progress_bar.start()

	def _bump_progress_estimate(self, stage: str) -> None:
		"""Advance the monotonic progress estimate after one backend stage.

		The headline number is count-based: within the collection band the
		percentage is simply "documents done / documents requested". Stages
		before the first document only raise a small floor; the closing
		stages (final report, indexing, completed) fill the tail. The
		estimate never moves backward.
		"""
		stage = (stage or "").strip()
		tail = self._TAIL_FLOOR.get(stage)
		if tail is not None:
			target = tail
		else:
			target = max(self._PRE_FLOOR.get(stage, 0.0), self._count_progress())
		self._progress_estimate = min(100.0, max(self._progress_estimate, target))

	def _count_progress(self) -> float:
		"""Percentage derived purely from how many of the requested documents
		have been collected so far, scaled across the collection band.

		A document that has been fetched but not yet summarized counts as
		half done; a summarized document counts as whole. This is what makes
		the bar read as "전체 N개 중 몇 개 진행" rather than a stage guess.
		"""
		total = max(1, self._max_docs)
		fetched = len(self._doc_bars)
		if fetched == 0:
			# Nothing collected yet — let the pre-collection floors drive the
			# bar rather than pinning it at the band start.
			return 0.0
		summarized = sum(
			1 for bar in self._doc_bars.values() if bar.is_summary_ready()
		)
		done = min(float(total), (fetched + summarized) / 2.0)
		span = self._COUNT_BAND_END - self._COUNT_BAND_START
		return self._COUNT_BAND_START + (done / total) * span

	def _adopt_new_workspace(self, detail: dict) -> None:
		"""Reflect a freshly-reserved workspace in the result card.

		Updates the info tiles (작업 이름 / 저장 경로) so the user sees the
		final workspace id and path as soon as term-grounding finishes,
		instead of waiting for the whole AutoSurvey run. Also broadcasts a
		`workspaceCreated` signal so the sidebar and chat panels can sync.
		"""
		workspace_id = str(detail.get("workspaceId") or "").strip()
		if not workspace_id:
			return
		name = str(detail.get("name") or workspace_id)
		path = str(detail.get("path") or "").strip()
		self._workspace_id = workspace_id
		self.info_job_name.set_value(workspace_id)
		if path:
			self.info_save_path.set_value(path)
			self._final_path = Path(path) / "final.md"
		self.info_doc_count.set_value(f"{len(self._doc_bars)}건")
		self.info_row_widget.setVisible(True)
		self.workspaceCreated.emit(workspace_id, name)

	def _doc_count_text(self) -> str:
		"""Caption for the '수집된 문서 수' tile — collected vs. requested."""
		return f"{len(self._doc_bars)} / {self._max_docs}건"

	def _add_pending_document_bar(self, detail: dict) -> None:
		doc_id = str(detail.get("doc_id") or "").strip()
		if not doc_id or doc_id in self._doc_bars:
			return
		title = str(detail.get("title") or "Untitled")
		url = str(detail.get("final_url") or detail.get("url") or "")
		index = len(self._doc_bars) + 1
		bar = DocumentBar(index=index, doc_id=doc_id, title=title, url=url)
		self._doc_bars[doc_id] = bar
		self.documents_container.addWidget(bar)
		# First arrival flips the section from "empty" to "list-of-bars".
		self.documents_header.setVisible(True)
		self.result_empty.setVisible(False)
		self.info_doc_count.set_value(self._doc_count_text())
		self.info_row_widget.setVisible(True)

	def _activate_document_bar(self, detail: dict) -> None:
		doc_id = str(detail.get("doc_id") or "").strip()
		if not doc_id:
			return
		bar = self._doc_bars.get(doc_id)
		if bar is None:
			# Late summarize event without a prior fetch event (rare —
			# polling lag or restart). Reconciliation at completion will
			# pick this doc up from the final response.
			return
		summary_path_str = str(detail.get("summary_path") or "").strip()
		if not summary_path_str:
			return
		bar.set_summary_ready(Path(summary_path_str))

	def _fail_document_bar(self, detail: dict) -> None:
		"""Mark a document's bar red after its summarization failed all retries.

		The run keeps going for the other documents; this only flips the one
		row so the failure is visible live instead of sitting at "요약 대기 중".
		"""
		doc_id = str(detail.get("doc_id") or "").strip()
		if not doc_id:
			return
		bar = self._doc_bars.get(doc_id)
		if bar is None:
			# Failure event with no prior fetch bar (polling lag): create one so
			# the failed document still shows up in the list.
			title = str(detail.get("title") or doc_id)
			index = len(self._doc_bars) + 1
			bar = DocumentBar(index=index, doc_id=doc_id, title=title, url="")
			self._doc_bars[doc_id] = bar
			self.documents_container.addWidget(bar)
			self.documents_header.setVisible(True)
			self.result_empty.setVisible(False)
			self.info_doc_count.set_value(self._doc_count_text())
			self.info_row_widget.setVisible(True)
		bar.set_failed(str(detail.get("reason") or ""))

	def _on_research_finished(self, response: dict[str, Any]) -> None:
		# Button re-enable is handled by JobManager.busy_changed → _sync_busy_state.
		self._stop_progress_poller()
		try:
			load_bootstrap_state()
		except Exception:
			pass
		workspace_name = str(response.get("workspaceName") or response.get("workspaceId") or "")
		if workspace_name:
			self.workspaceChanged.emit(workspace_name)
		self._render_result(response, from_live=True)

	def _show_research_error(self) -> None:
		"""Surface the persisted failure detail when the progress bar is clicked.

		Two cases share the clickable progress bar: a partial run (some
		documents failed to summarize) shows the per-document failure list; a
		fully failed run shows the single error message.
		"""
		if self._research_failed_documents:
			QMessageBox.warning(
				self,
				"일부 문서 요약 실패",
				self._format_failed_documents(self._research_failed_documents),
			)
			return
		if self._research_error_message:
			QMessageBox.critical(self, "조사 오류", self._research_error_message)

	@staticmethod
	def _format_failed_documents(failed_documents: list[dict[str, Any]]) -> str:
		lines: list[str] = []
		for item in failed_documents:
			if not isinstance(item, dict):
				continue
			doc_id = str(item.get("docId") or "?")
			title = str(item.get("title") or doc_id)
			reason = str(item.get("reason") or "사유 불명")
			lines.append(f"• 문서 {doc_id} — {title}\n   사유: {reason}")
		return "\n\n".join(lines) if lines else "실패한 문서 정보가 없습니다."

	def set_workspace_by_name(self, _workspace_name: str) -> None:
		self._workspace_id = current_workspace_id()
		self._load_existing_result()

	def _load_existing_result(self) -> None:
		self._workspace_id = current_workspace_id()
		# Workspace just changed (or the page is being loaded fresh): drop
		# any DocumentBar widgets from the previous workspace before we
		# render the new one. Without this, `_reconcile_documents` would
		# leave the previous workspace's bars in place (since `doc_id` is
		# workspace-relative — workspace A's "001" and B's "001" are
		# different documents) and only append B's tail end after A's bars.
		self._clear_documents()

		# list_research_jobs is a blocking HTTP call — run it off the UI thread
		# so app startup and workspace switches never freeze the window. The
		# token guards against an out-of-order completion (a newer workspace
		# load, or a research run the user started meanwhile) being clobbered.
		self._result_load_token += 1
		token = self._result_load_token
		workspace_id = self._workspace_id

		def _load() -> list:
			return AgentController().list_research_jobs(100)

		def _apply(jobs: object) -> None:
			if token != self._result_load_token:
				return
			job_list = jobs if isinstance(jobs, list) else []
			current_job = next(
				(
					job
					for job in job_list
					if isinstance(job, dict)
					and str(job.get("workspaceId") or "") == workspace_id
				),
				None,
			)
			if current_job is None:
				self._show_message("조사 실행 후 agent 결과가 여기에 표시됩니다.")
				return
			self._render_result(current_job)

		def _failed(_message: str) -> None:
			if token != self._result_load_token:
				return
			self._show_message("조사 실행 후 agent 결과가 여기에 표시됩니다.")

		get_job_manager().run_detached(_load, on_success=_apply, on_error=_failed)

	def _on_research_failed(self, message: str) -> None:
		# Button re-enable is handled by JobManager.busy_changed → _sync_busy_state.
		self._stop_progress_poller()
		self._clear_documents()
		self.info_row_widget.setVisible(False)
		self.documents_header.setVisible(False)
		self.result_empty.setVisible(False)
		self._research_failed_documents = []
		self._research_error_message = message or "알 수 없는 오류가 발생했습니다."
		self.progress_bar.mark_failed(self._research_error_message)

	def _render_result(self, response: dict[str, Any], from_live: bool = False) -> None:
		"""Apply final/persisted job state to the result card.

		Header info (progress bar, info tiles) is always refreshed from the
		response. Document bars are *reconciled* in place: bars that were
		already created from live events are kept and merely have their
		summary path filled in if needed; bars for documents that the live
		stream missed are appended. This preserves the realtime UX while
		guaranteeing the final view matches the persisted truth.

		`from_live` is True only when called straight off a just-finished run,
		in which case the completed bar animates to 100%; when restoring a
		persisted job it snaps there instead.
		"""
		status = str(response.get("status") or "").lower().strip()
		error_message = str(response.get("error") or "").strip()
		failed_documents = response.get("failedDocuments", [])
		if not isinstance(failed_documents, list):
			failed_documents = []
		failed_documents = [d for d in failed_documents if isinstance(d, dict)]
		self._research_failed_documents = failed_documents

		# Backend-reported total run duration. It is persisted in the workspace
		# (summary/timing.json), so it is present both for a just-finished live
		# run and for a job restored from disk — letting the elapsed time keep
		# showing after completion instead of vanishing on reload.
		elapsed_seconds = response.get("elapsedSeconds")

		if status == "completed":
			self._research_error_message = ""
			self.progress_bar.mark_completed(animate=from_live, elapsed_seconds=elapsed_seconds)
		elif status == "partial":
			# Some documents failed to summarize but the run as a whole
			# finished — a partial success, not an outright failure.
			self._research_error_message = ""
			self.progress_bar.mark_partial(animate=from_live, elapsed_seconds=elapsed_seconds)
		elif status == "failed":
			self._research_error_message = error_message or "조사 작업이 실패했습니다."
			self.progress_bar.mark_failed(self._research_error_message, elapsed_seconds=elapsed_seconds)
		elif status == "running":
			self._research_error_message = ""
			self.progress_bar.restore_running(self._progress_estimate)
		else:
			self._research_error_message = ""
			self.progress_bar.set_idle()

		documents = response.get("documents", [])
		if not isinstance(documents, list):
			documents = []
		documents = [doc for doc in documents if isinstance(doc, dict)]

		# "작업 이름" 타일은 term-grounding으로 만들어진 워크스페이스 이름을 보여준다.
		# jobId(rs_xxxxx)는 그 이름이 없을 때만 폴백으로 사용 — jobId를 먼저 쓰면
		# 라이브 중 _adopt_new_workspace가 채운 term 값이 가려진다.
		job_name = str(
			response.get("workspaceName")
			or response.get("workspaceId")
			or response.get("jobId")
			or "-"
		)
		final_path_raw = str(response.get("finalPath") or "").strip()
		self._final_path = Path(final_path_raw) if final_path_raw else None

		# Persisted/finished jobs carry the doc cap they ran with; adopt it so
		# the count tile's denominator matches what this job actually used.
		max_docs_value = response.get("maxDocs")
		try:
			if max_docs_value is not None and int(max_docs_value) > 0:
				self._max_docs = int(max_docs_value)
		except (TypeError, ValueError):
			pass

		self.info_job_name.set_value(job_name)
		self.info_save_path.set_value(final_path_raw or "-")
		self.info_row_widget.setVisible(True)

		self._reconcile_documents(documents)
		# Flip any failed documents red. Done after reconcile so it overrides
		# the default pending/ready state for those rows.
		self._apply_failed_documents(failed_documents)

		# The document list is always shown when there are bars: a partial
		# failure must never hide the documents that succeeded.
		if self._doc_bars:
			self.documents_header.setVisible(True)
			self.result_empty.setVisible(False)
		else:
			self.documents_header.setVisible(False)
			if status in ("completed", "partial"):
				self.result_empty.setText("수집된 문서가 없습니다.")
				self.result_empty.setVisible(True)
			else:
				self.result_empty.setVisible(False)

	def _apply_failed_documents(self, failed_documents: list[dict[str, Any]]) -> None:
		"""Mark every failed document's bar red, creating a bar if one is missing.

		A failed document has no ``doc_*.md`` file, so ``_reconcile_documents``
		leaves its bar in the pending state — this turns those rows into the
		explicit "요약 실패" state.
		"""
		for item in failed_documents:
			doc_id = str(item.get("docId") or "").strip()
			if not doc_id:
				continue
			bar = self._doc_bars.get(doc_id)
			if bar is None:
				title = str(item.get("title") or doc_id)
				index = len(self._doc_bars) + 1
				bar = DocumentBar(index=index, doc_id=doc_id, title=title, url="")
				self._doc_bars[doc_id] = bar
				self.documents_container.addWidget(bar)
			bar.set_failed(str(item.get("reason") or ""))
		if failed_documents:
			self.info_doc_count.set_value(self._doc_count_text())

	def _reconcile_documents(self, documents: list[dict[str, Any]]) -> None:
		"""Merge the authoritative document list from the API response with
		any bars created from live events. Existing bars are preserved;
		missing ones are appended at the end; ready summaries are filled in.
		"""
		summary_dir = self._summary_dir_from_final_path(self._final_path)
		for item in documents:
			doc_id = str(item.get("docId") or "").strip()
			if not doc_id:
				continue
			bar = self._doc_bars.get(doc_id)
			if bar is None:
				title = str(item.get("title") or "Untitled")
				url = str(item.get("url") or "")
				index = len(self._doc_bars) + 1
				bar = DocumentBar(index=index, doc_id=doc_id, title=title, url=url)
				self._doc_bars[doc_id] = bar
				self.documents_container.addWidget(bar)
			if summary_dir is not None:
				summary_path = summary_dir / f"doc_{doc_id}.md"
				if summary_path.exists():
					bar.set_summary_ready(summary_path)
		self.info_doc_count.set_value(self._doc_count_text())

	def _summary_dir_from_final_path(self, final_path: Path | None) -> Path | None:
		if final_path is None:
			return None
		try:
			return final_path.parent / "summary"
		except Exception:
			return None

	def _clear_documents(self) -> None:
		while self.documents_container.count():
			item = self.documents_container.takeAt(0)
			widget = item.widget() if item is not None else None
			if widget is not None:
				widget.setParent(None)
				widget.deleteLater()
		self._doc_bars.clear()

	def _apply_result_empty_theme(self, *args) -> None:
		self.result_empty.setStyleSheet(
			f"color: {theme.color('text.secondary.gray')}; "
			f"background-color: {theme.color('surface.muted')}; "
			f"border: 1px dashed {theme.color('border.strong')}; "
			"border-radius: 10px; padding: 24px; font-weight: 600;"
		)

	def _show_message(self, text: str) -> None:
		self._clear_documents()
		self._research_error_message = ""
		self._research_failed_documents = []
		self.progress_bar.set_idle()
		self.info_row_widget.setVisible(False)
		self.documents_header.setVisible(False)
		self.result_empty.setText(text)
		self.result_empty.setVisible(True)

	def _remove_reference_url(self, target: QFrame) -> None:
		if len(self._url_rows) == 1:
			self._url_rows[0][1].clear()
			return

		for index, (row, _url_input) in enumerate(self._url_rows):
			if row is target:
				self._url_rows.pop(index)
				break

		self.url_list.removeWidget(target)
		target.deleteLater()
