from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
	QButtonGroup,
	QComboBox,
	QFileDialog,
	QFrame,
	QHBoxLayout,
	QLabel,
	QLineEdit,
	QListWidget,
	QProgressBar,
	QPushButton,
	QVBoxLayout,
	QWidget,
)

from llm.context_settings import context_risk, detect_memory, recommended_context_tokens
from llm.hardware_policy import max_parallel_slots
from llm.model_catalog import (
	DEFAULT_LLM_MODEL_ID,
	ModelSpec,
	bytes_label,
	get_model,
	llm_models,
	selected_model_from_settings,
)

from ...api_common import STATE, current_workspace_id
from ...components.buttons import AppButton
from ...components.cards import CardWidget
from ...controllers import AgentController
from ...theme import theme
from .research_page import DocCountStepper


DEFAULT_MODEL_ID = DEFAULT_LLM_MODEL_ID


def _model_size_key(spec: ModelSpec) -> str:
	prefix = "qwen35-"
	suffix = f"-{spec.quantization_key}"
	if spec.id.startswith(prefix) and spec.id.endswith(suffix):
		return spec.id[len(prefix) : -len(suffix)]
	return spec.parameter_label or spec.id


def _model_size_label(spec: ModelSpec) -> str:
	if spec.active_parameter_size_b:
		active = int(spec.active_parameter_size_b)
		return f"{spec.parameter_label}-A{active}B"
	return spec.parameter_label or spec.short_name.rsplit(" ", 1)[0]


def _model_size_options() -> list[tuple[str, str]]:
	options: list[tuple[str, str]] = []
	seen: set[str] = set()
	for spec in sorted(llm_models(), key=lambda model: model.display_order):
		size_key = _model_size_key(spec)
		if size_key in seen:
			continue
		seen.add(size_key)
		options.append((_model_size_label(spec), size_key))
	return options


def _model_variants_for_size(size_key: str) -> list[ModelSpec]:
	return [
		spec
		for spec in sorted(llm_models(), key=lambda model: model.display_order)
		if _model_size_key(spec) == size_key
	]


def _model_variant(size_key: str, quantization_key: str) -> ModelSpec:
	for spec in _model_variants_for_size(size_key):
		if spec.quantization_key == quantization_key:
			return spec
	return get_model(DEFAULT_MODEL_ID, kind="llm")


def _default_quantization_for_size(size_key: str) -> str:
	variants = _model_variants_for_size(size_key)
	for spec in variants:
		if spec.recommended:
			return spec.quantization_key
	for preferred in ("q4", "q8_0", "q6", "q5", "q3", "q2", "bf16"):
		for spec in variants:
			if spec.quantization_key == preferred:
				return spec.quantization_key
	return variants[0].quantization_key if variants else get_model(DEFAULT_MODEL_ID, kind="llm").quantization_key


def _model_label(model_id: str) -> str:
	return get_model(model_id, kind="llm").name


def _model_meta(spec: ModelSpec) -> str:
	arch = "MoE" if spec.architecture == "moe" else "Dense"
	return f"{arch} · {spec.quantization} · 약 {bytes_label(spec.size_bytes)}"

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

CONTEXT_OPTIONS = [
	("자동 권장", "auto"),
	("8K tokens", "8192"),
	("16K tokens", "16384"),
	("32K tokens", "32768"),
	("50K tokens", "50000"),
	("90K tokens", "90000"),
]


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


class _LocalAccessWorker(QObject):
	done = Signal(bool, str, dict)

	def __init__(self, workspace_id: str, folder_paths: list[str]) -> None:
		super().__init__()
		self._workspace_id = workspace_id
		self._folder_paths = list(folder_paths)

	def run(self) -> None:
		try:
			payload = AgentController().update_local_access(
				self._folder_paths,
				self._workspace_id,
			)
			self.done.emit(True, "", payload)
		except Exception as exc:
			self.done.emit(False, str(exc), {})


class SettingsPage(QWidget):
	defaultWorkspaceChanged = Signal(str)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._settings = STATE.setdefault(
			"settings",
			{
				"model": {
					"modelId": DEFAULT_MODEL_ID,
					"modelName": _model_label(DEFAULT_MODEL_ID),
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
		self._applied_model_id = str(self._settings.get("model", {}).get("modelId") or DEFAULT_MODEL_ID)
		self._context_review_required = False
		self._settings.setdefault("localAccess", {})
		research_defaults = self._settings.setdefault("research", {})
		research_defaults.setdefault("sampleCount", DEFAULT_RESEARCH_SAMPLE_COUNT)
		research_defaults.setdefault("planCount", DEFAULT_RESEARCH_PLAN_COUNT)
		autosurvey_openai_defaults = self._settings.setdefault("autosurveyOpenAI", {})
		autosurvey_openai_defaults.setdefault("provider", "local")
		autosurvey_openai_defaults.setdefault("apiKeySet", False)
		autosurvey_openai_defaults.setdefault("apiKeyPreview", "")
		self._settings.setdefault("llmParallel", DEFAULT_LLM_PARALLEL)
		self._local_access_thread: QThread | None = None
		self._local_access_worker: _LocalAccessWorker | None = None

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

		self.model_size_combo = QComboBox()
		self.model_size_combo.setObjectName("SettingsInput")
		self.model_size_combo.setMinimumWidth(190)
		for label, size_key in _model_size_options():
			self.model_size_combo.addItem(label, size_key)
		card.layout.addWidget(
			self._research_param_row(
				"모델 크기",
				"파라미터 규모입니다. 큰 모델일수록 품질은 좋아질 수 있지만 메모리와 다운로드 용량이 커집니다.",
				self.model_size_combo,
			)
		)

		self.model_quant_combo = QComboBox()
		self.model_quant_combo.setObjectName("SettingsInput")
		self.model_quant_combo.setMinimumWidth(190)
		card.layout.addWidget(
			self._research_param_row(
				"양자화",
				"가중치 압축 수준입니다. BF16/Q8은 품질 우선, Q4/Q3/Q2는 메모리 절약에 유리합니다.",
				self.model_quant_combo,
			)
		)
		self.model_size_combo.currentIndexChanged.connect(self._on_model_size_changed)
		self.model_quant_combo.currentIndexChanged.connect(self._on_model_quant_changed)

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
		self._local_access_save_button = AppButton("폴더 설정 저장")
		self._local_access_save_button.clicked.connect(self._save_local_access_settings)
		action_row.addWidget(add_button)
		action_row.addWidget(remove_button)
		action_row.addWidget(clear_button)
		action_row.addWidget(self._local_access_save_button)
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
		section.add_widget(self._build_openai_api_key_section())
		section.add_widget(self._divider())
		section.add_widget(self._build_research_method_section())
		section.add_widget(self._divider())
		section.add_widget(self._build_llama_context_section())
		section.add_widget(self._divider())
		section.add_widget(self._build_llm_parallel_section())
		return section

	def _build_openai_api_key_section(self) -> QWidget:
		section = QWidget()
		layout = QVBoxLayout(section)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)

		layout.addWidget(self._subsection_title("OpenAI API"))

		subtitle = QLabel(
			"AutoSurvey 자료 조사 파이프라인에서 OpenAI GPT API를 사용할 때 적용할 API key입니다. "
			"저장된 key는 화면에 다시 표시하지 않습니다."
		)
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		layout.addWidget(subtitle)

		self.openai_api_key_input = QLineEdit()
		self.openai_api_key_input.setObjectName("SettingsInput")
		self.openai_api_key_input.setEchoMode(QLineEdit.Password)
		self.openai_api_key_input.setMinimumWidth(280)
		self.openai_api_key_input.setPlaceholderText("sk-...")
		layout.addWidget(
			self._research_param_row(
				"API key",
				"저장하면 AutoSurvey LLM provider가 OpenAI로 활성화됩니다. 삭제하면 로컬 LLM으로 돌아갑니다.",
				self.openai_api_key_input,
			)
		)

		action_row = QHBoxLayout()
		action_row.setSpacing(8)
		action_row.addStretch(1)
		clear_button = AppButton("삭제", variant="ghost")
		clear_button.clicked.connect(self._clear_openai_api_key_settings)
		save_button = AppButton("API key 저장")
		save_button.clicked.connect(self._save_openai_api_key_settings)
		action_row.addWidget(clear_button)
		action_row.addWidget(save_button)
		layout.addLayout(action_row)

		self.openai_api_key_status = QLabel()
		self.openai_api_key_status.setObjectName("SettingsStatus")
		self.openai_api_key_status.setWordWrap(True)
		layout.addWidget(self.openai_api_key_status)

		self._load_openai_api_key_settings()
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
			"OpenAI API 사용 시에도 같은 값으로 문서별 동시 요청 수를 제한합니다."
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
		self.llm_parallel_input.valueChanged.connect(self._on_llm_parallel_changed)
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
		self._llm_parallel_reset_button = AppButton("기본값", variant="ghost")
		self._llm_parallel_reset_button.clicked.connect(self._reset_llm_parallel_settings)
		self._llm_parallel_save_button = AppButton("병렬 설정 저장")
		self._llm_parallel_save_button.clicked.connect(self._save_llm_parallel_settings)
		action_row.addWidget(self._llm_parallel_reset_button)
		action_row.addWidget(self._llm_parallel_save_button)
		layout.addLayout(action_row)

		self.llm_parallel_status = QLabel()
		self.llm_parallel_status.setObjectName("SettingsStatus")
		self.llm_parallel_status.setWordWrap(True)
		layout.addWidget(self.llm_parallel_status)

		self._load_llm_parallel_settings()
		self._sync_llm_parallel_limit()
		return section

	def _build_llama_context_section(self) -> QWidget:
		section = QWidget()
		layout = QVBoxLayout(section)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)

		layout.addWidget(self._subsection_title("컨텍스트 크기"))

		subtitle = QLabel(
			"AI 모델이 한 번에 기억하고 처리할 수 있는 토큰 범위입니다. "
			"자동 권장은 현재 PC의 여유 메모리를 기준으로 안정적인 값을 선택합니다."
		)
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		layout.addWidget(subtitle)

		self.llama_context_combo = QComboBox()
		self.llama_context_combo.setObjectName("SettingsInput")
		self.llama_context_combo.setMinimumWidth(260)
		for label, value in CONTEXT_OPTIONS:
			self.llama_context_combo.addItem(label, value)
		self.llama_context_combo.currentIndexChanged.connect(self._on_llama_context_changed)
		self.llama_context_combo.activated.connect(self._on_llama_context_activated)

		layout.addWidget(
			self._research_param_row(
				"컨텍스트 프로파일",
				"현재 PC 기준으로 여유, 적합, 위험 상태를 함께 표시합니다. 값을 높이면 긴 문서를 더 많이 담지만 메모리 사용량이 커집니다.",
				self.llama_context_combo,
			)
		)

		action_row = QHBoxLayout()
		action_row.setSpacing(8)
		action_row.addStretch(1)
		self._llama_context_reset_button = AppButton("자동 권장", variant="ghost")
		self._llama_context_reset_button.clicked.connect(self._reset_llama_context_settings)
		self._llama_context_save_button = AppButton("컨텍스트 설정 저장")
		self._llama_context_save_button.clicked.connect(self._save_llama_context_settings)
		action_row.addWidget(self._llama_context_reset_button)
		action_row.addWidget(self._llama_context_save_button)
		layout.addLayout(action_row)

		self.llama_context_status = QLabel()
		self.llama_context_status.setObjectName("SettingsStatus")
		self.llama_context_status.setWordWrap(True)
		layout.addWidget(self.llama_context_status)

		self._load_llama_context_settings()
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
		model = selected_model_from_settings(self._settings)
		self._applied_model_id = model.id
		self._set_selected_model(model.id)
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

	def _set_selected_model(self, model_name: str) -> None:
		if not hasattr(self, "model_size_combo") or not hasattr(self, "model_quant_combo"):
			return
		spec = get_model(model_name, kind="llm")
		size_key = _model_size_key(spec)
		previous_size_blocked = self.model_size_combo.blockSignals(True)
		index = self.model_size_combo.findData(size_key)
		self.model_size_combo.setCurrentIndex(max(0, index))
		self.model_size_combo.blockSignals(previous_size_blocked)
		self._refresh_model_quant_options(spec.quantization_key)

	def _selected_model(self) -> str:
		if not hasattr(self, "model_size_combo") or not hasattr(self, "model_quant_combo"):
			return DEFAULT_MODEL_ID
		size_key = str(self.model_size_combo.currentData() or "")
		quantization_key = str(self.model_quant_combo.currentData() or "")
		return _model_variant(size_key, quantization_key).id

	def _refresh_model_quant_options(self, preferred_quantization: str | None = None) -> None:
		if not hasattr(self, "model_quant_combo"):
			return
		size_key = str(self.model_size_combo.currentData() or _model_size_options()[0][1])
		current = preferred_quantization or str(self.model_quant_combo.currentData() or "")
		if not current:
			current = _default_quantization_for_size(size_key)
		previous_blocked = self.model_quant_combo.blockSignals(True)
		self.model_quant_combo.clear()
		for spec in _model_variants_for_size(size_key):
			label = f"{spec.quantization} · 약 {bytes_label(spec.size_bytes)}"
			if spec.recommended:
				label = f"{label} · 권장"
			self.model_quant_combo.addItem(label, spec.quantization_key)
		index = self.model_quant_combo.findData(current)
		if index < 0:
			index = self.model_quant_combo.findData(_default_quantization_for_size(size_key))
		self.model_quant_combo.setCurrentIndex(max(0, index))
		self.model_quant_combo.blockSignals(previous_blocked)

	def _on_model_size_changed(self, *_args) -> None:
		size_key = str(self.model_size_combo.currentData() or "")
		self._refresh_model_quant_options(_default_quantization_for_size(size_key))
		self._sync_model_context_review_state()

	def _on_model_quant_changed(self, *_args) -> None:
		self._sync_model_context_review_state()

	def _selected_model_spec(self):
		return get_model(self._selected_model(), kind="llm")

	def _selected_parallel_slots(self) -> int:
		try:
			return int(self.llm_parallel_input.value())
		except Exception:
			try:
				return int(self._settings.get("llmParallel", DEFAULT_LLM_PARALLEL))
			except (TypeError, ValueError):
				return DEFAULT_LLM_PARALLEL

	def _selected_context_tokens_for_parallel(self) -> int:
		try:
			value = str(self.llama_context_combo.currentData() or "auto")
		except Exception:
			value = "auto"
		if value == "auto":
			model = self._selected_model_spec()
			return recommended_context_tokens(
				model_limit=getattr(model, "context_tokens", None),
				model=model,
				parallel_slots=1,
			)
		try:
			return int(value)
		except ValueError:
			return self._recommended_context_for_selected_model()

	def _recommended_context_for_selected_model(self) -> int:
		model = self._selected_model_spec()
		return recommended_context_tokens(
			model_limit=getattr(model, "context_tokens", None),
			model=model,
			parallel_slots=self._selected_parallel_slots(),
		)

	def _max_parallel_for_selected_runtime(self) -> int:
		return max_parallel_slots(
			self._selected_model_spec(),
			context_per_slot_tokens=self._selected_context_tokens_for_parallel(),
			hard_limit=MAX_LLM_PARALLEL,
		)

	def _sync_llm_parallel_limit(self) -> None:
		if not hasattr(self, "llm_parallel_input"):
			return
		limit = self._max_parallel_for_selected_runtime()
		self.llm_parallel_input.setMaximum(limit)
		self._update_llm_parallel_status()

	def _context_risk_for_selected_model(self, tokens: int, auto_tokens: int) -> str:
		return context_risk(
			tokens,
			auto_tokens,
			model=self._selected_model_spec(),
			parallel_slots=self._selected_parallel_slots(),
		)

	def _sync_model_context_review_state(self) -> None:
		self._context_review_required = self._selected_model() != self._applied_model_id
		settings = self._settings.get("llamaContext", {})
		self._refresh_llama_context_options(settings if isinstance(settings, dict) else {})
		self._sync_llm_parallel_limit()
		if self._context_review_required:
			self._update_model_status("선택한 모델입니다. 컨텍스트를 다시 확인해 주세요.")
			self._update_llama_context_status("모델 변경 후 컨텍스트 확인 필요")
			return
		self._update_model_status("선택한 모델입니다.")
		self._update_llama_context_status()

	def _mark_context_reviewed(self) -> None:
		if not self._context_review_required:
			return
		self._context_review_required = False
		self._update_model_status("컨텍스트 확인 완료")
		self._update_llama_context_status("컨텍스트 확인 완료")

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
		if self._selected_model() != self._applied_model_id and self._context_review_required:
			self._update_model_status("모델 변경 후 컨텍스트를 먼저 확인해 주세요.")
			self._update_llama_context_status("모델 변경 후 컨텍스트 확인 필요")
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
			model_changed = model_id != self._applied_model_id
			self._applied_model_id = model_id
			self._context_review_required = False
			self._settings["model"] = {
				"modelId": model_id,
				"modelName": _model_label(model_id),
			}
			if model_changed:
				self._persist_llama_context_after_model_switch()
			self._update_model_status("모델이 전환되었습니다.")
		else:
			self._update_model_status(f"모델 전환 실패: {message}")

	def _set_model_controls_enabled(self, enabled: bool) -> None:
		self._model_save_button.setEnabled(enabled)
		self._model_reset_button.setEnabled(enabled)
		self.model_size_combo.setEnabled(enabled)
		self.model_quant_combo.setEnabled(enabled)

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
		if self._local_access_thread is not None:
			return
		folder_paths = self._folder_paths()
		self._settings["localAccess"] = {
			"folderPaths": folder_paths,
		}
		self._local_access_save_button.setEnabled(False)
		self._update_local_folder_status("로컬 접근 폴더를 저장하고 인덱싱하는 중입니다...")
		self._local_access_thread = QThread(self)
		self._local_access_worker = _LocalAccessWorker(current_workspace_id(), folder_paths)
		self._local_access_worker.moveToThread(self._local_access_thread)
		self._local_access_thread.started.connect(self._local_access_worker.run)
		self._local_access_worker.done.connect(self._on_local_access_saved)
		self._local_access_thread.start()

	def _on_local_access_saved(self, success: bool, message: str, payload: dict) -> None:
		thread = self._local_access_thread
		self._local_access_thread = None
		self._local_access_worker = None
		if thread is not None:
			thread.quit()
			thread.wait(2000)
		self._local_access_save_button.setEnabled(True)
		if not success:
			self._update_local_folder_status(f"로컬 접근 폴더 저장 실패: {message}")
			return
		local_access = payload.get("localAccess") if isinstance(payload, dict) else None
		if isinstance(local_access, dict):
			self._settings["localAccess"] = local_access
		local_corpus = payload.get("localCorpus") if isinstance(payload, dict) else None
		if isinstance(local_corpus, dict):
			indexed = int(local_corpus.get("indexedCount") or 0)
			skipped = int(local_corpus.get("skippedCount") or 0)
			failed = int(local_corpus.get("failedCount") or 0)
			self._update_local_folder_status(
				f"로컬 접근 폴더 저장 및 인덱싱 완료: 신규 {indexed}, 건너뜀 {skipped}, 실패 {failed}"
			)
			return
		self._update_local_folder_status("로컬 접근 폴더 설정이 저장되었습니다.")

	def _folder_paths(self) -> list[str]:
		return [self.folder_list.item(index).text() for index in range(self.folder_list.count())]

	def _load_openai_api_key_settings(self) -> None:
		autosurvey_openai = self._settings.get("autosurveyOpenAI", {})
		if not isinstance(autosurvey_openai, dict):
			autosurvey_openai = {}
		api_key_set = bool(autosurvey_openai.get("apiKeySet"))
		preview = str(autosurvey_openai.get("apiKeyPreview") or "").strip()
		if api_key_set and preview:
			self.openai_api_key_input.setPlaceholderText(f"저장됨 ({preview})")
		elif api_key_set:
			self.openai_api_key_input.setPlaceholderText("저장됨")
		else:
			self.openai_api_key_input.setPlaceholderText("sk-...")
		self.openai_api_key_input.clear()
		self._update_openai_api_key_status()

	def _save_openai_api_key_settings(self) -> None:
		api_key = self.openai_api_key_input.text().strip()
		current = self._settings.get("autosurveyOpenAI", {})
		has_existing_key = bool(current.get("apiKeySet")) if isinstance(current, dict) else False
		if not api_key:
			if has_existing_key:
				self._update_openai_api_key_status("새 key를 입력하지 않아 기존 key를 유지합니다.")
			else:
				self._update_openai_api_key_status("저장할 OpenAI API key를 입력해 주세요.")
			return
		try:
			payload = AgentController().update_autosurvey_openai_api_key(api_key)
		except Exception as e:
			self._update_openai_api_key_status(f"저장 중 오류가 발생했습니다: {e}")
			return
		autosurvey_openai = payload.get("autosurveyOpenAI", {})
		if isinstance(autosurvey_openai, dict):
			self._settings["autosurveyOpenAI"] = autosurvey_openai
		self._load_openai_api_key_settings()
		self._update_openai_api_key_status("OpenAI API key가 저장되었습니다.")

	def _clear_openai_api_key_settings(self) -> None:
		try:
			payload = AgentController().update_autosurvey_openai_api_key(clear=True)
		except Exception as e:
			self._update_openai_api_key_status(f"삭제 중 오류가 발생했습니다: {e}")
			return
		autosurvey_openai = payload.get("autosurveyOpenAI", {})
		if isinstance(autosurvey_openai, dict):
			self._settings["autosurveyOpenAI"] = autosurvey_openai
		self._load_openai_api_key_settings()
		self._update_openai_api_key_status("OpenAI API key가 삭제되었습니다. AutoSurvey는 로컬 LLM을 사용합니다.")

	def _update_openai_api_key_status(self, prefix: str | None = None) -> None:
		autosurvey_openai = self._settings.get("autosurveyOpenAI", {})
		if not isinstance(autosurvey_openai, dict):
			autosurvey_openai = {}
		lead = f"{prefix} · " if prefix else ""
		if autosurvey_openai.get("apiKeySet"):
			preview = str(autosurvey_openai.get("apiKeyPreview") or "").strip()
			suffix = f" ({preview})" if preview else ""
			self.openai_api_key_status.setText(f"{lead}OpenAI 사용 중 · API key 등록됨{suffix}")
		else:
			self.openai_api_key_status.setText(f"{lead}로컬 LLM 사용 중 · 등록된 OpenAI API key 없음")

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
		self._sync_llm_parallel_limit()

	def _load_llama_context_settings(self) -> None:
		settings = self._settings.get("llamaContext", {})
		if not isinstance(settings, dict):
			settings = {}
		self._refresh_llama_context_options(settings)
		mode = str(settings.get("mode") or "auto")
		if mode == "manual":
			value = str(settings.get("tokens") or "")
		else:
			value = "auto"
		index = self.llama_context_combo.findData(value)
		self.llama_context_combo.setCurrentIndex(max(0, index))
		self._update_llama_context_status()

	def _refresh_llama_context_options(self, settings: dict) -> None:
		auto_tokens = self._recommended_context_for_selected_model()
		current = str(self.llama_context_combo.currentData() or "auto")
		self.llama_context_combo.blockSignals(True)
		self.llama_context_combo.clear()
		self.llama_context_combo.addItem(f"자동 권장 · {auto_tokens:,} tokens · 적합", "auto")
		for _label, value in CONTEXT_OPTIONS[1:]:
			tokens = int(value)
			risk = self._context_risk_for_selected_model(tokens, auto_tokens)
			self.llama_context_combo.addItem(f"{tokens:,} tokens · {risk}", value)
		index = self.llama_context_combo.findData(current)
		self.llama_context_combo.setCurrentIndex(max(0, index))
		self.llama_context_combo.blockSignals(False)

	def _on_llama_context_changed(self, *_args) -> None:
		self._sync_llm_parallel_limit()
		self._update_llama_context_status()

	def _on_llama_context_activated(self, *_args) -> None:
		self._mark_context_reviewed()
		self._sync_llm_parallel_limit()
		self._update_llama_context_status()

	def _on_llm_parallel_changed(self, *_args) -> None:
		settings = self._settings.get("llamaContext", {})
		self._refresh_llama_context_options(settings if isinstance(settings, dict) else {})
		self._update_llm_parallel_status()
		self._update_llama_context_status()

	def _reset_llama_context_settings(self) -> None:
		index = self.llama_context_combo.findData("auto")
		self.llama_context_combo.setCurrentIndex(max(0, index))
		self._mark_context_reviewed()
		self._save_llama_context_settings()

	def _save_llama_context_settings(self) -> None:
		self._mark_context_reviewed()
		value = str(self.llama_context_combo.currentData() or "auto")
		mode = "auto" if value == "auto" else "manual"
		tokens = None if mode == "auto" else int(value)
		try:
			payload = AgentController().update_llama_context(mode, tokens)
		except Exception as e:
			self._update_llama_context_status(f"저장 중 오류가 발생했습니다: {e}")
			return
		context = payload.get("llamaContext") if isinstance(payload, dict) else None
		if isinstance(context, dict):
			self._settings["llamaContext"] = context
			self._refresh_llama_context_options(context)
		self._sync_llm_parallel_limit()
		if payload.get("restartApplied") is False:
			self._update_llama_context_status(
				f"설정은 저장됐지만 모델 서버 재시작은 실패했습니다: {payload.get('restartError')}"
			)
			return
		self._update_llama_context_status("컨텍스트 설정이 저장되고 모델 서버에 적용됐습니다.")

	def _persist_llama_context_after_model_switch(self) -> None:
		value = str(self.llama_context_combo.currentData() or "auto")
		mode = "auto" if value == "auto" else "manual"
		tokens = None if mode == "auto" else int(value)
		try:
			payload = AgentController().update_llama_context(mode, tokens)
		except Exception as e:
			self._update_llama_context_status(f"모델은 전환됐지만 컨텍스트 적용 중 오류가 발생했습니다: {e}")
			return
		context = payload.get("llamaContext") if isinstance(payload, dict) else None
		if isinstance(context, dict):
			self._settings["llamaContext"] = context
			self._refresh_llama_context_options(context)
		self._sync_llm_parallel_limit()
		if payload.get("restartApplied") is False:
			self._update_llama_context_status(
				f"모델은 전환됐지만 컨텍스트 재적용은 실패했습니다: {payload.get('restartError')}"
			)
			return
		self._update_llama_context_status("모델 변경 후 컨텍스트 설정을 다시 적용했습니다.")

	def _update_llama_context_status(self, prefix: str | None = None) -> None:
		if not hasattr(self, "llama_context_status"):
			return
		settings = self._settings.get("llamaContext", {})
		if not isinstance(settings, dict):
			settings = {}
		auto_tokens = self._recommended_context_for_selected_model()
		memory = detect_memory()
		available_gb = round(memory.available_gb, 1)
		value = str(self.llama_context_combo.currentData() or "auto")
		if value == "auto":
			tokens = auto_tokens
			risk = "적합"
			label = "자동 권장"
		else:
			tokens = int(value)
			risk = self._context_risk_for_selected_model(tokens, auto_tokens)
			label = f"{tokens:,} tokens"
		if self._context_review_required and prefix is None:
			prefix = "모델 변경 후 컨텍스트 확인 필요"
		lead = f"{prefix} · " if prefix else ""
		mem_text = f" · 여유 RAM {available_gb}GB"
		self.llama_context_status.setText(
			f"{lead}{label} · {risk} · 자동 권장 {auto_tokens:,} tokens{mem_text}"
		)

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
			payload = AgentController().update_llm_parallel(value)
		except Exception as e:
			self._update_llm_parallel_status(f"저장 중 오류가 발생했습니다: {e}")
			return
		applied = value
		if isinstance(payload, dict):
			try:
				applied = int(payload.get("llmParallel", value))
			except (TypeError, ValueError):
				applied = value
		self._settings["llmParallel"] = applied
		self._sync_llm_parallel_limit()
		self.llm_parallel_input.setValue(applied)
		if applied != value:
			self._update_llm_parallel_status(
				f"현재 모델/컨텍스트 기준 최대 {applied}개로 조정되었습니다."
			)
			return
		self._update_llm_parallel_status("병렬 디코딩 설정이 저장되었습니다.")

	def _update_llm_parallel_status(self, prefix: str | None = None) -> None:
		lead = f"{prefix} · " if prefix else ""
		value = self.llm_parallel_input.value()
		limit = MAX_LLM_PARALLEL
		try:
			limit = self._max_parallel_for_selected_runtime()
		except Exception:
			pass
		mode = "순차 처리" if value <= 1 else f"동시 {value}개"
		self.llm_parallel_status.setText(
			f"{lead}{mode} · 현재 모델/컨텍스트 기준 최대 {limit}개"
		)

	def set_default_workspace_by_name(self, _workspace_name: str) -> None:
		return

	def _update_model_status(self, prefix: str) -> None:
		model_id = self._selected_model()
		spec = get_model(model_id, kind="llm")
		self.model_status.setText(f"{prefix} · {spec.name} · {_model_meta(spec)}")

	def _update_local_folder_status(self, prefix: str | None = None) -> None:
		folder_paths = self._folder_paths()
		lead = f"{prefix} · " if prefix else ""
		if folder_paths:
			self.local_folder_status.setText(f"{lead}{len(folder_paths)}개 폴더 접근 허용")
		else:
			self.local_folder_status.setText(f"{lead}지정된 로컬 접근 폴더가 없습니다.")
