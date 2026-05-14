from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEasingCurve, Property, QPropertyAnimation, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
	QComboBox,
	QDialog,
	QDialogButtonBox,
	QFrame,
	QHBoxLayout,
	QLabel,
	QPushButton,
	QSizePolicy,
	QVBoxLayout,
	QWidget,
)

from ..api_common import STATE, load_bootstrap_state, switch_workspace
from ..controllers import JobCategory, get_job_manager


class NavButton(QPushButton):
	def __init__(
		self,
		text: str,
		icon: QIcon,
		active_icon: QIcon,
		route: str,
		parent: QWidget | None = None,
	) -> None:
		super().__init__(text, parent)
		self.route = route
		self._label_text = text
		self._hover = 0.0
		self._compact = False
		self._icon_default = icon
		self._icon_active = active_icon

		self.setObjectName("NavButton")
		self.setCursor(Qt.PointingHandCursor)
		self.setIcon(self._icon_default)
		self.setIconSize(QSize(18, 18))
		self.setCheckable(True)
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
		self.setFixedHeight(40)
		self.toggled.connect(self._on_toggled)

		self._anim = QPropertyAnimation(self, b"hoverProgress", self)
		self._anim.setDuration(170)
		self._anim.setEasingCurve(QEasingCurve.OutCubic)

		self._apply_style()

	def _on_toggled(self, checked: bool) -> None:
		self.setIcon(self._icon_active if checked else self._icon_default)

	def set_compact(self, compact: bool) -> None:
		self._compact = compact
		self.setText("" if compact else self._label_text)
		self.setToolTip(self._label_text if compact else "")
		self._apply_style()

	@Property(float)
	def hoverProgress(self) -> float:
		return self._hover

	@hoverProgress.setter
	def hoverProgress(self, value: float) -> None:
		self._hover = value
		self._apply_style()

	def enterEvent(self, event) -> None:  # type: ignore[override]
		self._anim.stop()
		self._anim.setStartValue(self._hover)
		self._anim.setEndValue(1.0)
		self._anim.start()
		super().enterEvent(event)

	def leaveEvent(self, event) -> None:  # type: ignore[override]
		self._anim.stop()
		self._anim.setStartValue(self._hover)
		self._anim.setEndValue(0.0)
		self._anim.start()
		super().leaveEvent(event)

	def _apply_style(self) -> None:
		p = self._hover
		bg_alpha = int(0 + 18 * p)
		left_pad = int(12 + 4 * p) if not self._compact else 0
		right_pad = 12 if not self._compact else 0
		align = "left" if not self._compact else "center"
		self.setStyleSheet(
			f"""
			QPushButton#NavButton {{
				text-align: {align};
				border: 1px solid rgba(255, 255, 255, 0);
				border-radius: 11px;
				padding: 10px {right_pad}px 10px {left_pad}px;
				color: #D6DBE5;
				background-color: rgba(255, 255, 255, {bg_alpha});
				font-size: 13px;
				font-weight: 600;
			}}
			QPushButton#NavButton:checked {{
				background-color: rgba(99, 102, 241, 48);
				border: 1px solid rgba(165, 180, 252, 148);
				color: #F8FAFC;
				font-weight: 700;
			}}
			"""
		)


class CollapseButton(QPushButton):
	"""Sidebar collapse/expand button with a hand-painted chevron.

	The chevron is drawn rather than set as a glyph or platform standard icon:
	the standard icon rendered as an out-of-place coloured arrow, and a text
	glyph sat thin and off-centre inside the 30px button. Painting keeps it
	thick, crisp, and optically centred.
	"""

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("SidebarCollapseButton")
		self.setFixedSize(30, 30)
		self.setCursor(Qt.PointingHandCursor)
		self._points_right = False

	def set_points_right(self, points_right: bool) -> None:
		self._points_right = points_right
		self.update()

	def paintEvent(self, event) -> None:  # type: ignore[override]
		super().paintEvent(event)  # background/border from the stylesheet
		painter = QPainter(self)
		painter.setRenderHint(QPainter.Antialiasing, True)
		center = self.rect().center()
		cx, cy = center.x() + 0.5, center.y() + 0.5
		pen = QPen(QColor("#FFFFFF"))
		pen.setWidthF(2.6)
		pen.setCapStyle(Qt.RoundCap)
		pen.setJoinStyle(Qt.RoundJoin)
		painter.setPen(pen)
		# Two strokes meeting at a point. Pointing right means "expand" (the
		# sidebar is collapsed); pointing left means "collapse".
		dx, dy = 4.0, 6.0
		tip = cx + dx if self._points_right else cx - dx
		base = cx - dx if self._points_right else cx + dx
		path = QPainterPath()
		path.moveTo(base, cy - dy)
		path.lineTo(tip, cy)
		path.lineTo(base, cy + dy)
		painter.drawPath(path)


class Sidebar(QFrame):
	navRequested = Signal(str)
	toggleRequested = Signal()
	workspaceChanged = Signal(str)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("Sidebar")
		self._buttons: dict[str, NavButton] = {}
		self._compact = False
		self._ensure_workspace_state()
		self._workspace_names = [item["name"] for item in STATE["workspaces"]]
		self._workspace_ids = [item["workspaceId"] for item in STATE["workspaces"]]
		current_workspace_id = STATE["current_workspace_id"]
		self._current_workspace_index = next(
			(
				index
				for index, item in enumerate(STATE["workspaces"])
				if item["workspaceId"] == current_workspace_id
			),
			0,
		)

		layout = QVBoxLayout(self)
		layout.setContentsMargins(14, 18, 14, 18)
		layout.setSpacing(10)

		brand = QLabel("VERITAS")
		brand.setObjectName("BrandLabel")
		self._brand = brand

		logo = QLabel()
		logo.setObjectName("BrandLogo")
		logo.setFixedSize(26, 26)
		self._logo = logo
		logo_path = Path(__file__).resolve().parent / "public" / "images" / "veritas_logo.png"
		if logo_path.exists():
			pixmap = QPixmap(str(logo_path)).scaled(26, 26, Qt.KeepAspectRatio, Qt.SmoothTransformation)
			logo.setPixmap(pixmap)

		subtitle = QLabel("AI 워크플로우 스튜디오")
		subtitle.setObjectName("BrandSubLabel")
		self._subtitle = subtitle

		self._toggle_btn = CollapseButton()
		self._toggle_btn.clicked.connect(self.toggleRequested.emit)
		self._update_toggle_icon()

		header = QVBoxLayout()
		header.setContentsMargins(6, 4, 6, 12)
		header.setSpacing(2)

		brand_row = QHBoxLayout()
		brand_row.setContentsMargins(0, 0, 0, 0)
		brand_row.setSpacing(8)
		brand_row.addWidget(logo, 0, Qt.AlignVCenter)
		brand_row.addWidget(brand, 0, Qt.AlignVCenter)
		brand_row.addStretch(1)
		brand_row.addWidget(self._toggle_btn, 0, Qt.AlignRight | Qt.AlignTop)

		header.addLayout(brand_row)
		header.addWidget(subtitle)

		layout.addLayout(header)

		nav_container = QVBoxLayout()
		nav_container.setSpacing(8)

		icon_dir = Path(__file__).resolve().parent / "public" / "images" / "icons"

		nav_items = [
			("\ub300\uc2dc\ubcf4\ub4dc", "dashboard", "dashboard.svg", "dashboard_active.svg"),
			("\uc870\uc0ac", "research", "collect.svg", "collect_active.svg"),
			("\uc694\uc57d", "document", "document.svg", "document_active.svg"),
			("\uac80\uc99d", "verify", "verify.svg", "verify_active.svg"),
			("\ucd08\uc548", "draft", "draft.svg", "draft_active.svg"),
			("\ubb38\uc11c \ubcf4\uc870", "document_assist", "document_assist.svg", "document_assist_active.svg"),
			("\ucc44\ud305", "write", "write.svg", "write_active.svg"),
			("\ud53c\ub4dc\ubc31", "feedback", "feedback.svg", "feedback_active.svg"),
			("\uc124\uc815", "settings", "settings.svg", "settings_active.svg"),
		]

		for text, route, icon_name, active_icon_name in nav_items:
			icon_path = icon_dir / icon_name
			active_icon_path = icon_dir / active_icon_name
			icon = QIcon(str(icon_path)) if icon_path.exists() else QIcon()
			active_icon = QIcon(str(active_icon_path)) if active_icon_path.exists() else icon
			button = NavButton(text, icon, active_icon, route)
			button.clicked.connect(lambda checked=False, r=route: self.navRequested.emit(r))
			self._buttons[route] = button
			nav_container.addWidget(button)

		layout.addLayout(nav_container)
		layout.addStretch(1)

		footer_card = QFrame()
		footer_card.setObjectName("SidebarFooterCard")
		footer_layout = QVBoxLayout(footer_card)
		footer_layout.setContentsMargins(10, 10, 10, 10)
		footer_layout.setSpacing(6)

		footer_title = QLabel("현재 워크스페이스")
		footer_title.setObjectName("SidebarFooterTitle")
		self._workspace_desc = QLabel()
		self._workspace_desc.setObjectName("SidebarFooterDesc")
		self._workspace_desc.setWordWrap(True)

		self._switch_workspace_btn = QPushButton("워크스페이스 전환")
		self._switch_workspace_btn.setObjectName("SidebarWorkspaceButton")
		self._switch_workspace_btn.setCursor(Qt.PointingHandCursor)
		self._switch_workspace_btn.clicked.connect(self._open_workspace_dialog)

		footer_layout.addWidget(footer_title)
		footer_layout.addWidget(self._workspace_desc)
		footer_layout.addWidget(self._switch_workspace_btn)
		layout.addWidget(footer_card)
		self._footer_card = footer_card

		self._refresh_workspace_footer()

		# Workspace switching rebuilds the backend registry; it must not be
		# triggered while research/feedback/etc. are in flight.
		get_job_manager().busy_changed.connect(self._sync_busy_state)
		self._sync_busy_state()

	def _sync_busy_state(self) -> None:
		blocked = get_job_manager().is_blocked(JobCategory.WORKSPACE_SWITCH)
		self._switch_workspace_btn.setEnabled(not blocked)
		self._switch_workspace_btn.setToolTip(
			"다른 작업이 진행 중일 때는 워크스페이스를 전환할 수 없습니다."
			if blocked
			else ""
		)

	def set_active(self, route: str) -> None:
		for key, button in self._buttons.items():
			button.setChecked(key == route)

	def set_current_workspace(self, workspace_name: str) -> None:
		self._reload_workspaces()
		if workspace_name not in self._workspace_names:
			return
		self._current_workspace_index = self._workspace_names.index(workspace_name)
		self._refresh_workspace_footer()

	def set_compact(self, compact: bool) -> None:
		self._compact = compact
		self._brand.setVisible(not compact)
		self._logo.setVisible(not compact)
		self._subtitle.setVisible(not compact)
		self._footer_card.setVisible(not compact)
		self._update_toggle_icon()
		for button in self._buttons.values():
			button.set_compact(compact)

	def _update_toggle_icon(self) -> None:
		# Chevron points right to expand when collapsed, left to collapse.
		self._toggle_btn.set_points_right(self._compact)

	def _refresh_workspace_footer(self) -> None:
		if not self._workspace_names:
			self._workspace_names = ["default"]
			self._workspace_ids = ["default"]
			self._current_workspace_index = 0
		self._current_workspace_index = min(self._current_workspace_index, len(self._workspace_names) - 1)
		self._workspace_desc.setText(self._workspace_names[self._current_workspace_index])

	def _ensure_workspace_state(self) -> None:
		workspaces = STATE.get("workspaces")
		if isinstance(workspaces, list) and workspaces:
			return
		STATE["workspaces"] = [
			{
				"workspaceId": "default",
				"name": "default",
				"detail": "기본 워크스페이스",
				"status": "active",
			}
		]
		STATE["current_workspace_id"] = "default"

	def _reload_workspaces(self) -> None:
		try:
			load_bootstrap_state()
		except Exception:
			pass
		self._ensure_workspace_state()
		self._workspace_names = [item["name"] for item in STATE["workspaces"]]
		self._workspace_ids = [item["workspaceId"] for item in STATE["workspaces"]]
		current_workspace_id = STATE["current_workspace_id"]
		self._current_workspace_index = next(
			(
				index
				for index, workspace_id in enumerate(self._workspace_ids)
				if workspace_id == current_workspace_id
			),
			0,
		)
		self._refresh_workspace_footer()

	def _open_workspace_dialog(self) -> None:
		self._reload_workspaces()
		dialog = QDialog(self)
		dialog.setWindowTitle("워크스페이스 전환")
		dialog.setModal(True)
		dialog.resize(420, 180)

		layout = QVBoxLayout(dialog)
		layout.setContentsMargins(16, 14, 16, 14)
		layout.setSpacing(10)

		title = QLabel("사용할 워크스페이스를 선택하세요")
		title.setObjectName("CardPrimary")

		hint = QLabel("선택한 워크스페이스는 사이드바, 초안, 채팅 화면에 공통으로 반영됩니다.")
		hint.setObjectName("CardSecondary")
		hint.setWordWrap(True)

		selector = QComboBox()
		selector.addItems(self._workspace_names)
		selector.setCurrentIndex(self._current_workspace_index)

		buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
		switch_btn = buttons.addButton("전환", QDialogButtonBox.AcceptRole)
		switch_btn.setObjectName("PrimaryButton")
		switch_btn.setMinimumSize(72, 36)
		switch_btn.setContentsMargins(0, 0, 0, 0)
		buttons.button(QDialogButtonBox.Cancel).setMinimumSize(72, 36)

		buttons.rejected.connect(dialog.reject)
		buttons.accepted.connect(dialog.accept)

		layout.addWidget(title)
		layout.addWidget(hint)
		layout.addWidget(selector)
		layout.addStretch(1)
		layout.addSpacing(4)
		layout.addWidget(buttons)

		if dialog.exec() == QDialog.Accepted:
			self._current_workspace_index = selector.currentIndex()
			workspace_id = self._workspace_ids[self._current_workspace_index]
			# Run the switch on a worker thread; the backend rebuilds the
			# tool registry / ChromaDB handles which can take a moment.
			started = get_job_manager().submit(
				JobCategory.WORKSPACE_SWITCH,
				switch_workspace,
				workspace_id,
				on_success=self._on_workspace_switched,
				on_error=self._on_workspace_switch_failed,
			)
			if not started:
				# Should not happen — button is gated by busy_changed —
				# but be defensive.
				return

	def _on_workspace_switched(self, current: str) -> None:
		self._refresh_workspace_footer()
		self.workspaceChanged.emit(str(current or ""))

	def _on_workspace_switch_failed(self, message: str) -> None:
		print(f"[workspace][switch][warn] {message}")
