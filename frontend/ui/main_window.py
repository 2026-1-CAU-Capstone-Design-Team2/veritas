from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import (
	QEasingCurve,
	QEvent,
	QParallelAnimationGroup,
	QPoint,
	QPointF,
	QPropertyAnimation,
	QSize,
	Qt,
	QThread,
	Signal,
)
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
	QApplication,
	QFrame,
	QGraphicsDropShadowEffect,
	QGraphicsOpacityEffect,
	QHBoxLayout,
	QLabel,
	QMainWindow,
	QPushButton,
	QScrollArea,
	QStackedWidget,
	QVBoxLayout,
	QWidget,
)

from ..api_common import ApiError, current_workspace_id
from ..theme import build_main_window_qss, theme
from ..components.cards import CardWidget
from ..controllers import (
	AgentController,
	JobCategory,
	get_chat_bus,
	get_job_manager,
	get_screen_event_store,
)
from ..components.stepper import WorkflowStepper
from .pages.dashboard_page import DashboardPage
from .pages.document_page import DocumentPage
from .pages.draft_page import DraftPage
from .pages.guide_page import GuidePage
from .pages.research_page import ResearchPage
from .pages.settings_page import SettingsPage
from .pages.writing_page import DocumentAssistPage
from .pages.verify_page import VerifyPage
from .pages.write_page import WritePage

from .sidebar import Sidebar
from .windows.document_assist_window import DocumentAssistWindow, VeritasTitleBar, render_history_html
from .windows.editor_window import EditorWindow
from .windows.win_snap import WindowsSnapMixin, set_window_topmost


def _theme_icon(kind: str, color: str, size: int = 16) -> QIcon:
	"""Hand-painted moon / sun glyph for the light-dark toggle (no emoji/font
	dependency, and it re-tints with the button's ink colour)."""
	scale = 2
	phys = size * scale
	pixmap = QPixmap(phys, phys)
	pixmap.fill(Qt.transparent)
	painter = QPainter(pixmap)
	painter.setRenderHint(QPainter.Antialiasing, True)
	qcolor = QColor(color)
	cx = cy = phys / 2.0
	if kind == "moon":
		# Solid disc with an offset disc carved out → crescent.
		r = phys * 0.32
		painter.setPen(Qt.NoPen)
		painter.setBrush(qcolor)
		painter.drawEllipse(QPointF(cx - phys * 0.05, cy), r, r)
		painter.setCompositionMode(QPainter.CompositionMode_Clear)
		painter.drawEllipse(QPointF(cx + phys * 0.15, cy - phys * 0.09), r, r)
		painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
	else:  # sun
		r = phys * 0.18
		pen = QPen(qcolor)
		pen.setWidthF(1.6 * scale)
		pen.setCapStyle(Qt.RoundCap)
		painter.setPen(pen)
		painter.setBrush(Qt.NoBrush)
		painter.drawEllipse(QPointF(cx, cy), r, r)
		inner, outer = r * 1.55, r * 2.25
		for i in range(8):
			angle = math.pi * i / 4.0
			dx, dy = math.cos(angle), math.sin(angle)
			painter.drawLine(
				QPointF(cx + dx * inner, cy + dy * inner),
				QPointF(cx + dx * outer, cy + dy * outer),
			)
	painter.end()
	return QIcon(pixmap)


class ScreenEventPollWorker(QThread):
	eventsReceived = Signal(list)
	pollError = Signal(str)

	def __init__(
		self,
		agent_controller: "AgentController",
		parent: QWidget | None = None,
	) -> None:
		super().__init__(parent)
		self._agent = agent_controller
		self._cursor = 0
		self._stop = False
		# Poll fast even when idle: a short streamed answer can finish inside a
		# single slow idle window, so a 3s interval would show it as one lump.
		# Catching the stream start within ~0.3s keeps short answers streaming.
		self._sleep_seconds = 0.3
		# While an answer is mid-stream, poll a touch faster for smoother growth.
		self._fast_seconds = 0.15

	def request_stop(self) -> None:
		self._stop = True

	def reset_cursor(self) -> None:
		self._cursor = 0

	def run(self) -> None:  # type: ignore[override]
		while not self._stop:
			streaming = False
			try:
				response = self._agent.get_screen_monitoring_events(
					since=self._cursor, limit=20, workspace_id=current_workspace_id()
				)
				items = response.get("items", []) if isinstance(response, dict) else []
				if isinstance(items, list) and items:
					self._cursor = int(response.get("nextCursor") or self._cursor)
					self.eventsReceived.emit(items)
					# A partial event means an answer is mid-stream; keep polling
					# fast until it completes so the card fills in smoothly.
					streaming = any(
						isinstance(it, dict) and it.get("partial") for it in items
					)
			except Exception as e:
				self.pollError.emit(str(e))
			delay = self._fast_seconds if streaming else self._sleep_seconds
			for _ in range(max(1, int(delay * 20))):
				if self._stop:
					return
				self.msleep(50)


class AnimatedStackedWidget(QStackedWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._anim: QPropertyAnimation | None = None

	def setCurrentIndexAnimated(self, index: int) -> None:
		if index == self.currentIndex():
			return

		super().setCurrentIndex(index)
		current = self.currentWidget()
		if current is None:
			return

		effect = QGraphicsOpacityEffect(current)
		current.setGraphicsEffect(effect)

		anim = QPropertyAnimation(effect, b"opacity", self)
		anim.setDuration(230)
		anim.setStartValue(0.0)
		anim.setEndValue(1.0)
		anim.setEasingCurve(QEasingCurve.OutCubic)

		def cleanup() -> None:
			current.setGraphicsEffect(None)

		anim.finished.connect(cleanup)
		anim.start()
		self._anim = anim


class MainWindow(WindowsSnapMixin, QMainWindow):
	STEP_ORDER = ["research", "document", "verify", "draft", "document_assist", "write"]

	def __init__(self) -> None:
		super().__init__()
		self.setWindowTitle("VERITAS")
		# Frameless + translucent: the app wears the same rounded, floating chrome
		# as the editor / assist windows — a custom VeritasTitleBar instead of the
		# black OS title bar — so all three windows share one look.
		self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
		self.setAttribute(Qt.WA_TranslucentBackground, True)
		self.setMouseTracking(True)
		self._apply_window_icon()
		self._apply_default_geometry()
		# Low enough to snap to a half-screen; the hero subtitle wraps and the
		# sidebar auto-collapses on narrow widths so the layout floor stays under it.
		self.setMinimumSize(660, 600)

		# Edge-resize bookkeeping — a frameless window loses the native grips, so we
		# drive resizing from the central widget's transparent margin (eventFilter).
		self._resize_margin = 8
		self._resize_edges: set[str] = set()
		self._resize_origin: QPoint | None = None
		self._resize_geometry = None

		self.setStyleSheet(build_main_window_qss(theme.palette()))

		container = QWidget()
		container.setObjectName("AppRoot")
		container.setMouseTracking(True)
		container.installEventFilter(self)
		self.setCentralWidget(container)

		self._root_layout = QVBoxLayout(container)
		self._root_layout.setContentsMargins(10, 10, 10, 10)
		self._root_layout.setSpacing(0)

		# Rounded, shadowed panel carries the app gradient; the transparent margin
		# around it lets the desktop show through for the floating-card look.
		self.app_panel = QFrame()
		self.app_panel.setObjectName("AppPanel")
		panel_shadow = QGraphicsDropShadowEffect(self.app_panel)
		panel_shadow.setBlurRadius(28)
		panel_shadow.setXOffset(0)
		panel_shadow.setYOffset(10)
		panel_shadow.setColor(QColor(15, 23, 42, 45))
		self.app_panel.setGraphicsEffect(panel_shadow)
		self._root_layout.addWidget(self.app_panel)

		panel_layout = QVBoxLayout(self.app_panel)
		panel_layout.setContentsMargins(0, 0, 0, 0)
		panel_layout.setSpacing(0)

		self.title_bar = VeritasTitleBar(self)
		panel_layout.addWidget(self.title_bar)

		shell = QHBoxLayout()
		shell.setObjectName("ShellLayout")
		shell.setContentsMargins(20, 20, 20, 20)
		shell.setSpacing(16)

		self.sidebar_visible = True
		self._sidebar_expanded_width = 228
		self._sidebar_collapsed_width = 72
		self._sidebar_anim_group: QParallelAnimationGroup | None = None
		# True only while WE auto-collapsed the sidebar (narrow window). Lets us
		# auto-expand it again when the window widens, without overriding a manual
		# toggle (the manual handler clears this flag).
		self._sidebar_auto_collapsed = False

		self.sidebar = Sidebar()
		self.sidebar.setMinimumWidth(self._sidebar_expanded_width)
		self.sidebar.setMaximumWidth(self._sidebar_expanded_width)
		self.sidebar.navRequested.connect(self._navigate)
		self.sidebar.toggleRequested.connect(self._on_sidebar_toggle_requested)

		center_panel = QFrame()
		center_panel.setObjectName("CenterPanel")
		center_layout = QVBoxLayout(center_panel)
		center_layout.setContentsMargins(16, 16, 16, 16)
		center_layout.setSpacing(14)

		top_hero = QFrame()
		top_hero.setObjectName("TopHero")
		top_hero_layout = QHBoxLayout(top_hero)
		top_hero_layout.setContentsMargins(16, 14, 16, 14)
		top_hero_layout.setSpacing(10)

		hero_text_col = QVBoxLayout()
		hero_text_col.setContentsMargins(0, 0, 0, 0)
		hero_text_col.setSpacing(3)

		self.section_title = QLabel("대시보드")
		self.section_title.setObjectName("SectionTitle")
		self.section_desc = QLabel("오늘 워크플로우 진행 상태를 한눈에 확인하세요.")
		self.section_desc.setObjectName("SectionDesc")
		# Wrap instead of forcing the hero row (and thus the window) wide.
		self.section_desc.setWordWrap(True)
		hero_text_col.addWidget(self.section_title)
		hero_text_col.addWidget(self.section_desc)

		top_hero_layout.addLayout(hero_text_col, 1)

		# Light/dark toggle — sits with the other hero actions and flips the
		# whole app theme live (every window re-styles via theme.themeChanged).
		self.theme_button = QPushButton()
		self.theme_button.setObjectName("TopActionButton")
		self.theme_button.setCursor(Qt.PointingHandCursor)
		self.theme_button.setIconSize(QSize(15, 15))
		self.theme_button.clicked.connect(self._toggle_theme)
		top_hero_layout.addWidget(self.theme_button, 0, Qt.AlignTop)

		self.editor_button = QPushButton("글쓰기")
		self.editor_button.setObjectName("AssistToggleButton")
		self.editor_button.setCursor(Qt.PointingHandCursor)
		self.editor_button.clicked.connect(self.open_editor_new)
		top_hero_layout.addWidget(self.editor_button, 0, Qt.AlignTop)

		self.assist_toggle_button = QPushButton("AI 보조창")
		self.assist_toggle_button.setObjectName("AssistToggleButton")
		self.assist_toggle_button.setCursor(Qt.PointingHandCursor)
		self.assist_toggle_button.clicked.connect(self.toggle_document_assist_window)
		top_hero_layout.addWidget(self.assist_toggle_button, 0, Qt.AlignTop)

		self.stepper = WorkflowStepper(["조사", "요약", "검증", "초안 생성", "문서 보조", "채팅"])

		self.pages = AnimatedStackedWidget()

		self.route_to_index: dict[str, int] = {}
		self.dashboard_page = DashboardPage()
		# 대시보드 "열기" → 글쓰기 에디터에 초안 시드.
		self.dashboard_page.openDraftRequested.connect(self._open_editor_from_draft)
		self._add_page("dashboard", self.dashboard_page)
		self.research_page = ResearchPage()
		self.research_page.workspaceChanged.connect(self.sidebar.set_current_workspace)
		self.research_page.workspaceChanged.connect(self._on_workspace_changed)
		# "이 보고서로 글쓰기" → open the editor seeded from the workspace's final.md.
		self.research_page.openEditorRequested.connect(self._open_editor_from_research)
		# Mid-research workspace switch: light reset so the user sees the
		# new workspace name in the sidebar and a clean chat panel without
		# tearing down the active research display.
		self.research_page.workspaceCreated.connect(self._on_research_workspace_created)
		self._add_page("research", self.research_page)
		self.verify_page = VerifyPage()
		self._add_page("verify", self.verify_page)
		self.draft_page = DraftPage()
		# 초안 결과의 "에디터로 보내기" → 생성된 초안을 에디터 창에 시드해서 연다.
		self.draft_page.openEditorRequested.connect(self._open_editor_from_draft)
		self._add_page("draft", self.draft_page)
		self._add_page("document_assist", DocumentAssistPage())
		self.write_page = WritePage()
		self._add_page("write", self.write_page)
		self.document_page = DocumentPage()
		self._add_page("document", self.document_page)
		self.settings_page = SettingsPage()
		self.settings_page.defaultWorkspaceChanged.connect(self.sidebar.set_current_workspace)
		self.sidebar.workspaceChanged.connect(self._on_workspace_changed)
		self._add_page("settings", self.settings_page)
		self._add_page("guide", GuidePage())

		center_layout.addWidget(top_hero)
		center_layout.addWidget(self.stepper)
		center_layout.addWidget(self.pages, 1)

		shell.addWidget(self.sidebar)
		shell.addWidget(center_panel, 1)
		panel_layout.addLayout(shell, 1)

		# Independent top-level windows (no parent) so each gets its own taskbar
		# button and minimises ('-') on its own; MainWindow.closeEvent closes them
		# so the app still exits cleanly when the main window is closed.
		self.document_assist_window = DocumentAssistWindow()
		self.editor_window = EditorWindow()
		self._agent_controller = AgentController()
		self._chat_bus = get_chat_bus()
		self.document_assist_window.messageSubmitted.connect(self._send_assist_window_message)
		self.document_assist_window.visibilityChanged.connect(self._on_assist_visibility_changed)
		self.document_assist_window.hide()
		self._assist_streaming = False
		# Monotonic guard for the background history-hydration fetch — see
		# _hydrate_assist_history_from_backend.
		self._assist_history_token = 0
		self._chat_bus.userMessageQueued.connect(self._on_chat_user_queued)
		self._chat_bus.assistantStreamStarted.connect(self._on_chat_stream_started)
		self._chat_bus.assistantChunk.connect(self._on_chat_stream_chunk)
		self._chat_bus.assistantCompleted.connect(self._on_chat_stream_completed)
		self._chat_bus.assistantFailed.connect(self._on_chat_stream_failed)
		# Floating assist window's chat input follows the global busy state
		# (locked while AutoSurvey runs or another chat is mid-stream).
		get_job_manager().busy_changed.connect(self._sync_assist_busy_state)
		self._sync_assist_busy_state()

		self._screen_monitor_worker: ScreenEventPollWorker | None = None
		self._screen_monitor_active = False
		# screen-monitoring 이벤트 broker — polling worker가 store에 append하면 보조창/페이지가 동시 구독.
		self._screen_event_store = get_screen_event_store()
		# 폴링 시작 조건: 보조창 visible OR document_assist 페이지 활성 (count > 0 인 동안 폴링).
		self._monitor_subscriber_count = 0
		self._active_route: str | None = None

		self._assist_toggle_shortcut = QShortcut(QKeySequence("Ctrl+Shift+A"), self)
		self._assist_toggle_shortcut.activated.connect(self.toggle_document_assist_window)
		self._editor_shortcut = QShortcut(QKeySequence("Ctrl+Shift+W"), self)
		self._editor_shortcut.activated.connect(self.open_editor_new)

		# Theme: keep the toggle label in sync and re-style on every mode change.
		theme.themeChanged.connect(self._on_theme_changed)
		self._sync_theme_button()

		self._enable_text_selection(container)
		# Keep the title bar drag-friendly: its brand / logo labels must not grab
		# the mouse press for text selection (which would block window dragging).
		for title_label in self.title_bar.findChildren(QLabel):
			title_label.setTextInteractionFlags(Qt.NoTextInteraction)
		self._navigate("dashboard")

		# Windows 10/11 Snap Layouts · Aero Snap on this frameless window (no-op
		# elsewhere). Must run after the title bar exists — the mixin hit-tests it.
		self._install_snap_layout()

	def closeEvent(self, event) -> None:  # type: ignore[override]
		# The editor/assist windows are now parent-less top-levels, so closing the
		# main window no longer auto-destroys them (and the app would stay alive on
		# their taskbar buttons). Close them explicitly so the app exits.
		for window in (getattr(self, "editor_window", None), getattr(self, "document_assist_window", None)):
			if window is not None:
				window.close()
		super().closeEvent(event)

	# --------------------------------------------------------- frameless chrome

	def changeEvent(self, event) -> None:  # type: ignore[override]
		# Maximising a frameless window should fill the screen edge-to-edge: drop
		# the floating shadow margin + rounded corners, and restore them on normal.
		if event.type() == QEvent.WindowStateChange and hasattr(self, "title_bar"):
			maximized = self.isMaximized()
			if maximized:
				self._root_layout.setContentsMargins(0, 0, 0, 0)
			else:
				self._root_layout.setContentsMargins(10, 10, 10, 10)
			for widget in (self.app_panel, self.title_bar):
				widget.setProperty("maximized", maximized)
				widget.style().unpolish(widget)
				widget.style().polish(widget)
			self.title_bar.maximize_button.set_role("restore" if maximized else "max")
		super().changeEvent(event)

	def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
		# The central widget owns the transparent margin around the panel; that ring
		# is where an edge-resize drag begins now that the OS grips are gone.
		if obj is self.centralWidget() and not self.isMaximized():
			etype = event.type()
			if etype == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
				edges = self._hit_resize_edges(event.position().toPoint())
				if edges:
					self._resize_edges = edges
					self._resize_origin = event.globalPosition().toPoint()
					self._resize_geometry = self.geometry()
					return True
			elif etype == QEvent.MouseMove:
				if self._resize_origin is not None and self._resize_geometry is not None:
					self._resize_to(event.globalPosition().toPoint())
					return True
				self._update_resize_cursor(event.position().toPoint())
			elif etype == QEvent.MouseButtonRelease and self._resize_edges:
				self._resize_edges = set()
				self._resize_origin = None
				self._resize_geometry = None
				self.centralWidget().unsetCursor()
				return True
		return super().eventFilter(obj, event)

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
		widget = self.centralWidget()
		if {"left", "top"} <= edges or {"right", "bottom"} <= edges:
			widget.setCursor(Qt.SizeFDiagCursor)
		elif {"right", "top"} <= edges or {"left", "bottom"} <= edges:
			widget.setCursor(Qt.SizeBDiagCursor)
		elif "left" in edges or "right" in edges:
			widget.setCursor(Qt.SizeHorCursor)
		elif "top" in edges or "bottom" in edges:
			widget.setCursor(Qt.SizeVerCursor)
		else:
			widget.unsetCursor()

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

	def show_document_assist_window(self) -> None:
		win = self.document_assist_window
		# Restore first if minimised — a plain show() leaves a minimised window
		# minimised (it would stay in the taskbar instead of coming up).
		if win.isMinimized():
			win.setWindowState(win.windowState() & ~Qt.WindowMinimized)
		win.show()
		win.raise_()
		win.activateWindow()
		# Re-assert always-on-top (it may have yielded the band when the editor
		# was last opened — see _yield_assist_topmost).
		set_window_topmost(int(win.winId()), True)

	def _yield_assist_topmost(self) -> None:
		"""Let the always-on-top assist window drop out of the topmost z-band so a
		freshly-opened editor (a normal window) can actually come to the front
		instead of being rendered underneath it. The assist re-asserts topmost the
		next time it is shown/toggled (show_document_assist_window)."""
		win = self.document_assist_window
		if win.isVisible():
			set_window_topmost(int(win.winId()), False)

	def toggle_document_assist_window(self) -> None:
		if self.document_assist_window.isVisible():
			self.document_assist_window.hide()
			return
		self.show_document_assist_window()

	def open_editor_new(self) -> None:
		"""Open the editor on a fresh blank document for the current workspace."""
		# Yield the assist's topmost band BEFORE presenting the editor, so the
		# editor's raise() lands it above the (now non-topmost) assist. Doing it
		# after would push the just-demoted assist back to the front.
		self._yield_assist_topmost()
		self.editor_window.open_document(current_workspace_id(), source="new")

	def _open_editor_from_research(self, workspace_id: str) -> None:
		"""Open the editor seeded from a workspace's final.md ("이 보고서로 글쓰기")."""
		self._yield_assist_topmost()
		self.editor_window.open_document(workspace_id or current_workspace_id(), source="final")

	def _open_editor_from_draft(self, workspace_id: str, markdown: str) -> None:
		"""Open the editor seeded with a generated draft ("에디터로 보내기")."""
		self._yield_assist_topmost()
		self.editor_window.open_document(
			workspace_id or current_workspace_id(), source="draft", seed_markdown=markdown
		)

	def _start_screen_monitoring(self) -> None:
		if self._screen_monitor_active:
			return
		try:
			self._agent_controller.start_screen_monitoring(current_workspace_id())
		except ApiError as e:
			self.document_assist_window.title_bar.status.setText("연결 실패")
			self.document_assist_window.add_chat_message(
				"VERITAS",
				f"화면 모니터링 시작 실패: {e}",
			)
			return
		self._screen_monitor_active = True
		self.document_assist_window.title_bar.status.setText("● 모니터링 중")
		worker = ScreenEventPollWorker(self._agent_controller, self)
		# raw items를 store에 push — 보조창/페이지 위젯이 각자 store.eventsAppended를 구독해 렌더.
		worker.eventsReceived.connect(self._screen_event_store.append)
		worker.pollError.connect(self._on_proactive_poll_error)
		self._screen_monitor_worker = worker
		worker.start()

	def _stop_screen_monitoring(self) -> None:
		if not self._screen_monitor_active:
			return
		self._screen_monitor_active = False
		worker = self._screen_monitor_worker
		self._screen_monitor_worker = None
		if worker is not None:
			worker.request_stop()
			worker.wait(2000)
		try:
			self._agent_controller.stop_screen_monitoring()
		except ApiError:
			pass

	def _on_proactive_poll_error(self, message: str) -> None:
		print(f"[screen_monitoring][poll][warn] {message}")

	def _sync_assist_busy_state(self) -> None:
		blocked = get_job_manager().is_blocked(JobCategory.CHAT)
		self.document_assist_window.input_bar.setEnabled(not blocked)
		if blocked:
			self.document_assist_window.input_bar.input.setPlaceholderText(
				"다른 작업이 진행 중입니다. 잠시만 기다려 주세요..."
			)
		else:
			# Restore the mode-specific placeholder.
			current_mode = self.document_assist_window.input_bar.mode()
			self.document_assist_window.input_bar.set_mode(current_mode, emit=False)

	def _on_assist_visibility_changed(self, visible: bool) -> None:
		if visible:
			if not self._assist_streaming:
				self._hydrate_assist_history_from_backend()
			self._increment_monitor_subscriber()
		else:
			self._decrement_monitor_subscriber()

	def _increment_monitor_subscriber(self) -> None:
		"""활성 subscriber 카운트++. 0→1 전이 시 폴링 시작."""
		self._monitor_subscriber_count += 1
		if self._monitor_subscriber_count == 1:
			self._start_screen_monitoring()

	def _decrement_monitor_subscriber(self) -> None:
		"""활성 subscriber 카운트--. 1→0 전이 시 폴링 중단."""
		if self._monitor_subscriber_count <= 0:
			return
		self._monitor_subscriber_count -= 1
		if self._monitor_subscriber_count == 0:
			self._stop_screen_monitoring()

	def _update_monitor_subscriber_for_route(self, new_route: str) -> None:
		"""라우트 전환 시 document_assist 진입/이탈에 따라 카운트 조정."""
		if self._active_route == new_route:
			return
		if self._active_route == "document_assist":
			self._decrement_monitor_subscriber()
		if new_route == "document_assist":
			self._increment_monitor_subscriber()
		self._active_route = new_route

	def _restart_screen_monitoring_for_workspace_change(self) -> None:
		"""워크스페이스 전환 시 backend monitoring을 새 workspace로 재시작.
		비활성 상태면 아무 동작 안 함 — 다음 활성화 시 새 workspace_id로 자연스럽게 시작.
		재시작 흐름: stop → worker 종료/대기 → start(new workspace_id) + 새 worker (cursor=0).
		"""
		if not self._screen_monitor_active:
			return
		self._stop_screen_monitoring()
		self._start_screen_monitoring()

	def _send_assist_window_message(self, message: str) -> None:
		mode = self.document_assist_window.input_bar.mode()
		workspace_id = current_workspace_id()
		if not self._chat_bus.send(workspace_id, message, mode):
			self.document_assist_window.add_chat_message(
				"VERITAS", "이미 답변을 생성하고 있어요. 잠시만 기다려 주세요."
			)

	def _hydrate_assist_history_from_backend(self) -> None:
		# Fetch the history AND render its markdown on a worker thread — both the
		# HTTP round-trip and the per-message parse used to run on the UI thread
		# every time the assist window was shown.
		self._assist_history_token += 1
		token = self._assist_history_token
		workspace_id = current_workspace_id()
		controller = self._agent_controller

		def _load() -> list:
			history = controller.get_chat_history(workspace_id)
			return render_history_html(history if isinstance(history, list) else [])

		def _apply(prepared: object) -> None:
			# Drop a stale result: a newer hydrate request, or a chat stream that
			# started while we were fetching — applying now would clear the live
			# streaming bubble.
			if token != self._assist_history_token or self._assist_streaming:
				return
			self.document_assist_window.hydrate_history(
				prepared if isinstance(prepared, list) else []
			)

		def _failed(_message: str) -> None:
			if token != self._assist_history_token or self._assist_streaming:
				return
			self.document_assist_window.hydrate_history([])

		get_job_manager().run_detached(_load, on_success=_apply, on_error=_failed)

	def _on_chat_user_queued(self, _workspace_id: str, text: str) -> None:
		# Both views render the user bubble in response to the bus event.
		self.document_assist_window.add_chat_message("나", text)

	def _on_chat_stream_started(self) -> None:
		self._assist_streaming = True
		self.document_assist_window.chat_panel.start_streaming_assistant("VERITAS")
		self.document_assist_window.title_bar.status.setText("● 답변 생성 중")

	def _on_chat_stream_chunk(self, chunk: str) -> None:
		if not self._assist_streaming:
			return
		self.document_assist_window.chat_panel.append_streaming_chunk(chunk)

	def _on_chat_stream_completed(self, text: str) -> None:
		if not self._assist_streaming:
			return
		self._assist_streaming = False
		self.document_assist_window.chat_panel.finalize_streaming_assistant(text)
		self.document_assist_window.title_bar.status.setText("● 대기")

	def _on_chat_stream_failed(self, error: str) -> None:
		if not self._assist_streaming:
			return
		self._assist_streaming = False
		self.document_assist_window.chat_panel.cancel_streaming_assistant(error)
		self.document_assist_window.title_bar.status.setText("● 오류")

	def _on_workspace_changed(self, workspace_name: str) -> None:
		# 워크스페이스 경계: 이전 workspace의 screen 이벤트 잔류 방지 — 양쪽 suggestion_list 동시 reset.
		self._screen_event_store.clear()
		# backend monitoring도 새 workspace_id로 재시작 (active 상태에서만).
		self._restart_screen_monitoring_for_workspace_change()
		self.settings_page.set_default_workspace_by_name(workspace_name)
		self.draft_page.set_workspace_by_name(workspace_name)
		self.research_page.set_workspace_by_name(workspace_name)
		self.write_page.set_workspace_by_name(workspace_name)
		self.verify_page.set_workspace_by_name(workspace_name)
		self.document_page.refresh()

	def _on_research_workspace_created(self, workspace_id: str, workspace_name: str) -> None:
		"""Mid-research workspace adoption.

		Unlike :meth:`_on_workspace_changed`, this does NOT call
		`research_page.set_workspace_by_name` (which would reload the
		page state from disk and wipe the live DocumentBar timeline). It
		only touches the surfaces that should reflect the new workspace
		immediately:

		- bootstrap state cache (so `current_workspace_id()` resolves to
		  the new id everywhere)
		- sidebar footer + dropdown
		- write_page chat panel (cleared because the new workspace has
		  no chat history yet — and chat is JobManager-blocked during
		  research so there's no in-flight stream to interrupt)
		- document_assist window chat panel (same reason)
		"""
		try:
			from ..api_common import load_bootstrap_state

			load_bootstrap_state()
		except Exception:
			pass
		self.sidebar.set_current_workspace(workspace_name)

		self.write_page._workspace_id = workspace_id
		self.write_page.chat_panel.clear_messages()
		self.write_page.chat_panel.add_message(
			"VERITAS",
			"새 워크스페이스가 준비되었습니다. 조사가 완료되면 대화를 시작할 수 있습니다.",
			False,
		)

		self.document_assist_window.hydrate_history([])
		# 새 워크스페이스: 이전 screen 이벤트 잔류 방지 + backend monitoring 재시작.
		self._screen_event_store.clear()
		self._restart_screen_monitoring_for_workspace_change()

	def _toggle_sidebar(self, animate: bool = True) -> None:
		start = self.sidebar.width()
		end = self._sidebar_collapsed_width if self.sidebar_visible else self._sidebar_expanded_width

		# Width-driven auto-collapse (animate=False) snaps instantly so the layout
		# fits the new window size with no transient clip; the manual toggle button
		# keeps the smooth animation.
		if not animate:
			self.sidebar.set_compact(self.sidebar_visible)
			self.sidebar_visible = not self.sidebar_visible
			self.sidebar.setMinimumWidth(end)
			self.sidebar.setMaximumWidth(end)
			self._sidebar_anim_group = None
			return

		min_anim = QPropertyAnimation(self.sidebar, b"minimumWidth", self)
		min_anim.setDuration(220)
		min_anim.setStartValue(start)
		min_anim.setEndValue(end)
		min_anim.setEasingCurve(QEasingCurve.InOutCubic)

		max_anim = QPropertyAnimation(self.sidebar, b"maximumWidth", self)
		max_anim.setDuration(220)
		max_anim.setStartValue(start)
		max_anim.setEndValue(end)
		max_anim.setEasingCurve(QEasingCurve.InOutCubic)

		group = QParallelAnimationGroup(self)
		group.addAnimation(min_anim)
		group.addAnimation(max_anim)
		group.start()

		self._sidebar_anim_group = group
		self.sidebar.set_compact(self.sidebar_visible)
		self.sidebar_visible = not self.sidebar_visible

	def _on_sidebar_toggle_requested(self) -> None:
		# A manual click takes ownership: clear the auto-collapse flag so the
		# width-driven logic in resizeEvent never overrides the user's choice
		# (a manual collapse won't be auto-expanded; a manual expand stays).
		self._sidebar_auto_collapsed = False
		self._toggle_sidebar()

	def resizeEvent(self, event) -> None:  # type: ignore[override]
		# Auto-collapse the sidebar on narrow widths (so the window can shrink to
		# a half-screen) and restore it when wide again — reusing the existing
		# animated _toggle_sidebar / Sidebar.set_compact path. A 900/940 px
		# hysteresis band prevents flapping at the threshold; sidebar_visible
		# flips synchronously in _toggle_sidebar so each crossing fires once.
		super().resizeEvent(event)
		# setGeometry runs in __init__ before the sidebar exists, and a resize can
		# arrive then — bail until the chrome is built so we never crash on open.
		if not hasattr(self, "sidebar"):
			return
		width = event.size().width()
		if self.sidebar_visible and width < 900:
			self._sidebar_auto_collapsed = True
			self._toggle_sidebar(animate=False)
		elif (not self.sidebar_visible) and self._sidebar_auto_collapsed and width >= 940:
			self._sidebar_auto_collapsed = False
			self._toggle_sidebar(animate=False)

	def _enable_text_selection(self, root: QWidget) -> None:
		for label in root.findChildren(QLabel):
			label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)

	def _apply_window_icon(self) -> None:
		icon_path = Path(__file__).resolve().parent / "public" / "images" / "veritas_logo.ico"
		if icon_path.exists():
			self.setWindowIcon(QIcon(str(icon_path)))

	def _apply_default_geometry(self) -> None:
		screen = QApplication.primaryScreen()
		if screen is None:
			self.resize(1200, 820)
			return

		available = screen.availableGeometry()
		width = max(1120, int(available.width() * 0.5))
		height = max(760, int(available.height() * 0.88))

		width = min(width, available.width())
		height = min(height, available.height())

		x = available.x() + (available.width() - width) // 2
		y = available.y() + (available.height() - height) // 2
		self.setGeometry(x, y, width, height)

	def _add_page(self, route: str, widget: QWidget) -> None:
		# WritePage 와 DraftPage 는 고정 헤더/내비 + 내부 스크롤 본문을 스스로 관리한다.
		# 이들을 PageScroll 로 한 번 더 감싸면 스크롤 안에 스크롤(이중 스크롤)이 생기므로
		# 감싸지 않고, 나머지 페이지만 표준 외부 스크롤로 감싼다.
		page_widget = widget if isinstance(widget, (WritePage, DraftPage)) else self._wrap_page_with_scroll(widget)
		index = self.pages.addWidget(page_widget)
		self.route_to_index[route] = index

	def _wrap_page_with_scroll(self, widget: QWidget) -> QScrollArea:
		scroll = QScrollArea()
		scroll.setObjectName("PageScroll")
		scroll.setWidgetResizable(True)
		scroll.setFrameShape(QFrame.NoFrame)
		scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
		scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
		scroll.setWidget(widget)
		return scroll

	def _navigate(self, route: str) -> None:
		index = self.route_to_index.get(route)
		if index is None:
			return

		section_map = {
			"dashboard": ("대시보드", "오늘 진행 상태를 한눈에 확인하세요."),
			"research": ("조사", "조사 주제와 레퍼런스 사이트를 입력해 검증 준비를 진행합니다."),
			"verify": ("정합성 검증", "출처 신뢰도와 사실 일치성을 검토합니다."),
			"draft": ("초안 생성", "선택한 워크스페이스를 바탕으로 복사 가능한 초안을 생성합니다."),
			"document_assist": ("문서 보조", "실시간 문서 작성 보조 내용을 확인합니다."),
			"write": ("AI 채팅", "워크스페이스 기반 AI와 채팅이 가능합니다."),
			"document": ("요약", "최종 보고서 요약본을 확인합니다."),
			"settings": ("설정", "모델명과 로컬 접근 폴더를 구성합니다."),
			"guide": ("가이드", "VERITAS 사용법을 단계별로 안내합니다."),
		}
		title, desc = section_map.get(route, ("대시보드", ""))
		self.section_title.setText(title)
		self.section_desc.setText(desc)

		self.sidebar.set_active(route)
		self.pages.setCurrentIndexAnimated(index)
		current_widget = self.pages.currentWidget()
		page = current_widget.widget() if isinstance(current_widget, QScrollArea) else current_widget
		refresh = getattr(page, "refresh", None)
		if callable(refresh):
			refresh()

		is_workflow_route = route in self.STEP_ORDER
		self.stepper.setVisible(is_workflow_route)

		if is_workflow_route:
			self.stepper.set_current_step(self.STEP_ORDER.index(route))

		# document_assist 페이지 진입/이탈 → polling 활성 카운트 조정.
		self._update_monitor_subscriber_for_route(route)

	def _toggle_theme(self) -> None:
		theme.toggle()

	def _on_theme_changed(self, _mode: str) -> None:
		self.setStyleSheet(build_main_window_qss(theme.palette()))
		self._sync_theme_button()

	def _sync_theme_button(self) -> None:
		"""Label the hero toggle with the mode it switches *to*, with a painted icon."""
		ink = theme.color("hero.btn.text")
		if theme.is_dark():
			self.theme_button.setText("Light Mode")
			self.theme_button.setIcon(_theme_icon("sun", ink))
			self.theme_button.setToolTip("Switch to light mode")
		else:
			self.theme_button.setText("Dark Mode")
			self.theme_button.setIcon(_theme_icon("moon", ink))
			self.theme_button.setToolTip("Switch to dark mode")
