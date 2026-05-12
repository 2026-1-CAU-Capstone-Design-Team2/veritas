from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEasingCurve, QParallelAnimationGroup, QPropertyAnimation, Qt
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
	QApplication,
	QFrame,
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
from ..components.cards import CardWidget
from ..controllers import AgentController
from ..components.stepper import WorkflowStepper
from .pages.dashboard_page import DashboardPage
from .pages.document_page import DocumentPage
from .pages.draft_page import DraftPage
from .pages.feedback_page import FeedbackPage
from .pages.research_page import ResearchPage
from .pages.settings_page import SettingsPage
from .pages.writing_page import DocumentAssistPage
from .pages.verify_page import VerifyPage
from .pages.write_page import WritePage

from .sidebar import Sidebar
from .windows.document_assist_window import DocumentAssistWindow


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


class PlaceholderPage(QWidget):
	def __init__(self, title: str, detail: str, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(12)

		card = CardWidget(title)
		text = QLabel(detail)
		text.setWordWrap(True)
		text.setObjectName("PageSubtitle")
		card.layout.addWidget(text)

		root.addWidget(card)
		root.addStretch(1)


class MainWindow(QMainWindow):
	STEP_ORDER = ["research", "verify", "draft", "document_assist", "write", "document", "feedback"]

	def __init__(self) -> None:
		super().__init__()
		self.setWindowTitle("VERITAS")
		self._apply_window_icon()
		self._apply_default_geometry()

		self.setStyleSheet(self._build_stylesheet())

		container = QWidget()
		container.setObjectName("AppRoot")
		self.setCentralWidget(container)

		shell = QHBoxLayout(container)
		shell.setObjectName("ShellLayout")
		shell.setContentsMargins(20, 20, 20, 20)
		shell.setSpacing(16)

		self.sidebar_visible = True
		self._sidebar_expanded_width = 228
		self._sidebar_collapsed_width = 72
		self._sidebar_anim_group: QParallelAnimationGroup | None = None

		self.sidebar = Sidebar()
		self.sidebar.setMinimumWidth(self._sidebar_expanded_width)
		self.sidebar.setMaximumWidth(self._sidebar_expanded_width)
		self.sidebar.navRequested.connect(self._navigate)
		self.sidebar.toggleRequested.connect(self._toggle_sidebar)

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
		hero_text_col.addWidget(self.section_title)
		hero_text_col.addWidget(self.section_desc)

		top_hero_layout.addLayout(hero_text_col, 1)

		self.assist_toggle_button = QPushButton("AI 보조창")
		self.assist_toggle_button.setObjectName("AssistToggleButton")
		self.assist_toggle_button.setCursor(Qt.PointingHandCursor)
		self.assist_toggle_button.clicked.connect(self.toggle_document_assist_window)
		top_hero_layout.addWidget(self.assist_toggle_button, 0, Qt.AlignTop)

		self.stepper = WorkflowStepper(["조사", "검증", "초안 생성", "문서 보조", "채팅", "문서", "피드백"])

		self.pages = AnimatedStackedWidget()

		self.route_to_index: dict[str, int] = {}
		self._add_page("dashboard", DashboardPage())
		self._add_page("research", ResearchPage())
		self._add_page("verify", VerifyPage())
		self.draft_page = DraftPage()
		self._add_page("draft", self.draft_page)
		self._add_page("document_assist", DocumentAssistPage())
		self._add_page("write", WritePage())
		self._add_page("document", DocumentPage())
		self._add_page("feedback", FeedbackPage())
		self.settings_page = SettingsPage()
		self.settings_page.defaultWorkspaceChanged.connect(self.sidebar.set_current_workspace)
		self.sidebar.workspaceChanged.connect(self._on_workspace_changed)
		self._add_page("settings", self.settings_page)

		center_layout.addWidget(top_hero)
		center_layout.addWidget(self.stepper)
		center_layout.addWidget(self.pages, 1)

		shell.addWidget(self.sidebar)
		shell.addWidget(center_panel, 1)

		self.document_assist_window = DocumentAssistWindow(self)
		self._agent_controller = AgentController()
		self.document_assist_window.messageSubmitted.connect(self._send_assist_window_message)
		self.document_assist_window.hide()

		self._assist_toggle_shortcut = QShortcut(QKeySequence("Ctrl+Shift+A"), self)
		self._assist_toggle_shortcut.activated.connect(self.toggle_document_assist_window)

		self._enable_text_selection(container)
		self._navigate("dashboard")

	def show_document_assist_window(self) -> None:
		self.document_assist_window.show()
		self.document_assist_window.raise_()
		self.document_assist_window.activateWindow()

	def toggle_document_assist_window(self) -> None:
		if self.document_assist_window.isVisible():
			self.document_assist_window.hide()
			return
		self.show_document_assist_window()

	def _send_assist_window_message(self, message: str) -> None:
		mode = self.document_assist_window.input_bar.mode()
		try:
			reply = self._agent_controller.send_document_assist_message(
				current_workspace_id(),
				message,
				mode,
			)
		except ApiError as e:
			reply = f"API 요청 실패: {e}"
		self.document_assist_window.add_chat_message("VERITAS", reply)

	def _on_workspace_changed(self, workspace_name: str) -> None:
		self.settings_page.set_default_workspace_by_name(workspace_name)
		self.draft_page.set_workspace_by_name(workspace_name)

	def _toggle_sidebar(self) -> None:
		start = self.sidebar.width()
		end = self._sidebar_collapsed_width if self.sidebar_visible else self._sidebar_expanded_width

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
		page_widget = widget if isinstance(widget, WritePage) else self._wrap_page_with_scroll(widget)
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
			"dashboard": ("대시보드", "오늘 워크플로우 진행 상태를 한눈에 확인하세요."),
			"research": ("조사", "조사 주제와 레퍼런스 사이트를 입력해 검증 준비를 진행합니다."),
			"verify": ("정합성 검증", "출처 신뢰도와 사실 일치성을 검토합니다."),
			"draft": ("초안 생성", "선택한 워크스페이스를 바탕으로 복사 가능한 초안을 생성합니다."),
			"document_assist": ("문서 보조", "실시간 문서 작성 보조 내용을 확인합니다."),
			"write": ("AI 채팅", "워크스페이스 기반 AI와 채팅이 가능합니다."),
			"document": ("문서", "스크랩 합본과 요약본을 검토합니다."),
			"feedback": ("문서 피드백", "약한 주장과 저신뢰 문장을 우선 교정합니다."),
			"settings": ("설정", "모델명과 로컬 접근 폴더를 구성합니다."),
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

	def _build_stylesheet(self) -> str:
		return """
		QWidget {
			background-color: #F6F8FC;
			color: #111827;
			font-family: 'Segoe UI Variable', 'Segoe UI', 'Malgun Gothic', 'Noto Sans KR', sans-serif;
			font-size: 13px;
			font-weight: 500;
		}

		QLabel {
			background-color: transparent;
			selection-background-color: #BFDBFE;
			selection-color: #0F172A;
		}

		QPushButton {
			background-color: #1F2937;
			color: #F8FAFC;
			border: 1px solid #1F2937;
			border-radius: 8px;
			padding: 8px 12px;
			font-family: 'Segoe UI Variable', 'Segoe UI', 'Malgun Gothic', 'Noto Sans KR', sans-serif;
			font-size: 13px;
			font-weight: 700;
		}

		QPushButton:hover {
			background-color: #111827;
		}

		QWidget#AppRoot {
			background-color: qlineargradient(
				x1: 0,
				y1: 0,
				x2: 0,
				y2: 1,
				stop: 0 #F8FAFC,
				stop: 1 #EEF2FF
			);
		}

		QFrame#Sidebar {
			background-color: #0F172A;
			border-radius: 18px;
			border: 1px solid #1E293B;
		}

		QLabel#BrandLabel {
			color: #FFFFFF;
			font-family: 'Segoe UI Variable', 'Segoe UI', 'Malgun Gothic', 'Noto Sans KR', sans-serif;
			font-size: 19px;
			font-weight: 800;
			letter-spacing: -0.2px;
		}

		QLabel#BrandSubLabel {
			color: #94A3B8;
			font-size: 11px;
			font-weight: 600;
			letter-spacing: 0.3px;
		}

		QFrame#SidebarFooterCard {
			background-color: rgba(255, 255, 255, 0.06);
			border: 1px solid rgba(148, 163, 184, 0.24);
			border-radius: 11px;
		}

		QLabel#SidebarFooterTitle {
			color: #E2E8F0;
			font-size: 11px;
			font-weight: 800;
			letter-spacing: 0.3px;
		}

		QLabel#SidebarFooterDesc {
			color: #94A3B8;
			font-size: 10px;
			font-weight: 600;
		}

		QPushButton#SidebarWorkspaceButton {
			background-color: rgba(255, 255, 255, 0.12);
			color: #E2E8F0;
			border: 1px solid rgba(148, 163, 184, 0.45);
			border-radius: 8px;
			padding: 6px 10px;
			font-size: 11px;
			font-weight: 700;
		}

		QPushButton#SidebarWorkspaceButton:hover {
			background-color: rgba(255, 255, 255, 0.2);
			border-color: rgba(148, 163, 184, 0.75);
		}

		QFrame#CenterPanel {
			background-color: #FFFFFF;
			border: 1px solid #E2E8F0;
			border-radius: 18px;
		}

		QFrame#TopHero {
			background: qlineargradient(
				x1: 0,
				y1: 0,
				x2: 1,
				y2: 0,
				stop: 0 #1E3A8A,
				stop: 1 #3730A3
			);
			border: 1px solid rgba(129, 140, 248, 0.52);
			border-radius: 14px;
		}

		QLabel#SectionTitle {
			color: #FFFFFF;
			font-family: 'Segoe UI Variable', 'Segoe UI', 'Malgun Gothic', 'Noto Sans KR', sans-serif;
			font-size: 21px;
			font-weight: 800;
			letter-spacing: -0.1px;
		}

		QLabel#SectionDesc {
			color: #DDE7FF;
			font-size: 12px;
			font-weight: 600;
		}

		QLabel#StageChip {
			background-color: rgba(255, 255, 255, 0.16);
			border: 1px solid rgba(255, 255, 255, 0.24);
			border-radius: 12px;
			color: #F8FAFC;
			font-size: 11px;
			font-weight: 700;
			padding: 5px 10px;
		}

		QPushButton#TopActionButton {
			background-color: rgba(255, 255, 255, 0.14);
			color: #FFFFFF;
			border: 1px solid rgba(255, 255, 255, 0.3);
			border-radius: 9px;
			padding: 8px 13px;
			font-weight: 700;
		}

		QPushButton#TopActionButton:hover {
			background-color: rgba(255, 255, 255, 0.22);
		}

		QPushButton#SidebarCollapseButton {
			background-color: rgba(255, 255, 255, 0.14);
			color: #FFFFFF;
			border: 1px solid rgba(255, 255, 255, 0.32);
			border-radius: 10px;
			font-size: 15px;
			font-weight: 700;
			padding: 0px;
		}

		QPushButton#SidebarCollapseButton:hover {
			background-color: rgba(255, 255, 255, 0.24);
		}

		QFrame#WorkflowStepper {
			background-color: #FFFFFF;
			border: 1px solid #E2E8F0;
			border-radius: 14px;
		}

		QFrame#StepperConnector {
			background-color: #D1D5DB;
			border-radius: 1px;
		}

		QFrame#RightPanel {
			background-color: #FFFFFF;
			border-radius: 16px;
			border: 1px solid #E2E8F0;
		}

		QFrame#ChatHero {
			background: qlineargradient(
				x1: 0,
				y1: 0,
				x2: 1,
				y2: 0,
				stop: 0 #1E293B,
				stop: 1 #334155
			);
			border: 1px solid #475569;
			border-radius: 16px;
		}

		QFrame#ChatPanel {
			background-color: #FFFFFF;
			border: 1px solid #E2E8F0;
			border-radius: 16px;
		}

		QFrame#AssistPagePanel {
			background-color: #F8FAFC;
			border: 1px solid #E5E7EB;
			border-radius: 16px;
		}

		QFrame#AssistSectionCard {
			background-color: #FFFFFF;
			border: 1px solid #E5E7EB;
			border-radius: 13px;
		}

		QLabel#AssistSubText {
			color: #6B7280;
			font-size: 12px;
			font-weight: 600;
		}

		QLabel#AssistSectionTitle {
			color: #111827;
			font-size: 13px;
			font-weight: 850;
		}

		QScrollArea#AssistScrollArea {
			background-color: transparent;
			border: none;
		}

		QFrame#SuggestionCard {
			background-color: #FFFFFF;
			border: 1px solid #E5E7EB;
			border-radius: 12px;
		}

		QLabel#SuggestionText {
			color: #1F2937;
			font-size: 13px;
			font-weight: 650;
			line-height: 1.5;
		}

		QLabel#AssistEmptyState {
			background-color: #F8FAFC;
			border: 1px dashed #CBD5E1;
			border-radius: 12px;
			color: #6B7280;
			padding: 18px 14px;
			font-weight: 650;
		}

		QPushButton#AssistCopyButton {
			background-color: #FFFFFF;
			color: #4B5563;
			border: 1px solid #D1D5DB;
			border-radius: 8px;
			padding: 5px 8px;
			font-size: 11px;
			font-weight: 800;
		}

		QPushButton#AssistCopyButton:hover {
			background-color: #F3F4F6;
			color: #111827;
		}

		QFrame#AssistUserBubble {
			background-color: #DBEAFE;
			border: 1px solid #BFDBFE;
			border-radius: 13px;
			border-top-right-radius: 4px;
		}

		QFrame#AssistAiBubble {
			background-color: #FFFFFF;
			border: 1px solid #E5E7EB;
			border-radius: 13px;
			border-top-left-radius: 4px;
		}

		QLabel#AssistBubbleMeta {
			color: #6B7280;
			font-size: 10px;
			font-weight: 800;
		}

		QLabel#AssistBubbleText {
			color: #1F2937;
			font-size: 12px;
			font-weight: 600;
		}

		QFrame#AssistInputBar {
			background-color: #FFFFFF;
			border: 1px solid #E5E7EB;
			border-radius: 14px;
		}

		QTextEdit#AssistChatInput {
			background-color: #F8FAFC;
			border: 1px solid #E5E7EB;
			border-radius: 11px;
			padding: 8px 10px;
			color: #111827;
			selection-background-color: #BFDBFE;
			selection-color: #111827;
		}

		QTextEdit#AssistChatInput:focus {
			background-color: #FFFFFF;
			border: 1px solid #3B82F6;
		}

		QPushButton#AssistSendButton {
			background-color: #3B82F6;
			border: 1px solid #2563EB;
			border-radius: 11px;
			color: #FFFFFF;
			font-weight: 850;
		}

		QPushButton#AssistSendButton:hover {
			background-color: #2563EB;
		}

		QToolButton#AssistModeButton {
			background-color: #F8FAFC;
			color: #111827;
			border: 1px solid #D1D5DB;
			border-radius: 11px;
			padding: 0px 8px;
			font-size: 12px;
			font-weight: 850;
		}

		QToolButton#AssistModeButton:hover {
			background-color: #EEF2FF;
			border-color: #818CF8;
			color: #3730A3;
		}

		QToolButton#AssistModeButton::menu-indicator {
			image: none;
			width: 0px;
			height: 0px;
		}

		QFrame#ComposerCard {
			background-color: #F8FAFC;
			border: 1px solid #E2E8F0;
			border-radius: 18px;
			padding: 8px;
			box-shadow: 0px 6px 18px rgba(2,6,23,0.06);
		}

		QFrame#ChatHeroIconBox {
			background-color: rgba(255, 255, 255, 0.12);
			border: 1px solid rgba(255, 255, 255, 0.2);
			border-radius: 14px;
		}

		QLabel#ChatHeroIcon {
			background-color: transparent;
		}

		QScrollArea#ChatScroll {
			background: transparent;
			border: none;
		}

		QScrollArea#PageScroll {
			background: transparent;
			border: none;
		}

		QScrollBar:vertical {
			background: transparent;
			width: 10px;
			margin: 2px 0 2px 0;
		}

		QScrollBar::handle:vertical {
			background: #CBD5E1;
			border-radius: 5px;
			min-height: 28px;
		}

		QScrollBar::handle:vertical:hover {
			background: #94A3B8;
		}

		QScrollBar::add-line:vertical,
		QScrollBar::sub-line:vertical {
			height: 0px;
		}

		QScrollBar::add-page:vertical,
		QScrollBar::sub-page:vertical {
			background: transparent;
		}

		QScrollBar:horizontal {
			background: transparent;
			height: 10px;
			margin: 0 2px 0 2px;
		}

		QScrollBar::handle:horizontal {
			background: #CBD5E1;
			border-radius: 5px;
			min-width: 28px;
		}

		QScrollBar::handle:horizontal:hover {
			background: #94A3B8;
		}

		QScrollBar::add-line:horizontal,
		QScrollBar::sub-line:horizontal {
			width: 0px;
		}

		QScrollBar::add-page:horizontal,
		QScrollBar::sub-page:horizontal {
			background: transparent;
		}

		QLineEdit#ChatInput {
			background-color: #F8FAFC;
			border: 1px solid #CBD5E1;
			border-radius: 10px;
			padding: 10px 11px;
			color: #1F2937;
			selection-background-color: #C7D2FE;
			selection-color: #0F172A;
		}

		QPlainTextEdit#ChatInput {
			background-color: #FFFFFF;
			border: 1px solid #E2E8F0;
			border-radius: 16px;
			padding: 9px 13px;
			color: #0F172A;
			selection-background-color: #E9D5FF;
			selection-color: #0F172A;
			font-size: 13px;
		}

		QPlainTextEdit#ChatInput:focus {
			border: 1px solid #7C3AED;
			background-color: #FFFFFF;
		}

		QLineEdit#ChatInput:focus {
			border: 1px solid #7C3AED;
			background-color: #FFFFFF;
		}

		QPushButton#SendButton {
			background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #7C3AED, stop:1 #5B21B6);
			color: #FFFFFF;
			border: none;
			border-radius: 18px;
			min-width: 44px;
			min-height: 44px;
			padding: 8px 12px;
			font-weight: 700;
			font-size: 13px;
		}

		QPushButton#SendButton:hover {
			transform: translateY(-1px);
			background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6D28D9, stop:1 #4C1D95);
		}

		QToolButton#ModeMenuButton {
			background-color: #111827;
			color: #FFFFFF;
			border: 1px solid #111827;
			border-radius: 19px;
			padding: 0px;
			font-size: 12px;
			font-weight: 700;
			text-align: center;
			min-width: 82px;
			min-height: 38px;
			max-width: 82px;
			max-height: 38px;
		}

		QToolButton#ModeMenuButton:hover {
			background-color: #4F46E5;
			border-color: #4338CA;
		}

		QToolButton#ModeMenuButton::menu-indicator {
			image: none;
			width: 0px;
			height: 0px;
		}

		QTextEdit#ResearchInput {
			background-color: #FFFFFF;
			border: 1px solid #CBD5E1;
			border-radius: 12px;
			padding: 11px 12px;
			color: #0F172A;
			selection-background-color: #C7D2FE;
			selection-color: #0F172A;
		}

		QTextEdit#ResearchInput:focus {
			border: 1px solid #4F46E5;
		}

		QFrame#ReferenceUrlRow {
			background-color: #F8FAFC;
			border: 1px solid #E2E8F0;
			border-radius: 12px;
		}

		QLineEdit#ReferenceUrlInput {
			background-color: transparent;
			border: none;
			color: #0F172A;
			padding: 7px 4px;
			font-size: 13px;
		}

		QToolButton#RoundAddButton {
			background-color: #111827;
			color: #FFFFFF;
			border: 1px solid #111827;
			border-radius: 15px;
			font-size: 17px;
			font-weight: 800;
			padding: 0px;
		}

		QToolButton#RoundAddButton:hover {
			background-color: #4F46E5;
			border-color: #4338CA;
		}

		QToolButton#UrlRemoveButton {
			background-color: #FFFFFF;
			color: #64748B;
			border: 1px solid #CBD5E1;
			border-radius: 13px;
			font-size: 14px;
			font-weight: 800;
			padding: 0px;
		}

		QToolButton#UrlRemoveButton:hover {
			background-color: #FEF2F2;
			color: #B91C1C;
			border-color: #FECACA;
		}

		QMenu {
			background-color: #FFFFFF;
			border: 1px solid #CBD5E1;
			border-radius: 8px;
			padding: 6px;
		}

		QMenu::item {
			color: #111827;
			padding: 8px 28px 8px 12px;
			border-radius: 6px;
		}

		QMenu::item:selected {
			background-color: #EEF2FF;
			color: #3730A3;
		}

		QFrame#UserBubble {
			background-color: #EEF2FF;
			border: 1px solid #C7D2FE;
			border-radius: 11px;
			border-top-right-radius: 3px;
		}

		QFrame#AIBubble {
			background-color: #F8FAFC;
			border: 1px solid #E2E8F0;
			border-radius: 11px;
			border-top-left-radius: 3px;
		}

		QLabel#BubbleText {
			font-size: 14px;
			color: #1F2937;
			font-weight: 550;
		}

		QLabel#BubbleMeta {
			font-size: 10px;
			color: #9CA3AF;
		}

		QFrame#UserBubble QLabel#BubbleText {
			color: #312E81;
		}

		QFrame#UserBubble QLabel#BubbleMeta {
			color: #6366F1;
		}

		QLabel#ChatContextChip {
			background-color: #EEF2FF;
			color: #3730A3;
			border: 1px solid #C7D2FE;
			border-radius: 10px;
			padding: 5px 9px;
		}

		QFrame#WorkflowBadge {
			background-color: #FFF7ED;
			border: 1px solid #FED7AA;
			border-radius: 10px;
		}

		QLabel#PanelTitle {
			font-size: 16px;
			font-weight: 800;
			color: #1F2937;
		}

		QLabel#PanelSubtitle {
			font-size: 12px;
			color: #6B7280;
		}

		QFrame#CardWidget, QFrame#StatTile {
			background-color: #FFFFFF;
			border: 1px solid #E2E8F0;
			border-radius: 13px;
		}

		QLabel#CardTitle {
			font-size: 14px;
			font-weight: 800;
			color: #0F172A;
		}

		QLabel#CardPrimary {
			font-size: 13px;
			font-weight: 700;
			color: #0F172A;
		}

		QLabel#CardSecondary {
			font-size: 12px;
			color: #64748B;
		}

		QLabel#CardFooter {
			font-size: 11px;
			color: #94A3B8;
		}

		QLabel#PageTitle {
			font-size: 24px;
			font-weight: 800;
			color: #0F172A;
			letter-spacing: -0.1px;
		}

		QLabel#PageSubtitle {
			font-size: 13px;
			color: #64748B;
			font-weight: 600;
		}

		QLabel#IssueText {
			font-size: 13px;
			font-weight: 700;
			color: #991B1B;
		}

		QLabel#WarningSummary {
			background-color: #FFF7ED;
			border: 1px solid #FED7AA;
			border-radius: 10px;
			color: #92400E;
			padding: 10px 11px;
			font-weight: 700;
			font-size: 12px;
		}

		QLabel#StatLabel {
			font-size: 11px;
			color: #94A3B8;
			font-weight: 700;
			letter-spacing: 0.3px;
		}

		QLabel#StatValue {
			font-size: 28px;
			color: #0F172A;
			font-weight: 800;
			letter-spacing: -0.5px;
		}

		QLabel#StatDelta {
			font-size: 12px;
			color: #10B981;
			font-weight: 700;
		}

		QPushButton#PrimaryButton {
			background-color: #4F46E5;
			color: #FFFFFF;
			border: 1px solid #4338CA;
			border-radius: 10px;
			padding: 10px 14px;
			font-weight: 700;
		}

		QPushButton#PrimaryButton:hover {
			background-color: #4338CA;
		}

		QPushButton#GhostButton {
			background-color: #FFFFFF;
			color: #334155;
			border: 1px solid #CBD5E1;
			border-radius: 9px;
			padding: 8px 12px;
			font-weight: 700;
		}

		QPushButton#GhostButton:hover {
			background-color: #F8FAFC;
			border-color: #94A3B8;
		}

		QPushButton#VerifyDetailButton {
			background-color: #FFFFFF;
			color: #334155;
			border: 1px solid #CBD5E1;
			border-radius: 8px;
			padding: 4px 8px;
			font-size: 11px;
			font-weight: 700;
		}

		QPushButton#VerifyDetailButton:hover {
			background-color: #F8FAFC;
			border-color: #94A3B8;
		}

		QPushButton#FilterChip {
			background-color: #FFFFFF;
			color: #334155;
			border: 1px solid #CBD5E1;
			border-radius: 14px;
			padding: 7px 13px;
			font-size: 11px;
			font-weight: 700;
		}

		QPushButton#FilterChip:hover {
			background-color: #F8FAFC;
			border-color: #94A3B8;
		}

		QPushButton#FilterChip:checked {
			background-color: #EEF2FF;
			color: #3730A3;
			border: 1px solid #818CF8;
		}

		QTextEdit#DocEditor {
			background-color: #FFFFFF;
			border: 1px solid #CBD5E1;
			border-radius: 12px;
			padding: 13px;
			font-size: 13px;
			line-height: 1.6;
			color: #1F2937;
			selection-background-color: #BFDBFE;
			selection-color: #0F172A;
		}

		QComboBox#SettingsInput,
		QLineEdit#SettingsInput,
		QSpinBox#SettingsInput,
		QDoubleSpinBox#SettingsInput {
			background-color: #F8FAFC;
			border: 1px solid #CBD5E1;
			border-radius: 9px;
			padding: 7px 10px;
			color: #111827;
			min-height: 24px;
		}

		QComboBox#SettingsInput:focus,
		QLineEdit#SettingsInput:focus,
		QSpinBox#SettingsInput:focus,
		QDoubleSpinBox#SettingsInput:focus {
			border: 1px solid #4F46E5;
			background-color: #FFFFFF;
		}

		QCheckBox#SettingsCheckbox {
			color: #334155;
			font-weight: 700;
			spacing: 8px;
		}

		QPushButton#SettingsModelToggle {
			background-color: #FFFFFF;
			color: #334155;
			border: 1px solid #CBD5E1;
			border-radius: 10px;
			padding: 9px 14px;
			font-weight: 800;
		}

		QPushButton#SettingsModelToggle:hover {
			background-color: #F8FAFC;
			border-color: #94A3B8;
		}

		QPushButton#SettingsModelToggle:checked {
			background-color: #EEF2FF;
			color: #3730A3;
			border: 1px solid #818CF8;
		}

		QListWidget#SettingsFolderList {
			background-color: #F8FAFC;
			border: 1px solid #CBD5E1;
			border-radius: 10px;
			padding: 6px;
			color: #0F172A;
			selection-background-color: #DBEAFE;
			selection-color: #0F172A;
		}

		QListWidget#SettingsFolderList::item {
			border-radius: 7px;
			padding: 7px 8px;
			margin: 2px;
		}

		QListWidget#SettingsFolderList::item:selected {
			background-color: #DBEAFE;
			color: #0F172A;
		}

		QLabel#SettingsStatus {
			background-color: #F8FAFC;
			border: 1px solid #E2E8F0;
			border-radius: 10px;
			color: #475569;
			padding: 10px 11px;
			font-size: 12px;
			font-weight: 700;
		}
		"""
