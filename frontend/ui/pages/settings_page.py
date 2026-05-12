from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
	QButtonGroup,
	QFileDialog,
	QHBoxLayout,
	QLabel,
	QListWidget,
	QPushButton,
	QVBoxLayout,
	QWidget,
)

from ...api_common import STATE
from ...components.buttons import AppButton
from ...components.cards import CardWidget


MODEL_OPTIONS = [
	("0.8B", "0.8B"),
	("9B", "9B"),
]


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
			},
		)
		self._settings.setdefault("model", {}).setdefault("modelName", "0.8B")
		self._settings.setdefault("localAccess", {})
		self._model_buttons: dict[str, QPushButton] = {}

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(14)

		root.addWidget(self._build_model_card())
		root.addWidget(self._build_local_access_card())
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
