from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
	QFrame,
	QHBoxLayout,
	QLabel,
	QMessageBox,
	QPushButton,
	QVBoxLayout,
	QWidget,
)

from db.dashboard_service import get_dashboard_summary
from db.db import init_db

from ...api_common import ApiError, load_bootstrap_state
from ...components.cards import CardWidget
from ...controllers import AgentController


class DashboardPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		init_db()
		self._controller = AgentController()

		self._stat_values: dict[str, QLabel] = {}
		self._workspace_list = QVBoxLayout()
		self._activity_list = QVBoxLayout()

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(14)

		stats_row = QHBoxLayout()
		stats_row.setSpacing(12)
		stats_row.addWidget(self._create_stat_tile("processed_docs", "처리 문서", "0"))
		stats_row.addWidget(self._create_stat_tile("validated_workspaces", "검증 완료 워크스페이스", "0"))
		stats_row.addWidget(self._create_stat_tile("feedback_rate", "피드백 완료율", "0%"))
		root.addLayout(stats_row)

		recent = CardWidget("최근 작업")
		recent_subtitle = QLabel("최근 워크스페이스 진행 현황과 문서 작업 이력을 확인하세요.")
		recent_subtitle.setObjectName("PageSubtitle")
		recent_subtitle.setWordWrap(True)
		recent.layout.addWidget(recent_subtitle)

		workspace_card = CardWidget("최근 작업 워크스페이스")
		self._workspace_list.setSpacing(8)
		workspace_card.layout.addLayout(self._workspace_list)

		doc_card = CardWidget("최근 문서/피드백")
		self._activity_list.setSpacing(8)
		doc_card.layout.addLayout(self._activity_list)

		recent.layout.addWidget(workspace_card)
		recent.layout.addWidget(doc_card)

		root.addWidget(recent)
		root.addStretch(1)

		self.load_dashboard_data()

		self._refresh_timer = QTimer(self)
		self._refresh_timer.setInterval(4000)
		self._refresh_timer.timeout.connect(self.load_dashboard_data)
		self._refresh_timer.start()

	def refresh(self) -> None:
		"""Public hook for document, workspace, or feedback events."""
		self.load_dashboard_data()

	def load_dashboard_data(self) -> None:
		data = get_dashboard_summary()

		self._stat_values["processed_docs"].setText(str(data["processed_docs"]))
		self._stat_values["validated_workspaces"].setText(str(data["validated_workspaces"]))
		self._stat_values["feedback_rate"].setText(f"{data['feedback_rate']}%")

		self._render_recent_workspaces(data["recent_workspaces"])
		self._render_recent_activities(data["recent_activities"])

	def _create_stat_tile(self, key: str, label: str, value: str) -> QFrame:
		tile = CardWidget()
		tile.setObjectName("StatTile")

		label_widget = QLabel(label)
		label_widget.setObjectName("StatLabel")

		value_widget = QLabel(value)
		value_widget.setObjectName("StatValue")

		tile.layout.addWidget(label_widget)
		tile.layout.addWidget(value_widget)
		self._stat_values[key] = value_widget
		return tile

	def _render_recent_workspaces(self, workspaces: list[dict[str, object]]) -> None:
		self._clear_layout(self._workspace_list)
		if not workspaces:
			self._workspace_list.addWidget(self._empty_label("최근 작업 없음"))
			return

		for workspace in workspaces:
			workspace_id = str(workspace.get("id") or "")
			name = str(workspace.get("name") or "이름 없는 워크스페이스")
			last_worked_at = str(workspace.get("last_worked_at") or "-")
			self._workspace_list.addLayout(
				self._create_workspace_row(workspace_id, name, last_worked_at)
			)

	def _create_workspace_row(self, workspace_id: str, name: str, last_worked_at: str) -> QHBoxLayout:
		row = self._create_text_row(name, f"마지막 작업: {last_worked_at}")
		delete_button = QPushButton("삭제")
		delete_button.setObjectName("DashboardWorkspaceDeleteButton")
		delete_button.setCursor(Qt.PointingHandCursor)
		delete_button.setFixedHeight(28)
		delete_button.setStyleSheet(
			"QPushButton#DashboardWorkspaceDeleteButton {"
			" background-color: #FFFFFF; color: #B91C1C;"
			" border: 1px solid #FCA5A5; border-radius: 8px;"
			" padding: 4px 10px; font-size: 11px; font-weight: 800;"
			"}"
			"QPushButton#DashboardWorkspaceDeleteButton:hover {"
			" background-color: #FEE2E2; border-color: #F87171;"
			"}"
			"QPushButton#DashboardWorkspaceDeleteButton:disabled {"
			" color: #9CA3AF; border-color: #E5E7EB;"
			"}"
		)
		if not workspace_id:
			delete_button.setEnabled(False)
			delete_button.setToolTip("워크스페이스 ID가 없어 삭제할 수 없습니다.")
		else:
			delete_button.setToolTip(f"{name} 워크스페이스 삭제")
			delete_button.clicked.connect(
				lambda _checked=False, wid=workspace_id, wname=name: self._confirm_delete_workspace(wid, wname)
			)
		row.addWidget(delete_button, 0, Qt.AlignTop | Qt.AlignRight)
		return row

	def _confirm_delete_workspace(self, workspace_id: str, workspace_name: str) -> None:
		"""Confirm with a Yes/No popup, then delete the workspace.

		The actual delete request hits ``DELETE /api/v1/workspaces/{id}``
		which removes the `runs/<id>/` directory AND the corresponding rows
		in `appdata/VERITAS/veritas.db` (workspaces, documents,
		activity_logs, plus app_state.current_workspace_id if it pointed
		here). Dashboard refresh happens immediately after so the row
		disappears from the panel.
		"""
		box = QMessageBox(self)
		box.setIcon(QMessageBox.Warning)
		box.setWindowTitle("워크스페이스 삭제 확인")
		box.setText(f"{workspace_name} 워크스페이스가 삭제됩니다. 계속 하시겠습니까?")
		yes_button = box.addButton("예", QMessageBox.YesRole)
		no_button = box.addButton("아니오", QMessageBox.NoRole)
		box.setDefaultButton(no_button)
		box.exec()
		if box.clickedButton() is not yes_button:
			return

		try:
			result = self._controller.delete_workspace(workspace_id)
		except ApiError as e:
			QMessageBox.critical(self, "삭제 실패", f"워크스페이스 삭제에 실패했습니다.\n\n{e}")
			return

		# The API clears the DB rows even when the on-disk folder could not be
		# removed (e.g. a lingering file lock). Surface that case so the user
		# isn't left thinking the runs/ folder is gone when it isn't.
		if isinstance(result, dict) and result.get("diskError"):
			QMessageBox.warning(
				self,
				"폴더 삭제 실패",
				"워크스페이스 항목은 제거되었지만 디스크 폴더를 삭제하지 못했습니다.\n"
				"수동으로 삭제해야 할 수 있습니다.\n\n"
				f"{result.get('diskError')}",
			)

		# Refresh the bootstrap state so the sidebar workspace dropdown
		# also reflects the removal on the next render.
		try:
			load_bootstrap_state()
		except Exception:
			pass
		self.load_dashboard_data()

	def _render_recent_activities(self, activities: list[dict[str, object]]) -> None:
		self._clear_layout(self._activity_list)
		if not activities:
			self._activity_list.addWidget(self._empty_label("최근 작업 없음"))
			return

		for activity in activities:
			action = self._format_action(str(activity.get("action") or "activity"))
			description = str(activity.get("description") or "작업 설명 없음")
			created_at = str(activity.get("created_at") or "-")
			self._activity_list.addLayout(self._create_text_row(action, f"{description} · {created_at}"))

	def _create_text_row(self, title: str, detail: str) -> QHBoxLayout:
		row = QHBoxLayout()
		row.setSpacing(8)

		text_col = QVBoxLayout()
		text_col.setSpacing(2)

		title_label = QLabel(title)
		title_label.setObjectName("CardPrimary")
		title_label.setWordWrap(True)

		detail_label = QLabel(detail)
		detail_label.setObjectName("CardSecondary")
		detail_label.setWordWrap(True)

		text_col.addWidget(title_label)
		text_col.addWidget(detail_label)
		row.addLayout(text_col, 1)
		return row

	def _empty_label(self, text: str) -> QLabel:
		label = QLabel(text)
		label.setObjectName("CardSecondary")
		label.setWordWrap(True)
		return label

	def _clear_layout(self, layout: QVBoxLayout) -> None:
		while layout.count():
			item = layout.takeAt(0)
			child_layout = item.layout()
			if child_layout is not None:
				self._clear_nested_layout(child_layout)

			widget = item.widget()
			if widget is not None:
				widget.deleteLater()

	def _clear_nested_layout(self, layout) -> None:
		while layout.count():
			item = layout.takeAt(0)
			child_layout = item.layout()
			if child_layout is not None:
				self._clear_nested_layout(child_layout)

			widget = item.widget()
			if widget is not None:
				widget.deleteLater()

	def _format_action(self, action: str) -> str:
		return {
			"document_uploaded": "최근 업로드 문서",
			"draft_created": "최근 초안 생성",
			"validation_completed": "최근 검증 완료",
			"feedback_completed": "최근 피드백 완료",
			"workspace_opened": "최근 워크스페이스 열림",
		}.get(action, action)
