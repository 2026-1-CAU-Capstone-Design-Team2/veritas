from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
	QFrame,
	QHBoxLayout,
	QLabel,
	QLineEdit,
	QPlainTextEdit,
	QToolButton,
	QTextEdit,
	QVBoxLayout,
	QWidget,
)

from ...api_common import current_workspace_id, load_bootstrap_state
from ...components.buttons import AppButton
from ...components.cards import CardWidget
from ...controllers import AgentController


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


class ResearchPage(QWidget):
	workspaceChanged = Signal(str)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._url_rows: list[tuple[QFrame, QLineEdit]] = []
		self._workspace_id = current_workspace_id()
		self._research_thread: QThread | None = None
		self._research_worker: ResearchWorker | None = None

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
		self.result_output = QPlainTextEdit()
		self.result_output.setReadOnly(True)
		self.result_output.setMinimumHeight(260)
		self.result_output.setObjectName("DraftOutput")
		self.result_output.setPlainText("조사 실행 후 agent 결과가 여기에 표시됩니다.")
		result_card.layout.addWidget(self.result_output)
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
			self.result_output.setPlainText("조사할 내용을 입력하세요.")
			return
		if self._research_thread is not None:
			return

		self._workspace_id = current_workspace_id()
		self.run_button.setEnabled(False)
		self.result_output.setPlainText("AutoSurvey workflow를 실행하는 중입니다. 문서 수집과 요약에 시간이 걸릴 수 있습니다...")
		self._start_research_worker(instruction, self.get_reference_urls())

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
		try:
			load_bootstrap_state()
		except Exception:
			pass
		workspace_name = str(response.get("workspaceName") or response.get("workspaceId") or "")
		if workspace_name:
			self.workspaceChanged.emit(workspace_name)
		documents = response.get("documents", [])
		if not isinstance(documents, list):
			documents = []
		lines = [
			f"status: {response.get('status')}",
			f"jobId: {response.get('jobId')}",
			f"finalPath: {response.get('finalPath')}",
			f"indexedChunks: {response.get('indexedChunks')}",
			f"documentCount: {response.get('documentCount', len(documents))}",
			f"elapsedSeconds: {response.get('elapsedSeconds')}",
			"",
			"찾아낸 문서",
		]
		for index, item in enumerate(documents, start=1):
			if not isinstance(item, dict):
				continue
			title = str(item.get("title") or "Untitled")
			url = str(item.get("url") or "")
			lines.append(f"{index}. {title}")
			if url:
				lines.append(f"   {url}")
		lines.extend(["", "요약", str(response.get("summary") or "")])
		self.result_output.setPlainText("\n".join(lines).strip())
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
			return
		documents = current_job.get("documents", [])
		if not isinstance(documents, list):
			documents = []
		lines = [
			f"status: {current_job.get('status')}",
			f"jobId: {current_job.get('jobId')}",
			f"finalPath: {current_job.get('finalPath')}",
			f"documentCount: {current_job.get('documentCount', len(documents))}",
			"",
			"Collected documents",
		]
		for index, item in enumerate(documents, start=1):
			if not isinstance(item, dict):
				continue
			title = str(item.get("title") or "Untitled")
			url = str(item.get("url") or "")
			lines.append(f"{index}. {title}")
			if url:
				lines.append(f"   {url}")
		lines.extend(["", "Summary", str(current_job.get("summary") or "")])
		self.result_output.setPlainText("\n".join(lines).strip())

	def _on_research_failed(self, message: str) -> None:
		self.result_output.setPlainText(f"API 요청 실패: {message}")
		self.run_button.setEnabled(True)

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
