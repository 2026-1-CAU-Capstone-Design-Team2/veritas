from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEasingCurve, Property, QPropertyAnimation, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
	QComboBox,
	QDialog,
	QDialogButtonBox,
	QFrame,
	QHBoxLayout,
	QLabel,
	QPushButton,
	QSizePolicy,
	QStyle,
	QVBoxLayout,
	QWidget,
)

from ..api_common import STATE


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


class Sidebar(QFrame):
	navRequested = Signal(str)
	toggleRequested = Signal()
	workspaceChanged = Signal(str)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("Sidebar")
		self._buttons: dict[str, NavButton] = {}
		self._compact = False
		self._workspace_names = [item["name"] for item in STATE["workspaces"]]
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

		self._toggle_btn = QPushButton()
		self._toggle_btn.setObjectName("SidebarCollapseButton")
		self._toggle_btn.setFixedSize(30, 30)
		self._toggle_btn.setCursor(Qt.PointingHandCursor)
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
			("\uac80\uc99d", "verify", "verify.svg", "verify_active.svg"),
			("\ucd08\uc548", "draft", "draft.svg", "draft_active.svg"),
			("\ubb38\uc11c \ubcf4\uc870", "document_assist", "document_assist.svg", "document_assist_active.svg"),
			("\ucc44\ud305", "write", "write.svg", "write_active.svg"),
			("\ubb38\uc11c", "document", "document.svg", "document_active.svg"),
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

	def set_active(self, route: str) -> None:
		for key, button in self._buttons.items():
			button.setChecked(key == route)

	def set_current_workspace(self, workspace_name: str) -> None:
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
		icon_kind = QStyle.SP_ArrowRight if self._compact else QStyle.SP_ArrowLeft
		self._toggle_btn.setIcon(self.style().standardIcon(icon_kind))
		self._toggle_btn.setIconSize(QSize(14, 14))

	def _refresh_workspace_footer(self) -> None:
		self._workspace_desc.setText(self._workspace_names[self._current_workspace_index])

	def _open_workspace_dialog(self) -> None:
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
		switch_btn.setFixedHeight(32)

		buttons.rejected.connect(dialog.reject)
		buttons.accepted.connect(dialog.accept)

		layout.addWidget(title)
		layout.addWidget(hint)
		layout.addWidget(selector)
		layout.addStretch(1)
		layout.addWidget(buttons)

		if dialog.exec() == QDialog.Accepted:
			self._current_workspace_index = selector.currentIndex()
			current = self._workspace_names[self._current_workspace_index]
			self._refresh_workspace_footer()
			self.workspaceChanged.emit(current)
