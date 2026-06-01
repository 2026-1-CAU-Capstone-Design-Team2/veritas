from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
	QFrame,
	QHBoxLayout,
	QInputDialog,
	QLabel,
	QMessageBox,
	QPushButton,
	QVBoxLayout,
	QWidget,
)

from db.dashboard_service import get_dashboard_summary
from db.db import get_connection, init_db

from ...api_common import ApiError, load_bootstrap_state
from ...components.cards import CardWidget
from ...controllers import AgentController, get_job_manager
from ...theme import theme

# Stored timestamps are written by the backend with SQLite ``datetime('now')``
# or as ISO-8601 strings with a trailing ``Z`` — both UTC. The dashboard shows
# them in Korean Standard Time (UTC+9).
_KST = timezone(timedelta(hours=9))


class _DashboardActionButton(QPushButton):
	"""A small pill action button (rename / delete / open) on the dashboard.

	These buttons are created and destroyed per data-refresh, so each instance
	owns its own theme handling: ``_apply_theme`` is a bound method on the button
	and ``themeChanged`` connects to it, which Qt auto-disconnects when the button
	is destroyed. Subclasses supply the semantic colour tokens via class
	attributes; only the colours differ between them.
	"""

	# (foreground, border, hover background, hover border) theme tokens.
	_FG = ""
	_BORDER = ""
	_HOVER_BG = ""
	_HOVER_BORDER = ""

	def __init__(self, text: str, object_name: str, parent: QWidget | None = None) -> None:
		super().__init__(text, parent)
		self.setObjectName(object_name)
		self.setCursor(Qt.PointingHandCursor)
		self.setFixedHeight(28)
		self._apply_theme()
		theme.themeChanged.connect(self._apply_theme)

	def _apply_theme(self, *args) -> None:
		name = self.objectName()
		self.setStyleSheet(
			f"QPushButton#{name} {{"
			f" background-color: {theme.color('surface')}; color: {theme.color(self._FG)};"
			f" border: 1px solid {theme.color(self._BORDER)}; border-radius: 8px;"
			" padding: 4px 10px; font-size: 11px; font-weight: 800;"
			"}"
			f"QPushButton#{name}:hover {{"
			f" background-color: {theme.color(self._HOVER_BG)}; border-color: {theme.color(self._HOVER_BORDER)};"
			"}"
			f"QPushButton#{name}:disabled {{"
			f" color: {theme.color('text.muted.gray')}; border-color: {theme.color('border.gray')};"
			"}"
		)


class _WorkspaceRenameButton(_DashboardActionButton):
	_FG = "action.edit.fg"
	_BORDER = "action.edit.border"
	_HOVER_BG = "action.edit.hover.bg"
	_HOVER_BORDER = "action.edit.hover.border"


class _WorkspaceDeleteButton(_DashboardActionButton):
	_FG = "action.delete.fg"
	_BORDER = "action.delete.border"
	_HOVER_BG = "action.delete.hover.bg"
	_HOVER_BORDER = "action.delete.hover.border"


class _DraftOpenButton(_DashboardActionButton):
	_FG = "action.open.fg"
	_BORDER = "action.open.border"
	_HOVER_BG = "action.open.hover.bg"
	_HOVER_BORDER = "action.open.hover.border"


class DashboardPage(QWidget):
	openDraftRequested = Signal(str, str)  # (workspace_id, markdown)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		init_db()
		self._controller = AgentController()

		self._stat_values: dict[str, QLabel] = {}
		self._workspace_list = QVBoxLayout()
		self._draft_list = QVBoxLayout()
		# Last payload rendered — an unchanged refresh skips the widget rebuild
		# entirely so an idle dashboard costs nothing. ``_loading`` coalesces
		# overlapping ticks into one in-flight fetch.
		self._last_data: dict | None = None
		self._loading = False

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

		doc_card = CardWidget("최근 작성 초안")
		self._draft_list.setSpacing(8)
		doc_card.layout.addLayout(self._draft_list)

		recent.layout.addWidget(workspace_card)
		recent.layout.addWidget(doc_card)

		root.addWidget(recent)
		root.addStretch(1)

		self.load_dashboard_data()

		# The dashboard polls the local SQLite DB. Keep that work off the UI
		# thread (see load_dashboard_data) and only let the timer tick while the
		# page is actually on screen — see showEvent / hideEvent.
		self._refresh_timer = QTimer(self)
		self._refresh_timer.setInterval(4000)
		self._refresh_timer.timeout.connect(self.load_dashboard_data)

	def showEvent(self, event) -> None:  # type: ignore[override]
		super().showEvent(event)
		self.load_dashboard_data()
		self._refresh_timer.start()

	def hideEvent(self, event) -> None:  # type: ignore[override]
		super().hideEvent(event)
		# No point polling the DB while another page is on screen.
		self._refresh_timer.stop()

	def refresh(self) -> None:
		"""Public hook for document, workspace, or feedback events."""
		self.load_dashboard_data()

	def load_dashboard_data(self) -> None:
		"""Refresh the dashboard from the local DB on a worker thread.

		``get_dashboard_summary`` opens and queries SQLite; running it inline
		stutters the UI every few seconds. The result is applied back on the
		main thread, and an unchanged payload skips the widget rebuild entirely.
		"""
		if self._loading:
			return
		self._loading = True

		def _fetch() -> dict:
			return get_dashboard_summary()

		def _apply(data: object) -> None:
			self._loading = False
			if not isinstance(data, dict) or data == self._last_data:
				return
			self._last_data = data
			self._stat_values["processed_docs"].setText(str(data["processed_docs"]))
			self._stat_values["validated_workspaces"].setText(str(data["validated_workspaces"]))
			self._stat_values["feedback_rate"].setText(f"{data['feedback_rate']}%")
			self._render_recent_workspaces(data["recent_workspaces"])
			self._render_recent_drafts(data.get("recent_drafts", []))

		def _failed(_message: str) -> None:
			self._loading = False

		get_job_manager().run_detached(_fetch, on_success=_apply, on_error=_failed)

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
		row = self._create_text_row(name, f"생성 일자: {self._format_created_at(last_worked_at)}")

		rename_button = _WorkspaceRenameButton("이름 변경", "DashboardWorkspaceRenameButton")

		delete_button = _WorkspaceDeleteButton("삭제", "DashboardWorkspaceDeleteButton")
		if not workspace_id:
			rename_button.setEnabled(False)
			rename_button.setToolTip("워크스페이스 ID가 없어 이름을 변경할 수 없습니다.")
			delete_button.setEnabled(False)
			delete_button.setToolTip("워크스페이스 ID가 없어 삭제할 수 없습니다.")
		else:
			rename_button.setToolTip(f"{name} 워크스페이스 이름 변경")
			rename_button.clicked.connect(
				lambda _checked=False, wid=workspace_id, wname=name: self._rename_workspace(wid, wname)
			)
			delete_button.setToolTip(f"{name} 워크스페이스 삭제")
			delete_button.clicked.connect(
				lambda _checked=False, wid=workspace_id, wname=name: self._confirm_delete_workspace(wid, wname)
			)
		row.addWidget(rename_button, 0, Qt.AlignTop | Qt.AlignRight)
		row.addWidget(delete_button, 0, Qt.AlignTop | Qt.AlignRight)
		return row

	def _format_created_at(self, raw: str) -> str:
		"""Render a stored UTC timestamp as ``YYYY년 MM월 DD일 HH시 MM분`` in KST.

		Values arrive either as ISO-8601 with a trailing ``Z`` (workspace
		catalog) or as a naive ``YYYY-MM-DD HH:MM:SS`` string written via
		SQLite ``datetime('now')``; both represent UTC, so naive values are
		treated as UTC before converting to Korean Standard Time. Anything
		that fails to parse is shown unchanged.
		"""
		text = (raw or "").strip()
		if not text or text == "-":
			return "-"

		parsed: datetime | None = None
		try:
			parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
		except ValueError:
			for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
				try:
					parsed = datetime.strptime(text, fmt)
					break
				except ValueError:
					continue
		if parsed is None:
			return text

		if parsed.tzinfo is None:
			parsed = parsed.replace(tzinfo=timezone.utc)
		return parsed.astimezone(_KST).strftime("%Y년 %m월 %d일 %H시 %M분")

	def _rename_workspace(self, workspace_id: str, current_name: str) -> None:
		"""Prompt for a new name and persist it to the local workspaces table.

		The dashboard reads workspace rows straight from ``veritas.db`` (see
		``db.dashboard_service``), so the rename is applied with a direct
		``UPDATE`` here and picked up by the next refresh tick.
		"""
		new_name, ok = QInputDialog.getText(
			self,
			"워크스페이스 이름 변경",
			"새 워크스페이스 이름을 입력하세요.",
			text=current_name,
		)
		if not ok:
			return
		new_name = new_name.strip()
		if not new_name or new_name == current_name:
			return

		try:
			conn = get_connection()
			try:
				updated = conn.execute(
					"UPDATE workspaces SET name = ?, updated_at = ? WHERE id = ?",
					(
						new_name,
						datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
						workspace_id,
					),
				).rowcount
				conn.commit()
			finally:
				conn.close()
		except Exception as e:  # noqa: BLE001 - surface any DB failure to the user
			QMessageBox.critical(self, "이름 변경 실패", f"워크스페이스 이름을 변경하지 못했습니다.\n\n{e}")
			return

		if not updated:
			QMessageBox.warning(self, "이름 변경 실패", "해당 워크스페이스를 찾을 수 없습니다.")
			return

		# Keep the sidebar workspace dropdown in sync with the new name.
		try:
			load_bootstrap_state()
		except Exception:
			pass
		self.load_dashboard_data()

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

	def _render_recent_drafts(self, drafts: list[dict[str, object]]) -> None:
		self._clear_layout(self._draft_list)
		if not drafts:
			self._draft_list.addWidget(self._empty_label("작성된 초안 없음"))
			return

		for draft in drafts:
			title = str(draft.get("title") or "제목 없는 초안")
			workspace_name = str(draft.get("workspace_name") or "")
			workspace_id = str(draft.get("workspace_id") or "")
			updated_at = self._format_created_at(str(draft.get("updated_at") or "-"))
			detail = f"{workspace_name} · {updated_at}" if workspace_name else updated_at
			draft_path = str(draft.get("path") or "")
			self._draft_list.addLayout(self._create_draft_row(title, detail, workspace_id, draft_path))

	def _create_draft_row(self, title: str, detail: str, workspace_id: str, draft_path: str) -> QHBoxLayout:
		row = self._create_text_row(title, detail)
		open_button = _DraftOpenButton("열기", "DashboardDraftOpenButton")
		if draft_path and Path(draft_path).exists():
			open_button.setToolTip(f"글쓰기 창에서 열기: {title}")
			open_button.clicked.connect(
				lambda _checked=False, wid=workspace_id, p=draft_path: self._open_draft(wid, p)
			)
		else:
			open_button.setEnabled(False)
			open_button.setToolTip("파일을 찾을 수 없습니다.")
		row.addWidget(open_button, 0, Qt.AlignTop | Qt.AlignRight)
		return row

	def _open_draft(self, workspace_id: str, path: str) -> None:
		try:
			markdown = Path(path).read_text(encoding="utf-8")
		except Exception as e:
			QMessageBox.warning(self, "파일 열기 실패", f"초안 파일을 읽지 못했습니다.\n\n{e}")
			return
		self.openDraftRequested.emit(workspace_id, markdown)

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
