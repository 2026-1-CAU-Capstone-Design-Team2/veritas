from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
	QButtonGroup,
	QFileDialog,
	QFrame,
	QHBoxLayout,
	QLabel,
	QLayout,
	QLayoutItem,
	QLineEdit,
	QListWidget,
	QListWidgetItem,
	QPushButton,
	QSizePolicy,
	QVBoxLayout,
	QWidget,
)

from ...api_common import STATE
from ...components.buttons import AppButton
from ...components.cards import CardWidget
from ...controllers import AgentController
from .research_page import DocCountStepper


class FlowLayout(QLayout):
	"""Left-to-right layout that wraps items onto the next line when the
	current row runs out of width.

	Used for the read-only "지원되는 도구" chips so they reflow cleanly as the
	settings card is resized, instead of overflowing or clipping.
	"""

	def __init__(self, parent: QWidget | None = None, spacing: int = 8) -> None:
		super().__init__(parent)
		self._items: list[QLayoutItem] = []
		self._spacing = spacing
		self.setContentsMargins(0, 0, 0, 0)

	def addItem(self, item: QLayoutItem) -> None:  # type: ignore[override]
		self._items.append(item)

	def count(self) -> int:  # type: ignore[override]
		return len(self._items)

	def itemAt(self, index: int) -> QLayoutItem | None:  # type: ignore[override]
		if 0 <= index < len(self._items):
			return self._items[index]
		return None

	def takeAt(self, index: int) -> QLayoutItem | None:  # type: ignore[override]
		if 0 <= index < len(self._items):
			return self._items.pop(index)
		return None

	def expandingDirections(self) -> Qt.Orientations:  # type: ignore[override]
		return Qt.Orientation(0)

	def hasHeightForWidth(self) -> bool:  # type: ignore[override]
		return True

	def heightForWidth(self, width: int) -> int:  # type: ignore[override]
		return self._do_layout(QRect(0, 0, width, 0), test_only=True)

	def setGeometry(self, rect: QRect) -> None:  # type: ignore[override]
		super().setGeometry(rect)
		self._do_layout(rect, test_only=False)

	def sizeHint(self) -> QSize:  # type: ignore[override]
		return self.minimumSize()

	def minimumSize(self) -> QSize:  # type: ignore[override]
		size = QSize()
		for item in self._items:
			size = size.expandedTo(item.minimumSize())
		return size

	def _do_layout(self, rect: QRect, test_only: bool) -> int:
		x = rect.x()
		y = rect.y()
		line_height = 0
		for item in self._items:
			hint = item.sizeHint()
			next_x = x + hint.width() + self._spacing
			if next_x - self._spacing > rect.right() and line_height > 0:
				x = rect.x()
				y = y + line_height + self._spacing
				next_x = x + hint.width() + self._spacing
				line_height = 0
			if not test_only:
				item.setGeometry(QRect(QPoint(x, y), hint))
			x = next_x
			line_height = max(line_height, hint.height())
		return y + line_height - rect.y()


MODEL_OPTIONS = [
	("0.8B", "0.8B"),
	("9B", "9B"),
]

# Document editing tools VERITAS already recognizes as "문서 작업" screens.
# Shown read-only in the settings page so users see the current coverage
# before adding their own (e.g. a new collaboration tool).
BUILTIN_DOCUMENT_TOOLS = [
	"txt",
	"md",
	"rst",
	"log",
	"pdf",
	"docx",
	"pptx",
	"ppt",
	"hwp",
	"hwpx",
	"raw",
]

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
		self.setStyleSheet(
			"QPushButton#AdvancedToggleHeader {"
			" background-color: transparent; border: none; text-align: left;"
			" padding: 0px; color: #0F172A; font-size: 14px; font-weight: 800; }"
			"QPushButton#AdvancedToggleHeader:hover { color: #4F46E5; }"
		)

	def paintEvent(self, event) -> None:  # type: ignore[override]
		super().paintEvent(event)  # title text + hover colour from the stylesheet
		painter = QPainter(self)
		painter.setRenderHint(QPainter.Antialiasing, True)
		color = QColor("#4F46E5") if self.underMouse() else QColor("#475569")
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


class SettingsPage(QWidget):
	defaultWorkspaceChanged = Signal(str)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._settings = STATE.setdefault(
			"settings",
			{
				"model": {
					"modelName": "0.8B",
				},
				"localAccess": {
					"folderPaths": [],
				},
				"documentTools": {
					"custom": [],
				},
			},
		)
		self._settings.setdefault("model", {}).setdefault("modelName", "0.8B")
		self._settings.setdefault("localAccess", {})
		self._settings.setdefault("documentTools", {}).setdefault("custom", [])
		research_defaults = self._settings.setdefault("research", {})
		research_defaults.setdefault("sampleCount", DEFAULT_RESEARCH_SAMPLE_COUNT)
		research_defaults.setdefault("planCount", DEFAULT_RESEARCH_PLAN_COUNT)
		self._model_buttons: dict[str, QPushButton] = {}

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(14)

		root.addWidget(self._build_model_card())
		root.addWidget(self._build_local_access_card())
		root.addWidget(self._build_advanced_section())
		root.addStretch(1)

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

		action_row = QHBoxLayout()
		action_row.addStretch(1)
		reset_button = AppButton("기본값", variant="ghost")
		reset_button.clicked.connect(self._reset_model_settings)
		save_button = AppButton("모델 저장")
		save_button.clicked.connect(self._save_model_settings)
		action_row.addWidget(reset_button)
		action_row.addWidget(save_button)
		card.layout.addLayout(action_row)

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
		section.add_widget(self._build_document_tools_section())
		section.add_widget(self._divider())
		section.add_widget(self._build_research_method_section())
		return section

	def _build_document_tools_section(self) -> QWidget:
		section = QWidget()
		layout = QVBoxLayout(section)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)

		layout.addWidget(self._subsection_title("새로운 문서 작업 도구 추가"))

		subtitle = QLabel(
			"VERITAS가 '문서 작업' 화면으로 인식하는 편집 도구입니다. "
			"Notion 같은 협업 툴이나 새로운 편집기를 직접 추가할 수 있습니다."
		)
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		layout.addWidget(subtitle)

		# --- 현재 지원되는 도구: 읽기 전용 칩 ---
		builtin_label = QLabel("현재 지원되는 도구")
		builtin_label.setObjectName("CardPrimary")
		layout.addWidget(builtin_label)

		chip_container = QWidget()
		chip_container.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
		chip_flow = FlowLayout(chip_container, spacing=8)
		for tool in BUILTIN_DOCUMENT_TOOLS:
			chip = QLabel(tool)
			chip.setObjectName("ToolChip")
			chip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
			chip_flow.addWidget(chip)
		layout.addWidget(chip_container)

		# --- 직접 추가한 도구: 목록 ---
		custom_label = QLabel("직접 추가한 도구")
		custom_label.setObjectName("CardPrimary")
		layout.addWidget(custom_label)

		self.document_tool_list = QListWidget()
		self.document_tool_list.setObjectName("SettingsFolderList")
		self.document_tool_list.setMinimumHeight(108)
		layout.addWidget(self.document_tool_list)

		# --- 추가 입력 영역: 라벨이 붙은 한 줄 폼 ---
		add_row = QFrame()
		add_row.setObjectName("DocToolAddRow")
		add_layout = QHBoxLayout(add_row)
		add_layout.setContentsMargins(14, 12, 14, 12)
		add_layout.setSpacing(10)

		name_col = QVBoxLayout()
		name_col.setContentsMargins(0, 0, 0, 0)
		name_col.setSpacing(5)
		name_field_label = QLabel("도구 이름")
		name_field_label.setObjectName("FieldLabel")
		self.document_tool_name_input = QLineEdit()
		self.document_tool_name_input.setObjectName("SettingsInput")
		self.document_tool_name_input.setPlaceholderText("예: Obsidian")
		self.document_tool_name_input.setFixedHeight(38)
		self.document_tool_name_input.returnPressed.connect(self._add_document_tool_from_inputs)
		name_col.addWidget(name_field_label)
		name_col.addWidget(self.document_tool_name_input)

		id_col = QVBoxLayout()
		id_col.setContentsMargins(0, 0, 0, 0)
		id_col.setSpacing(5)
		id_field_label = QLabel("프로세스명 / URL 키워드 (선택)")
		id_field_label.setObjectName("FieldLabel")
		self.document_tool_id_input = QLineEdit()
		self.document_tool_id_input.setObjectName("SettingsInput")
		self.document_tool_id_input.setPlaceholderText("예: obsidian.exe")
		self.document_tool_id_input.setFixedHeight(38)
		self.document_tool_id_input.returnPressed.connect(self._add_document_tool_from_inputs)
		id_col.addWidget(id_field_label)
		id_col.addWidget(self.document_tool_id_input)

		add_tool_button = AppButton("추가")
		add_tool_button.setFixedHeight(38)
		add_tool_button.setMinimumWidth(72)
		add_tool_button.clicked.connect(self._add_document_tool_from_inputs)

		add_layout.addLayout(name_col, 2)
		add_layout.addLayout(id_col, 3)
		add_layout.addWidget(add_tool_button, 0, Qt.AlignBottom)
		layout.addWidget(add_row)

		# --- 액션 버튼 ---
		action_row = QHBoxLayout()
		action_row.setSpacing(8)
		action_row.addStretch(1)
		remove_button = AppButton("선택 삭제", variant="ghost")
		remove_button.clicked.connect(self._remove_selected_document_tool)
		clear_button = AppButton("전체 비우기", variant="ghost")
		clear_button.clicked.connect(self._clear_document_tools)
		save_button = AppButton("도구 설정 저장")
		save_button.clicked.connect(self._save_document_tools_settings)
		action_row.addWidget(remove_button)
		action_row.addWidget(clear_button)
		action_row.addWidget(save_button)
		layout.addLayout(action_row)

		self.document_tools_status = QLabel()
		self.document_tools_status.setObjectName("SettingsStatus")
		self.document_tools_status.setWordWrap(True)
		layout.addWidget(self.document_tools_status)

		self._load_document_tools_settings()
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
		label.setStyleSheet("font-size: 13px; font-weight: 800; color: #0F172A;")
		return label

	def _divider(self) -> QFrame:
		line = QFrame()
		line.setFixedHeight(1)
		line.setStyleSheet("background-color: #E2E8F0; border: none;")
		return line

	def _load_model_settings(self) -> None:
		model_settings = self._settings.get("model", {})
		model_name = model_settings.get("modelName") or model_settings.get("modelId") or "0.8B"
		self._set_selected_model(str(model_name))
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
			model_name = "0.8B"
		self._model_buttons[model_name].setChecked(True)

	def _selected_model(self) -> str:
		for model_name, button in self._model_buttons.items():
			if button.isChecked():
				return model_name
		return "0.8B"

	def _reset_model_settings(self) -> None:
		self._set_selected_model("0.8B")
		self._save_model_settings()

	def _save_model_settings(self) -> None:
		model_name = self._selected_model()
		self._settings["model"] = {
			"modelName": model_name,
		}
		self._update_model_status("모델 설정이 저장되었습니다.")

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

	def _load_document_tools_settings(self) -> None:
		document_tools = self._settings.get("documentTools", {})
		custom_tools = document_tools.get("custom") or []
		self.document_tool_list.clear()
		for tool in custom_tools:
			if not isinstance(tool, dict):
				continue
			self._add_document_tool_item(
				str(tool.get("name") or ""),
				str(tool.get("identifier") or ""),
			)
		self._update_document_tools_status()

	def _add_document_tool_item(self, name: str, identifier: str) -> None:
		name = name.strip()
		if not name:
			return
		identifier = identifier.strip()
		for existing in self._document_tools():
			if (
				existing["name"].lower() == name.lower()
				and existing["identifier"].lower() == identifier.lower()
			):
				return
		display = f"{name}  —  {identifier}" if identifier else name
		item = QListWidgetItem(display)
		item.setData(Qt.UserRole, {"name": name, "identifier": identifier})
		self.document_tool_list.addItem(item)

	def _add_document_tool_from_inputs(self) -> None:
		name = self.document_tool_name_input.text().strip()
		if not name:
			self._update_document_tools_status("도구 이름을 입력하세요.")
			return
		identifier = self.document_tool_id_input.text().strip()
		self._add_document_tool_item(name, identifier)
		self.document_tool_name_input.clear()
		self.document_tool_id_input.clear()
		self.document_tool_name_input.setFocus()
		self._update_document_tools_status()

	def _remove_selected_document_tool(self) -> None:
		for item in self.document_tool_list.selectedItems():
			self.document_tool_list.takeItem(self.document_tool_list.row(item))
		self._update_document_tools_status()

	def _clear_document_tools(self) -> None:
		self.document_tool_list.clear()
		self._save_document_tools_settings()

	def _document_tools(self) -> list[dict[str, str]]:
		tools: list[dict[str, str]] = []
		for index in range(self.document_tool_list.count()):
			data = self.document_tool_list.item(index).data(Qt.UserRole)
			if isinstance(data, dict):
				tools.append(
					{
						"name": str(data.get("name") or ""),
						"identifier": str(data.get("identifier") or ""),
					}
				)
		return tools

	def _save_document_tools_settings(self) -> None:
		custom_tools = self._document_tools()
		self._settings["documentTools"] = {"custom": custom_tools}
		try:
			AgentController().update_document_tools(custom_tools)
		except Exception as e:
			self._update_document_tools_status(f"저장 중 오류가 발생했습니다: {e}")
			return
		self._update_document_tools_status("문서 작업 도구 설정이 저장되었습니다.")

	def _update_document_tools_status(self, prefix: str | None = None) -> None:
		count = self.document_tool_list.count()
		lead = f"{prefix} · " if prefix else ""
		if count:
			self.document_tools_status.setText(f"{lead}{count}개 도구가 추가되어 있습니다.")
		else:
			self.document_tools_status.setText(f"{lead}추가한 문서 작업 도구가 없습니다.")

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
		self._settings["research"] = {
			"sampleCount": self.research_sample_input.value(),
			"planCount": self.research_plan_input.value(),
		}
		self._update_research_method_status("조사 진행 방식 설정이 저장되었습니다.")

	def _update_research_method_status(self, prefix: str | None = None) -> None:
		lead = f"{prefix} · " if prefix else ""
		self.research_method_status.setText(
			f"{lead}최초 샘플링 {self.research_sample_input.value()}개 · "
			f"플랜당 {self.research_plan_input.value()}개"
		)

	def set_default_workspace_by_name(self, _workspace_name: str) -> None:
		return

	def _update_model_status(self, prefix: str) -> None:
		self.model_status.setText(f"{prefix} · {self._selected_model()}")

	def _update_local_folder_status(self, prefix: str | None = None) -> None:
		folder_paths = self._folder_paths()
		lead = f"{prefix} · " if prefix else ""
		if folder_paths:
			self.local_folder_status.setText(f"{lead}{len(folder_paths)}개 폴더 접근 허용")
		else:
			self.local_folder_status.setText(f"{lead}지정된 로컬 접근 폴더가 없습니다.")
