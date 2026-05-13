from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QMouseEvent
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

from ...api_common import current_workspace_id, load_bootstrap_state
from ...components.buttons import AppButton
from ...components.cards import CardWidget
from ...controllers import AgentController


class StatusPill(QPushButton):
	"""Top-of-card status indicator.

	- "completed" → green ● 완료 (non-clickable)
	- "failed"    → red ● 오류 (clickable; opens the error message popup)
	- "running"   → blue ● 진행 중 (non-clickable)
	- "idle"      → hidden
	"""

	BASE_STYLE = (
		"QPushButton#StatusPill {{ background-color: {bg}; color: {fg}; "
		"border: 1px solid {border}; border-radius: 12px; padding: 4px 12px; "
		"font-size: 12px; font-weight: 800; }}"
		"QPushButton#StatusPill:hover {{ background-color: {hover_bg}; }}"
	)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("StatusPill")
		self.setCursor(Qt.PointingHandCursor)
		self._error_message: str = ""
		self.clicked.connect(self._on_clicked)
		self.set_state("idle")

	def set_state(self, state: str, error_message: str = "") -> None:
		self._error_message = error_message or ""
		if state == "completed":
			self.setVisible(True)
			self.setEnabled(False)
			self.setCursor(Qt.ArrowCursor)
			self.setText("● 완료")
			self.setStyleSheet(self.BASE_STYLE.format(
				bg="#DCFCE7", fg="#15803D", border="#86EFAC", hover_bg="#DCFCE7"
			))
			self.setToolTip("조사가 완료되었습니다.")
		elif state == "failed":
			self.setVisible(True)
			self.setEnabled(True)
			self.setCursor(Qt.PointingHandCursor)
			self.setText("● 오류")
			self.setStyleSheet(self.BASE_STYLE.format(
				bg="#FEE2E2", fg="#B91C1C", border="#FCA5A5", hover_bg="#FECACA"
			))
			self.setToolTip("클릭하면 오류 메시지를 확인할 수 있습니다.")
		elif state == "running":
			self.setVisible(True)
			self.setEnabled(False)
			self.setCursor(Qt.ArrowCursor)
			self.setText("● 진행 중")
			self.setStyleSheet(self.BASE_STYLE.format(
				bg="#DBEAFE", fg="#1D4ED8", border="#BFDBFE", hover_bg="#DBEAFE"
			))
			self.setToolTip("AutoSurvey workflow를 실행하고 있습니다.")
		else:
			self.setVisible(False)
			self.setEnabled(False)
			self._error_message = ""

	def _on_clicked(self) -> None:
		if not self._error_message:
			return
		QMessageBox.critical(self, "조사 오류", self._error_message)


class InfoTile(QFrame):
	"""Small label/value tile shown in the top info row of the result card."""

	def __init__(self, label: str, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("ResearchInfoTile")
		self.setStyleSheet(
			"QFrame#ResearchInfoTile { background-color: #F8FAFC; border: 1px solid #E2E8F0; "
			"border-radius: 10px; padding: 8px 12px; }"
		)
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(2)
		self._label = QLabel(label)
		self._label.setStyleSheet("color: #6B7280; font-size: 10px; font-weight: 700; letter-spacing: 0.4px;")
		self._value = QLabel("-")
		self._value.setWordWrap(True)
		self._value.setTextInteractionFlags(Qt.TextSelectableByMouse)
		self._value.setStyleSheet("color: #0F172A; font-size: 13px; font-weight: 700;")
		layout.addWidget(self._label)
		layout.addWidget(self._value)

	def set_value(self, value: str) -> None:
		self._value.setText(value if value else "-")
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
		display = html.escape(self._url, quote=False)
		# Rich text is only used for the underline + color; the click handler
		# does not rely on Qt parsing an <a> tag.
		self.setText(
			f'<span style="color:#2563EB; text-decoration:underline;">{display}</span>'
		)
		self.setTextFormat(Qt.RichText)
		self.setTextInteractionFlags(Qt.NoTextInteraction)
		self.setWordWrap(True)
		self.setCursor(Qt.PointingHandCursor)
		self.setToolTip(self._url)
		self.setStyleSheet("font-size: 11px;")

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
		self.setStyleSheet(
			"QFrame#ResearchDocumentBar { background-color: #FFFFFF; border: 1px solid #E2E8F0; "
			"border-radius: 10px; }"
			"QFrame#ResearchDocumentBar:hover { border-color: #C7D2FE; }"
		)
		self.doc_id = str(doc_id or "")
		self._summary_path: Path | None = None

		layout = QHBoxLayout(self)
		layout.setContentsMargins(12, 10, 12, 10)
		layout.setSpacing(10)

		text_column = QVBoxLayout()
		text_column.setContentsMargins(0, 0, 0, 0)
		text_column.setSpacing(2)

		safe_title = title if title else "Untitled"
		title_text = f"{index}. {safe_title}"
		title_label = QLabel(title_text)
		title_label.setWordWrap(True)
		title_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
		title_label.setStyleSheet("color: #0F172A; font-size: 13px; font-weight: 700;")
		text_column.addWidget(title_label)

		if url:
			text_column.addWidget(LinkLabel(url))

		layout.addLayout(text_column, 1)

		self._open_button = QPushButton(self)
		self._open_button.setCursor(Qt.PointingHandCursor)
		self._open_button.setStyleSheet(
			"QPushButton { background-color: #EEF2FF; color: #3730A3; "
			"border: 1px solid #C7D2FE; border-radius: 8px; padding: 6px 10px; "
			"font-size: 11px; font-weight: 800; }"
			"QPushButton:hover { background-color: #E0E7FF; border-color: #818CF8; }"
			"QPushButton:disabled { background-color: #F3F4F6; color: #9CA3AF; "
			"border-color: #E5E7EB; }"
		)
		self._open_button.clicked.connect(self._on_open_clicked)
		layout.addWidget(self._open_button, 0, Qt.AlignTop)

		# Render initial state. If a path was passed (e.g. when reconstructing
		# from a completed job), we honor it immediately; otherwise the button
		# starts in pending/disabled state.
		if summary_path is not None and summary_path.exists():
			self.set_summary_ready(summary_path)
		else:
			self._apply_pending_state()

	def set_summary_ready(self, summary_path: Path) -> None:
		"""Mark this document's summary as available and enable the open button."""
		self._summary_path = summary_path
		self._open_button.setEnabled(True)
		self._open_button.setText(f"{summary_path.name} ↗")
		self._open_button.setToolTip(str(summary_path))

	def _apply_pending_state(self) -> None:
		self._summary_path = None
		self._open_button.setEnabled(False)
		self._open_button.setText("요약 대기 중")
		self._open_button.setToolTip("요약이 완료되면 열 수 있습니다.")

	def _on_open_clicked(self) -> None:
		if self._summary_path is None or not self._summary_path.exists():
			return
		QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._summary_path)))


class ResearchWorker(QObject):
	finished = Signal(dict)
	failed = Signal(str)

	def __init__(self, workspace_id: str, instruction: str, reference_urls: list[str]) -> None:
		super().__init__()
		self._workspace_id = workspace_id
		self._instruction = instruction
		self._reference_urls = reference_urls

	def run(self) -> None:
		try:
			response = AgentController().run_research(
				self._workspace_id,
				self._instruction,
				self._reference_urls,
			)
			self.finished.emit(response)
		except Exception as e:
			self.failed.emit(str(e))


class ResearchProgressPoller(QThread):
	"""Polls /api/v1/research/progress on a background thread.

	Emits the full list of new events each tick so the controller can drive
	per-document UI lifecycle (doc_fetched/doc_summarized) in addition to the
	single-line gray status display. Cursor advances strictly forward, so a
	long-running job that survives an API hiccup will still see every event
	exactly once.
	"""

	events = Signal(list)

	def __init__(self, parent: QObject | None = None) -> None:
		super().__init__(parent)
		self._cursor = 0
		self._stop = False
		self._sleep_ms = 800

	def request_stop(self) -> None:
		self._stop = True

	def reset(self) -> None:
		self._cursor = 0

	def run(self) -> None:  # type: ignore[override]
		while not self._stop:
			try:
				response = AgentController().get_research_progress(since=self._cursor, limit=100)
				items = response.get("items", []) if isinstance(response, dict) else []
				if isinstance(items, list) and items:
					self._cursor = int(response.get("nextCursor") or self._cursor)
					valid = [item for item in items if isinstance(item, dict)]
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

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._url_rows: list[tuple[QFrame, QLineEdit]] = []
		self._workspace_id = current_workspace_id()
		self._research_thread: QThread | None = None
		self._research_worker: ResearchWorker | None = None
		self._progress_poller: ResearchProgressPoller | None = None
		# Controller-side document model keyed by doc_id. Bars are created on
		# `doc_fetched` events and activated on `doc_summarized`. The final
		# response reconciles anything that polling may have missed.
		self._doc_bars: dict[str, DocumentBar] = {}

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

		add_url_btn = QToolButton()
		add_url_btn.setObjectName("RoundAddButton")
		add_url_btn.setText("+")
		add_url_btn.setFixedSize(30, 30)
		add_url_btn.setCursor(Qt.PointingHandCursor)
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

		action_row = QHBoxLayout()
		action_row.addStretch(1)
		self.run_button = AppButton("조사 실행")
		self.run_button.clicked.connect(self._run_research)
		action_row.addWidget(self.run_button)

		content_card.layout.addWidget(research_label)
		content_card.layout.addWidget(self.research_input)
		content_card.layout.addLayout(reference_header)
		content_card.layout.addLayout(self.url_list)
		content_card.layout.addWidget(guide)
		content_card.layout.addLayout(action_row)
		root.addWidget(content_card)

		result_card = CardWidget("조사 결과")
		self._final_path: Path | None = None

		header_row = QHBoxLayout()
		header_row.setContentsMargins(0, 0, 0, 0)
		header_row.setSpacing(8)
		self.status_pill = StatusPill()
		header_row.addWidget(self.status_pill, 0, Qt.AlignLeft)
		header_row.addStretch(1)
		result_card.layout.addLayout(header_row)

		self.progress_line = QLabel("")
		self.progress_line.setObjectName("ResearchProgressLine")
		self.progress_line.setWordWrap(False)
		self.progress_line.setTextFormat(Qt.PlainText)
		self.progress_line.setStyleSheet("color: #9CA3AF; font-size: 12px; font-weight: 500;")
		self.progress_line.setVisible(False)
		result_card.layout.addWidget(self.progress_line)

		info_row = QHBoxLayout()
		info_row.setContentsMargins(0, 0, 0, 0)
		info_row.setSpacing(8)
		self.info_job_name = InfoTile("작업 이름")
		self.info_save_path = InfoTile("저장 경로")
		self.info_doc_count = InfoTile("수집된 문서 수")
		info_row.addWidget(self.info_job_name, 1)
		info_row.addWidget(self.info_save_path, 2)
		info_row.addWidget(self.info_doc_count, 1)
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
		self.result_empty.setStyleSheet(
			"color: #6B7280; background-color: #F8FAFC; border: 1px dashed #CBD5E1; "
			"border-radius: 10px; padding: 24px; font-weight: 600;"
		)
		self.result_empty.setAlignment(Qt.AlignCenter)
		self.result_empty.setWordWrap(True)
		result_card.layout.addWidget(self.result_empty)

		result_card.layout.addStretch(1)
		root.addWidget(result_card, 1)

		self.add_reference_url()
		self._load_existing_result()

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

		remove_btn = QToolButton()
		remove_btn.setObjectName("UrlRemoveButton")
		remove_btn.setText("x")
		remove_btn.setFixedSize(26, 26)
		remove_btn.setCursor(Qt.PointingHandCursor)
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
		if self._research_thread is not None:
			return

		self._workspace_id = current_workspace_id()
		self.run_button.setEnabled(False)
		self._clear_documents()
		self.info_row_widget.setVisible(False)
		self.documents_header.setVisible(False)
		self.result_empty.setVisible(False)
		self.status_pill.set_state("running")
		self.progress_line.setText("조사 준비 중...")
		self.progress_line.setVisible(True)
		self._start_progress_poller()
		self._start_research_worker(instruction, self.get_reference_urls())

	def _start_progress_poller(self) -> None:
		self._stop_progress_poller()
		poller = ResearchProgressPoller(self)
		poller.events.connect(self._on_progress_events)
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
		- `doc_fetched`     → add a pending DocumentBar
		- `doc_summarized`  → activate the matching bar
		- any other stage   → refresh the gray single-line progress label
		The latest message wins on the progress label, mirroring the previous
		behavior.
		"""
		latest_message = ""
		for event in items:
			if not isinstance(event, dict):
				continue
			stage = str(event.get("stage") or "").strip()
			detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
			message = str(event.get("message") or "")
			if stage == "doc_fetched":
				self._add_pending_document_bar(detail)
			elif stage == "doc_summarized":
				self._activate_document_bar(detail)
			if message:
				latest_message = message
		if latest_message:
			self._set_progress_line(latest_message)

	def _set_progress_line(self, message: str) -> None:
		text = " ".join(message.split())
		if len(text) > 200:
			text = text[:197] + "..."
		self.progress_line.setText(text)
		self.progress_line.setVisible(True)

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
		self.info_doc_count.set_value(f"{len(self._doc_bars)}건")
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

	def _start_research_worker(self, instruction: str, reference_urls: list[str]) -> None:
		thread = QThread(self)
		worker = ResearchWorker(self._workspace_id, instruction, reference_urls)
		worker.moveToThread(thread)

		thread.started.connect(worker.run)
		worker.finished.connect(self._on_research_finished)
		worker.failed.connect(self._on_research_failed)
		worker.finished.connect(thread.quit)
		worker.failed.connect(thread.quit)
		worker.finished.connect(worker.deleteLater)
		worker.failed.connect(worker.deleteLater)
		thread.finished.connect(thread.deleteLater)
		thread.finished.connect(self._clear_research_worker)

		self._research_thread = thread
		self._research_worker = worker
		thread.start()

	def _on_research_finished(self, response: dict[str, Any]) -> None:
		self._stop_progress_poller()
		self.progress_line.setVisible(False)
		try:
			load_bootstrap_state()
		except Exception:
			pass
		workspace_name = str(response.get("workspaceName") or response.get("workspaceId") or "")
		if workspace_name:
			self.workspaceChanged.emit(workspace_name)
		self._render_result(response)
		self.run_button.setEnabled(True)

	def set_workspace_by_name(self, _workspace_name: str) -> None:
		self._workspace_id = current_workspace_id()
		self._load_existing_result()

	def _load_existing_result(self) -> None:
		self._workspace_id = current_workspace_id()
		try:
			jobs = AgentController().list_research_jobs(100)
		except Exception:
			return
		current_job = next(
			(
				job
				for job in jobs
				if isinstance(job, dict) and str(job.get("workspaceId") or "") == self._workspace_id
			),
			None,
		)
		if current_job is None:
			self._show_message("조사 실행 후 agent 결과가 여기에 표시됩니다.")
			return
		self._render_result(current_job)

	def _on_research_failed(self, message: str) -> None:
		self._stop_progress_poller()
		self.progress_line.setVisible(False)
		self._clear_documents()
		self.info_row_widget.setVisible(False)
		self.documents_header.setVisible(False)
		self.result_empty.setVisible(False)
		self.status_pill.set_state("failed", error_message=message or "알 수 없는 오류가 발생했습니다.")
		self.run_button.setEnabled(True)

	def _render_result(self, response: dict[str, Any]) -> None:
		"""Apply final/persisted job state to the result card.

		Header info (status pill, info tiles) is always refreshed from the
		response. Document bars are *reconciled* in place: bars that were
		already created from live events are kept and merely have their
		summary path filled in if needed; bars for documents that the live
		stream missed are appended. This preserves the realtime UX while
		guaranteeing the final view matches the persisted truth.
		"""
		status = str(response.get("status") or "").lower().strip()
		error_message = str(response.get("error") or "").strip()
		if status == "completed":
			self.status_pill.set_state("completed")
		elif status == "failed":
			self.status_pill.set_state("failed", error_message=error_message or "조사 작업이 실패했습니다.")
		elif status == "running":
			self.status_pill.set_state("running")
		else:
			self.status_pill.set_state("idle")

		documents = response.get("documents", [])
		if not isinstance(documents, list):
			documents = []
		documents = [doc for doc in documents if isinstance(doc, dict)]

		job_name = str(
			response.get("jobId")
			or response.get("workspaceName")
			or response.get("workspaceId")
			or "-"
		)
		final_path_raw = str(response.get("finalPath") or "").strip()
		self._final_path = Path(final_path_raw) if final_path_raw else None

		self.info_job_name.set_value(job_name)
		self.info_save_path.set_value(final_path_raw or "-")
		self.info_row_widget.setVisible(True)

		self._reconcile_documents(documents)

		if self._doc_bars:
			self.documents_header.setVisible(True)
			self.result_empty.setVisible(False)
		else:
			self.documents_header.setVisible(False)
			if status == "completed":
				self.result_empty.setText("수집된 문서가 없습니다.")
				self.result_empty.setVisible(True)
			else:
				self.result_empty.setVisible(False)

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
		self.info_doc_count.set_value(f"{len(self._doc_bars)}건")

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

	def _show_message(self, text: str) -> None:
		self._clear_documents()
		self.status_pill.set_state("idle")
		self.info_row_widget.setVisible(False)
		self.documents_header.setVisible(False)
		self.result_empty.setText(text)
		self.result_empty.setVisible(True)

	def _clear_research_worker(self) -> None:
		self._research_thread = None
		self._research_worker = None

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
