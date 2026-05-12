from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from db.dashboard_service import get_dashboard_summary
from db.db import init_db

from ...components.cards import CardWidget


class DashboardPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		init_db()

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
			name = str(workspace.get("name") or "이름 없는 워크스페이스")
			last_worked_at = str(workspace.get("last_worked_at") or "-")
			self._workspace_list.addLayout(self._create_text_row(name, f"마지막 작업: {last_worked_at}"))

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
