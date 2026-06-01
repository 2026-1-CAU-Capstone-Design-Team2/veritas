from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
	QButtonGroup,
	QFileDialog,
	QFrame,
	QHBoxLayout,
	QLabel,
	QListWidget,
	QProgressBar,
	QPushButton,
	QVBoxLayout,
	QWidget,
)

from ...api_common import STATE
from ...components.buttons import AppButton
from ...components.cards import CardWidget
from ...controllers import AgentController
from ...theme import theme
from .research_page import DocCountStepper


MODEL_OPTIONS = [
	("0.8B 8bit", "qwen35-0.8b-q8_0"),
	("2B 8bit", "qwen35-2b-q8_0"),
	("4B 4bit", "qwen35-4b-q4"),
	("9B 4bit", "qwen35-9b-q4"),
]
DEFAULT_MODEL_ID = "qwen35-0.8b-q8_0"
MODEL_LABELS = {model_id: label for label, model_id in MODEL_OPTIONS}

# 조사 진행 방식 — defaults + frontend-enforced bounds for how the AutoSurvey
# LLM paces its research. The plan count has no real upper limit, so
# MAX_RESEARCH_PLAN_COUNT is just a large practical cap (the stepper needs a
# finite maximum).
DEFAULT_RESEARCH_SAMPLE_COUNT = 3
MIN_RESEARCH_SAMPLE_COUNT = 3
MAX_RESEARCH_SAMPLE_COUNT = 5
DEFAULT_RESEARCH_PLAN_COUNT = 5
MIN_RESEARCH_PLAN_COUNT = 5
MAX_RESEARCH_PLAN_COUNT = 9999

# 병렬 디코딩 — concurrent LLM requests for per-doc cleanup/summary + embeddings
# (LLMClient.max_parallel / llama-server -np). Hard-bounded 1~5: 1 = serial,
# 5 caps how hard a low-spec local server is pushed.
DEFAULT_LLM_PARALLEL = 1
MIN_LLM_PARALLEL = 1
MAX_LLM_PARALLEL = 5


class _CollapsibleHeader(QPushButton):
	"""Flat, full-width header for a CollapsibleSection.

	Reads as a card title rather than a button; a hand-painted chevron on the
	right edge points right when collapsed and down when expanded. The glyph is
	painted (not a text character) so it stays crisp and exactly placed.
	"""

	def __init__(self, title: str, parent: QWidget | None = None) -> None:
		super().__init__(title, parent)
		self.setObjectName("AdvancedToggleHeader")
		self.setCheckable(True)
		self.setCursor(Qt.PointingHandCursor)
		self.setFocusPolicy(Qt.NoFocus)
		self.setFixedHeight(30)
		# Surface colours come from the app stylesheet (#AdvancedToggleHeader); the
		# hand-painted chevron repaints itself on a theme toggle.
		theme.themeChanged.connect(self._apply_theme)

	def _apply_theme(self, *args) -> None:
		self.update()

	def paintEvent(self, event) -> None:  # type: ignore[override]
		super().paintEvent(event)  # title text + hover colour from the stylesheet
		painter = QPainter(self)
		painter.setRenderHint(QPainter.Antialiasing, True)
		color = (
			QColor(theme.color("accent"))
			if self.underMouse()
			else QColor(theme.color("text.slate600"))
		)
		pen = QPen(color)
		pen.setWidthF(2.0)
		pen.setCapStyle(Qt.RoundCap)
		pen.setJoinStyle(Qt.RoundJoin)
		painter.setPen(pen)
		cx = self.width() - 13.0
		cy = self.height() / 2.0 + 0.5
		arm = 4.0
		path = QPainterPath()
		if self.isChecked():  # expanded -> chevron points down
			path.moveTo(cx - arm, cy - arm * 0.55)
			path.lineTo(cx, cy + arm * 0.55)
			path.lineTo(cx + arm, cy - arm * 0.55)
		else:  # collapsed -> chevron points right
			path.moveTo(cx - arm * 0.55, cy - arm)
			path.lineTo(cx + arm * 0.55, cy)
			path.lineTo(cx - arm * 0.55, cy + arm)
		painter.drawPath(path)


class CollapsibleSection(CardWidget):
	"""A CardWidget whose body collapses behind a clickable header.

	Reuses the settings page's card surface (white panel, rounded border,
	shadow) so the toggle reads as part of the same design language as the
	other setting cards.
	"""

	def __init__(
		self,
		title: str,
		expanded: bool = False,
		parent: QWidget | None = None,
	) -> None:
		super().__init__(parent=parent)

		self._header = _CollapsibleHeader(title)
		self._header.toggled.connect(self._on_toggled)

		self._body = QWidget()
		self.body_layout = QVBoxLayout(self._body)
		self.body_layout.setContentsMargins(0, 6, 0, 0)
		self.body_layout.setSpacing(14)

		self.layout.addWidget(self._header)
		self.layout.addWidget(self._body)

		self._header.setChecked(expanded)
		self._body.setVisible(expanded)

	def add_widget(self, widget: QWidget) -> None:
		self.body_layout.addWidget(widget)

	def _on_toggled(self, checked: bool) -> None:
		self._body.setVisible(checked)


class _ModelSwitchWorker(QObject):
	"""Runs the (possibly multi-minute, download-bearing) model switch off the
	UI thread so the settings window never freezes. Emits ``done(success,
	message)`` when the API call returns."""

	done = Signal(bool, str)

	def __init__(self, model_id: str) -> None:
		super().__init__()
		self._model_id = model_id

	def run(self) -> None:
		try:
			AgentController().update_model(self._model_id)
			self.done.emit(True, "")
		except Exception as exc:  # surfaced on the settings status line
			self.done.emit(False, str(exc))


class SettingsPage(QWidget):
	defaultWorkspaceChanged = Signal(str)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._settings = STATE.setdefault(
			"settings",
			{
				"model": {
					"modelId": DEFAULT_MODEL_ID,
					"modelName": MODEL_LABELS[DEFAULT_MODEL_ID],
				},
				"embeddingModel": {
					"modelId": "granite-embedding-97m-r2-q8_0",
					"modelName": "Granite Embedding 97M Multilingual R2 8-bit",
				},
				"localAccess": {
					"folderPaths": [],
				},
			},
		)
		self._settings.setdefault("model", {}).setdefault("modelId", DEFAULT_MODEL_ID)
		self._settings.setdefault("localAccess", {})
		research_defaults = self._settings.setdefault("research", {})
		research_defaults.setdefault("sampleCount", DEFAULT_RESEARCH_SAMPLE_COUNT)
		research_defaults.setdefault("planCount", DEFAULT_RESEARCH_PLAN_COUNT)
		self._settings.setdefault("llmParallel", DEFAULT_LLM_PARALLEL)
		self._model_buttons: dict[str, QPushButton] = {}

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(14)

		root.addWidget(self._build_appearance_card())
		root.addWidget(self._build_model_card())
		root.addWidget(self._build_local_access_card())
		root.addWidget(self._build_advanced_section())
		root.addStretch(1)

	def _build_appearance_card(self) -> CardWidget:
		card = CardWidget("다크모드")

		subtitle = QLabel("라이트 모드와 다크 모드를 선택합니다. 상단 헤더의 토글로도 전환할 수 있습니다.")
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		card.layout.addWidget(subtitle)

		toggle_row = QHBoxLayout()
		toggle_row.setSpacing(8)
		self._theme_group = QButtonGroup(self)
		self._theme_group.setExclusive(True)
		self._theme_buttons: dict[str, QPushButton] = {}
		for label, mode in (("Light Mode", "light"), ("Dark Mode", "dark")):
			button = QPushButton(label)
			button.setObjectName("SettingsModelToggle")
			button.setCheckable(True)
			button.setCursor(Qt.PointingHandCursor)
			button.clicked.connect(lambda _checked=False, m=mode: theme.set_mode(m))
			self._theme_group.addButton(button)
			self._theme_buttons[mode] = button
			toggle_row.addWidget(button)
		toggle_row.addStretch(1)
		card.layout.addLayout(toggle_row)

		self._sync_theme_buttons()
		# Reflect changes made from the header toggle too.
		theme.themeChanged.connect(self._sync_theme_buttons)
		return card

	def _sync_theme_buttons(self, *args) -> None:
		button = self._theme_buttons.get(theme.mode())
		if button is not None:
			button.setChecked(True)

	def _build_model_card(self) -> CardWidget:
		card = CardWidget("모델 설정")

		subtitle = QLabel("초안 생성, 문서 보조, AI 채팅에서 사용할 모델명을 선택합니다.")
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		card.layout.addWidget(subtitle)

		self.model_group = QButtonGroup(self)
		self.model_group.setExclusive(True)

		toggle_row = QHBoxLayout()
		toggle_row.setSpacing(8)
		for label, model_name in MODEL_OPTIONS:
			button = QPushButton(label)
			button.setObjectName("SettingsModelToggle")
			button.setCheckable(True)
			button.setCursor(Qt.PointingHandCursor)
			button.clicked.connect(lambda _checked, value=model_name: self._select_model(value))
			self.model_group.addButton(button)
			self._model_buttons[model_name] = button
			toggle_row.addWidget(button)
		toggle_row.addStretch(1)
		card.layout.addLayout(toggle_row)

		self.model_status = QLabel()
		self.model_status.setObjectName("SettingsStatus")
		self.model_status.setWordWrap(True)
		card.layout.addWidget(self.model_status)

		# Hidden until a switch starts; reflects the download/restart progress
		# streamed from /api/v1/settings/model/progress.
		self.model_progress = QProgressBar()
		self.model_progress.setRange(0, 100)
		self.model_progress.setValue(0)
		self.model_progress.setVisible(False)
		card.layout.addWidget(self.model_progress)

		action_row = QHBoxLayout()
		action_row.addStretch(1)
		self._model_reset_button = AppButton("기본값", variant="ghost")
		self._model_reset_button.clicked.connect(self._reset_model_settings)
		self._model_save_button = AppButton("모델 저장")
		self._model_save_button.clicked.connect(self._save_model_settings)
		action_row.addWidget(self._model_reset_button)
		action_row.addWidget(self._model_save_button)
		card.layout.addLayout(action_row)

		# Live model-switch worker + progress poller state.
		self._model_switch_thread: QThread | None = None
		self._model_switch_worker: _ModelSwitchWorker | None = None
		self._model_progress_cursor = 0
		self._model_progress_timer = QTimer(self)
		self._model_progress_timer.setInterval(700)
		self._model_progress_timer.timeout.connect(self._poll_model_progress)

		self._load_model_settings()
		return card

	def _build_local_access_card(self) -> CardWidget:
		card = CardWidget("로컬 접근 폴더 설정")

		subtitle = QLabel("자료조사와 RAG에서 접근을 허용할 로컬 폴더를 지정합니다.")
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		card.layout.addWidget(subtitle)

		self.folder_list = QListWidget()
		self.folder_list.setObjectName("SettingsFolderList")
		self.folder_list.setMinimumHeight(150)
		card.layout.addWidget(self.folder_list)

		action_row = QHBoxLayout()
		action_row.addStretch(1)
		add_button = AppButton("폴더 추가", variant="ghost")
		add_button.clicked.connect(self._browse_local_folder)
		remove_button = AppButton("선택 삭제", variant="ghost")
		remove_button.clicked.connect(self._remove_selected_folder)
		clear_button = AppButton("전체 비우기", variant="ghost")
		clear_button.clicked.connect(self._clear_local_folders)
		save_button = AppButton("폴더 설정 저장")
		save_button.clicked.connect(self._save_local_access_settings)
		action_row.addWidget(add_button)
		action_row.addWidget(remove_button)
		action_row.addWidget(clear_button)
		action_row.addWidget(save_button)
		card.layout.addLayout(action_row)

		self.local_folder_status = QLabel()
		self.local_folder_status.setObjectName("SettingsStatus")
		self.local_folder_status.setWordWrap(True)
		card.layout.addWidget(self.local_folder_status)

		self._load_local_access_settings()
		return card

	def _build_advanced_section(self) -> CollapsibleSection:
		"""고급 설정: a collapsed-by-default card holding the less-used settings —
		the document-tool registry and the AutoSurvey research pacing."""
		section = CollapsibleSection("고급 설정", expanded=False)
		section.add_widget(self._build_research_method_section())
		section.add_widget(self._divider())
		section.add_widget(self._build_llm_parallel_section())
		return section

	def _build_research_method_section(self) -> QWidget:
		section = QWidget()
		layout = QVBoxLayout(section)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)

		layout.addWidget(self._subsection_title("조사 진행 방식"))

		subtitle = QLabel(
			"AutoSurvey LLM이 자료 조사를 진행하는 방식을 설정합니다. "
			"최초 샘플링 개수와 각 플랜당 조사 개수를 조절할 수 있습니다."
		)
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		layout.addWidget(subtitle)

		# Same −/＋ stepper widget the 조사 페이지 uses for "최대 조사 문서 수";
		# the min/max passed here are the frontend-enforced bounds.
		self.research_sample_input = DocCountStepper(
			MIN_RESEARCH_SAMPLE_COUNT,
			MAX_RESEARCH_SAMPLE_COUNT,
			DEFAULT_RESEARCH_SAMPLE_COUNT,
		)
		layout.addWidget(
			self._research_param_row(
				"최초 샘플링 개수",
				f"조사를 시작할 때 LLM이 처음으로 살펴볼 자료 샘플의 개수입니다. "
				f"({MIN_RESEARCH_SAMPLE_COUNT}~{MAX_RESEARCH_SAMPLE_COUNT}개, "
				f"기본값 {DEFAULT_RESEARCH_SAMPLE_COUNT}개)",
				self.research_sample_input,
			)
		)

		self.research_plan_input = DocCountStepper(
			MIN_RESEARCH_PLAN_COUNT,
			MAX_RESEARCH_PLAN_COUNT,
			DEFAULT_RESEARCH_PLAN_COUNT,
		)
		layout.addWidget(
			self._research_param_row(
				"각 플랜당 조사 개수",
				f"LLM이 세운 각 조사 플랜마다 한 번에 조사할 자료의 개수입니다. "
				f"(최소 {MIN_RESEARCH_PLAN_COUNT}개, 최대 무제한 · "
				f"기본값 {DEFAULT_RESEARCH_PLAN_COUNT}개)",
				self.research_plan_input,
			)
		)

		action_row = QHBoxLayout()
		action_row.setSpacing(8)
		action_row.addStretch(1)
		reset_button = AppButton("기본값", variant="ghost")
		reset_button.clicked.connect(self._reset_research_method_settings)
		save_button = AppButton("조사 방식 저장")
		save_button.clicked.connect(self._save_research_method_settings)
		action_row.addWidget(reset_button)
		action_row.addWidget(save_button)
		layout.addLayout(action_row)

		self.research_method_status = QLabel()
		self.research_method_status.setObjectName("SettingsStatus")
		self.research_method_status.setWordWrap(True)
		layout.addWidget(self.research_method_status)

		self._load_research_method_settings()
		return section

	def _build_llm_parallel_section(self) -> QWidget:
		section = QWidget()
		layout = QVBoxLayout(section)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)

		layout.addWidget(self._subsection_title("병렬 디코딩"))

		subtitle = QLabel(
			"문서 정제·요약 등 LLM 호출을 동시에 몇 개까지 처리할지 설정합니다. "
			"값을 올리면 자료조사 속도가 빨라지지만 로컬 LLM 서버 메모리를 더 사용합니다. "
			"LLM 서버의 병렬 슬롯 수(-np)와 맞추는 것이 좋습니다."
		)
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		layout.addWidget(subtitle)

		# Same −/＋ stepper the 조사 페이지 / 조사 진행 방식 use, bounded 1~5.
		self.llm_parallel_input = DocCountStepper(
			MIN_LLM_PARALLEL,
			MAX_LLM_PARALLEL,
			DEFAULT_LLM_PARALLEL,
		)
		layout.addWidget(
			self._research_param_row(
				"동시 처리 개수",
				f"동시에 실행할 LLM 요청 수입니다. "
				f"({MIN_LLM_PARALLEL}~{MAX_LLM_PARALLEL}, 기본값 {DEFAULT_LLM_PARALLEL} = 순차 처리)",
				self.llm_parallel_input,
			)
		)

		action_row = QHBoxLayout()
		action_row.setSpacing(8)
		action_row.addStretch(1)
		reset_button = AppButton("기본값", variant="ghost")
		reset_button.clicked.connect(self._reset_llm_parallel_settings)
		save_button = AppButton("병렬 설정 저장")
		save_button.clicked.connect(self._save_llm_parallel_settings)
		action_row.addWidget(reset_button)
		action_row.addWidget(save_button)
		layout.addLayout(action_row)

		self.llm_parallel_status = QLabel()
		self.llm_parallel_status.setObjectName("SettingsStatus")
		self.llm_parallel_status.setWordWrap(True)
		layout.addWidget(self.llm_parallel_status)

		self._load_llm_parallel_settings()
		return section

	def _research_param_row(self, title: str, hint: str, field: QWidget) -> QFrame:
		"""A title/hint + control row, styled like the 조사 페이지's count card so
		the advanced settings stay visually consistent with the rest of the app."""
		row = QFrame()
		row.setObjectName("ResearchCountCard")
		row_layout = QHBoxLayout(row)
		row_layout.setContentsMargins(16, 13, 16, 13)
		row_layout.setSpacing(14)

		text_col = QVBoxLayout()
		text_col.setContentsMargins(0, 0, 0, 0)
		text_col.setSpacing(3)
		title_label = QLabel(title)
		title_label.setObjectName("ResearchCountTitle")
		hint_label = QLabel(hint)
		hint_label.setObjectName("ResearchCountHint")
		hint_label.setWordWrap(True)
		text_col.addWidget(title_label)
		text_col.addWidget(hint_label)

		row_layout.addLayout(text_col, 1)
		row_layout.addWidget(field, 0, Qt.AlignVCenter)
		return row

	def _subsection_title(self, text: str) -> QLabel:
		label = QLabel(text)
		label.setObjectName("SettingsSubsectionTitle")
		return label

	def _divider(self) -> QFrame:
		line = QFrame()
		line.setObjectName("SettingsDivider")
		line.setFixedHeight(1)
		return line

	def _load_model_settings(self) -> None:
		model_settings = self._settings.get("model", {})
		model_id = model_settings.get("modelId")
		if not model_id:
			legacy_name = str(model_settings.get("modelName") or "")
			model_id = {
				"0.8B": "qwen35-0.8b-q8_0",
				"2B": "qwen35-2b-q8_0",
				"4B": "qwen35-4b-q4",
				"9B": "qwen35-9b-q4",
			}.get(legacy_name, DEFAULT_MODEL_ID)
		self._set_selected_model(str(model_id))
		self._update_model_status("현재 모델이 적용되어 있습니다.")

	def _load_local_access_settings(self) -> None:
		local_settings = self._settings.get("localAccess", {})
		folder_paths = list(local_settings.get("folderPaths") or [])
		legacy_file_path = local_settings.get("filePath")
		if legacy_file_path and not folder_paths:
			folder_paths = [str(legacy_file_path)]

		self.folder_list.clear()
		for folder_path in folder_paths:
			self._add_folder_item(str(folder_path))
		self._update_local_folder_status()

	def _select_model(self, model_name: str) -> None:
		self._set_selected_model(model_name)
		self._update_model_status("선택한 모델입니다.")

	def _set_selected_model(self, model_name: str) -> None:
		if model_name not in self._model_buttons:
			model_name = DEFAULT_MODEL_ID
		self._model_buttons[model_name].setChecked(True)

	def _selected_model(self) -> str:
		for model_name, button in self._model_buttons.items():
			if button.isChecked():
				return model_name
		return DEFAULT_MODEL_ID

	def _reset_model_settings(self) -> None:
		self._set_selected_model(DEFAULT_MODEL_ID)
		self._save_model_settings()

	def _save_model_settings(self) -> None:
		# Live switch: a model change downloads (if needed) + restarts the
		# llama-server, which can take minutes. Run it on a worker thread and
		# stream progress so the settings window stays responsive. Guard against
		# overlapping switches.
		if self._model_switch_thread is not None:
			return
		model_id = self._selected_model()
		self._set_model_controls_enabled(False)
		self.model_progress.setVisible(True)
		self.model_progress.setRange(0, 0)
		self.model_progress.setValue(0)
		self._model_progress_cursor = 0
		self.model_status.setText("모델 전환을 시작합니다...")
		self._model_switch_thread = QThread(self)
		self._model_switch_worker = _ModelSwitchWorker(model_id)
		self._model_switch_worker.moveToThread(self._model_switch_thread)
		self._model_switch_thread.started.connect(self._model_switch_worker.run)
		self._model_switch_worker.done.connect(self._on_model_switch_done)
		self._model_switch_thread.start()
		self._model_progress_timer.start()

	def _poll_model_progress(self) -> None:
		try:
			payload = AgentController().get_model_switch_progress(
				since=self._model_progress_cursor
			)
		except Exception:
			return
		for event in payload.get("items", []):
			self._model_progress_cursor = max(
				self._model_progress_cursor, int(event.get("seq", 0))
			)
			message = str(event.get("message") or "").strip()
			if message:
				self.model_status.setText(message)
			pct = (event.get("detail") or {}).get("pct")
			if isinstance(pct, int):
				self.model_progress.setRange(0, 100)
				self.model_progress.setValue(max(0, min(100, pct)))

	def _on_model_switch_done(self, success: bool, message: str) -> None:
		self._model_progress_timer.stop()
		self._poll_model_progress()
		thread = self._model_switch_thread
		self._model_switch_thread = None
		self._model_switch_worker = None
		if thread is not None:
			thread.quit()
			thread.wait(2000)
		self.model_progress.setRange(0, 100)
		self.model_progress.setVisible(False)
		self._set_model_controls_enabled(True)
		if success:
			model_id = self._selected_model()
			self._settings["model"] = {
				"modelId": model_id,
				"modelName": MODEL_LABELS.get(model_id, model_id),
			}
			self._update_model_status("모델이 전환되었습니다.")
		else:
			self._update_model_status(f"모델 전환 실패: {message}")

	def _set_model_controls_enabled(self, enabled: bool) -> None:
		self._model_save_button.setEnabled(enabled)
		self._model_reset_button.setEnabled(enabled)
		for button in self._model_buttons.values():
			button.setEnabled(enabled)

	def _browse_local_folder(self) -> None:
		folder_path = QFileDialog.getExistingDirectory(
			self,
			"접근을 허용할 로컬 폴더 선택",
			"",
		)
		if folder_path:
			self._add_folder_item(folder_path)
			self._update_local_folder_status()

	def _add_folder_item(self, folder_path: str) -> None:
		normalized = folder_path.strip()
		if not normalized:
			return
		if normalized in self._folder_paths():
			return
		self.folder_list.addItem(normalized)

	def _remove_selected_folder(self) -> None:
		for item in self.folder_list.selectedItems():
			row = self.folder_list.row(item)
			self.folder_list.takeItem(row)
		self._update_local_folder_status()

	def _clear_local_folders(self) -> None:
		self.folder_list.clear()
		self._save_local_access_settings()

	def _save_local_access_settings(self) -> None:
		folder_paths = self._folder_paths()
		self._settings["localAccess"] = {
			"folderPaths": folder_paths,
		}
		self._update_local_folder_status("로컬 접근 폴더 설정이 저장되었습니다.")

	def _folder_paths(self) -> list[str]:
		return [self.folder_list.item(index).text() for index in range(self.folder_list.count())]

	def _load_research_method_settings(self) -> None:
		research_settings = self._settings.get("research", {})
		sample_count = research_settings.get("sampleCount", DEFAULT_RESEARCH_SAMPLE_COUNT)
		plan_count = research_settings.get("planCount", DEFAULT_RESEARCH_PLAN_COUNT)
		try:
			self.research_sample_input.setValue(int(sample_count))
		except (TypeError, ValueError):
			self.research_sample_input.setValue(DEFAULT_RESEARCH_SAMPLE_COUNT)
		try:
			self.research_plan_input.setValue(int(plan_count))
		except (TypeError, ValueError):
			self.research_plan_input.setValue(DEFAULT_RESEARCH_PLAN_COUNT)
		self._update_research_method_status()

	def _reset_research_method_settings(self) -> None:
		self.research_sample_input.setValue(DEFAULT_RESEARCH_SAMPLE_COUNT)
		self.research_plan_input.setValue(DEFAULT_RESEARCH_PLAN_COUNT)
		self._save_research_method_settings()

	def _save_research_method_settings(self) -> None:
		sample_count = self.research_sample_input.value()
		plan_count = self.research_plan_input.value()
		self._settings["research"] = {
			"sampleCount": sample_count,
			"planCount": plan_count,
		}
		# Persist to the backend so the value survives load_bootstrap_state()
		# (which replaces STATE["settings"] wholesale) and is actually applied
		# when a research run reads STATE["settings"]["research"].
		try:
			AgentController().update_research_method(sample_count, plan_count)
		except Exception as e:
			self._update_research_method_status(f"저장 중 오류가 발생했습니다: {e}")
			return
		self._update_research_method_status("조사 진행 방식 설정이 저장되었습니다.")

	def _update_research_method_status(self, prefix: str | None = None) -> None:
		lead = f"{prefix} · " if prefix else ""
		self.research_method_status.setText(
			f"{lead}최초 샘플링 {self.research_sample_input.value()}개 · "
			f"플랜당 {self.research_plan_input.value()}개"
		)

	def _load_llm_parallel_settings(self) -> None:
		value = self._settings.get("llmParallel", DEFAULT_LLM_PARALLEL)
		try:
			self.llm_parallel_input.setValue(int(value))
		except (TypeError, ValueError):
			self.llm_parallel_input.setValue(DEFAULT_LLM_PARALLEL)
		self._update_llm_parallel_status()

	def _reset_llm_parallel_settings(self) -> None:
		self.llm_parallel_input.setValue(DEFAULT_LLM_PARALLEL)
		self._save_llm_parallel_settings()

	def _save_llm_parallel_settings(self) -> None:
		value = self.llm_parallel_input.value()
		self._settings["llmParallel"] = value
		# Persist to the backend so the value survives load_bootstrap_state()
		# (which replaces STATE["settings"] wholesale) and is applied live to
		# the shared LLM client's max_parallel.
		try:
			AgentController().update_llm_parallel(value)
		except Exception as e:
			self._update_llm_parallel_status(f"저장 중 오류가 발생했습니다: {e}")
			return
		self._update_llm_parallel_status("병렬 디코딩 설정이 저장되었습니다.")

	def _update_llm_parallel_status(self, prefix: str | None = None) -> None:
		lead = f"{prefix} · " if prefix else ""
		value = self.llm_parallel_input.value()
		mode = "순차 처리" if value <= 1 else f"동시 {value}개"
		self.llm_parallel_status.setText(f"{lead}{mode}")

	def set_default_workspace_by_name(self, _workspace_name: str) -> None:
		return

	def _update_model_status(self, prefix: str) -> None:
		model_id = self._selected_model()
		self.model_status.setText(f"{prefix} · {MODEL_LABELS.get(model_id, model_id)}")

	def _update_local_folder_status(self, prefix: str | None = None) -> None:
		folder_paths = self._folder_paths()
		lead = f"{prefix} · " if prefix else ""
		if folder_paths:
			self.local_folder_status.setText(f"{lead}{len(folder_paths)}개 폴더 접근 허용")
		else:
			self.local_folder_status.setText(f"{lead}지정된 로컬 접근 폴더가 없습니다.")
