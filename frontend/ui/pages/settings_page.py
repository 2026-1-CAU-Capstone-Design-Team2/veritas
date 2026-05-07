from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
	QCheckBox,
	QComboBox,
	QDoubleSpinBox,
	QFormLayout,
	QHBoxLayout,
	QLabel,
	QSpinBox,
	QVBoxLayout,
	QWidget,
)

from ...api_common import STATE
from ...components.buttons import AppButton
from ...components.cards import CardWidget


@dataclass(frozen=True)
class WorkspaceOption:
	workspace_id: str
	name: str
	detail: str


class SettingsPage(QWidget):
	defaultWorkspaceChanged = Signal(str)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._workspace_options = [
			WorkspaceOption(item["workspaceId"], item["name"], item["detail"])
			for item in STATE["workspaces"]
		]
		settings = STATE.setdefault(
			"settings",
			{
				"model": {
					"modelId": "veritas-balanced",
					"temperature": 0.2,
					"maxOutputTokens": 1600,
				},
				"defaultWorkspace": {
					"workspaceId": STATE["current_workspace_id"],
					"openOnLaunch": True,
				},
			},
		)
		self._settings = settings

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(14)

		root.addWidget(self._build_model_card())
		root.addWidget(self._build_workspace_card())
		root.addStretch(1)

	def _build_model_card(self) -> CardWidget:
		card = CardWidget("모델 설정")

		subtitle = QLabel("초안 생성, 문서 보조, AI 채팅에서 사용할 기본 모델 동작을 설정합니다.")
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		card.layout.addWidget(subtitle)

		self.model_selector = QComboBox()
		self.model_selector.setObjectName("SettingsInput")
		self.model_selector.addItem("VERITAS Balanced", "veritas-balanced")
		self.model_selector.addItem("VERITAS Fast", "veritas-fast")
		self.model_selector.addItem("VERITAS Deep Review", "veritas-deep-review")

		self.temperature_input = QDoubleSpinBox()
		self.temperature_input.setObjectName("SettingsInput")
		self.temperature_input.setRange(0.0, 1.0)
		self.temperature_input.setSingleStep(0.1)
		self.temperature_input.setDecimals(1)

		self.max_tokens_input = QSpinBox()
		self.max_tokens_input.setObjectName("SettingsInput")
		self.max_tokens_input.setRange(512, 8192)
		self.max_tokens_input.setSingleStep(256)
		self.max_tokens_input.setSuffix(" tokens")

		form = QFormLayout()
		form.setContentsMargins(0, 4, 0, 0)
		form.setHorizontalSpacing(18)
		form.setVerticalSpacing(12)
		form.addRow("기본 모델", self.model_selector)
		form.addRow("응답 창의성", self.temperature_input)
		form.addRow("최대 응답 길이", self.max_tokens_input)
		card.layout.addLayout(form)

		self.model_status = QLabel()
		self.model_status.setObjectName("SettingsStatus")
		self.model_status.setWordWrap(True)
		card.layout.addWidget(self.model_status)

		action_row = QHBoxLayout()
		action_row.addStretch(1)
		reset_button = AppButton("기본값", variant="ghost")
		reset_button.clicked.connect(self._reset_model_settings)
		save_button = AppButton("모델 설정 저장")
		save_button.clicked.connect(self._save_model_settings)
		action_row.addWidget(reset_button)
		action_row.addWidget(save_button)
		card.layout.addLayout(action_row)

		self._load_model_settings()
		return card

	def _build_workspace_card(self) -> CardWidget:
		card = CardWidget("기본 워크스페이스 설정")

		subtitle = QLabel("앱을 시작하거나 새 작업을 열 때 우선 사용할 워크스페이스를 지정합니다.")
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		card.layout.addWidget(subtitle)

		self.workspace_selector = QComboBox()
		self.workspace_selector.setObjectName("SettingsInput")
		for option in self._workspace_options:
			self.workspace_selector.addItem(option.name, option.workspace_id)
		self.workspace_selector.currentIndexChanged.connect(lambda _index: self._update_workspace_summary())

		self.open_on_launch = QCheckBox("시작 시 기본 워크스페이스로 열기")
		self.open_on_launch.setObjectName("SettingsCheckbox")
		self.open_on_launch.setCursor(Qt.PointingHandCursor)
		self.open_on_launch.stateChanged.connect(lambda _state: self._update_workspace_summary())

		form = QFormLayout()
		form.setContentsMargins(0, 4, 0, 0)
		form.setHorizontalSpacing(18)
		form.setVerticalSpacing(12)
		form.addRow("기본 워크스페이스", self.workspace_selector)
		form.addRow("", self.open_on_launch)
		card.layout.addLayout(form)

		self.workspace_summary = QLabel()
		self.workspace_summary.setObjectName("SettingsStatus")
		self.workspace_summary.setWordWrap(True)
		card.layout.addWidget(self.workspace_summary)

		action_row = QHBoxLayout()
		action_row.addStretch(1)
		apply_button = AppButton("기본 워크스페이스 저장")
		apply_button.clicked.connect(self._save_workspace_settings)
		action_row.addWidget(apply_button)
		card.layout.addLayout(action_row)

		self._load_workspace_settings()
		return card

	def _load_model_settings(self) -> None:
		model_settings = self._settings["model"]
		model_id = model_settings["modelId"]
		model_index = self.model_selector.findData(model_id)
		self.model_selector.setCurrentIndex(max(0, model_index))
		self.temperature_input.setValue(float(model_settings["temperature"]))
		self.max_tokens_input.setValue(int(model_settings["maxOutputTokens"]))
		self._update_model_status("현재 모델 설정이 적용되어 있습니다.")

	def _load_workspace_settings(self) -> None:
		workspace_settings = self._settings["defaultWorkspace"]
		workspace_id = workspace_settings["workspaceId"]
		workspace_index = self.workspace_selector.findData(workspace_id)
		self.workspace_selector.setCurrentIndex(max(0, workspace_index))
		self.open_on_launch.setChecked(bool(workspace_settings["openOnLaunch"]))
		self._update_workspace_summary()

	def _reset_model_settings(self) -> None:
		self.model_selector.setCurrentIndex(self.model_selector.findData("veritas-balanced"))
		self.temperature_input.setValue(0.2)
		self.max_tokens_input.setValue(1600)
		self._save_model_settings()

	def _save_model_settings(self) -> None:
		self._settings["model"] = {
			"modelId": self.model_selector.currentData(),
			"modelName": self.model_selector.currentText(),
			"temperature": self.temperature_input.value(),
			"maxOutputTokens": self.max_tokens_input.value(),
		}
		self._update_model_status("모델 설정이 저장되었습니다.")

	def _save_workspace_settings(self) -> None:
		workspace_id = self.workspace_selector.currentData()
		workspace_name = self.workspace_selector.currentText()
		self._settings["defaultWorkspace"] = {
			"workspaceId": workspace_id,
			"workspaceName": workspace_name,
			"openOnLaunch": self.open_on_launch.isChecked(),
		}
		STATE["current_workspace_id"] = workspace_id
		STATE["ui_state"]["workspaceId"] = workspace_id
		STATE["ui_state"]["workspaceName"] = workspace_name
		self.defaultWorkspaceChanged.emit(workspace_name)
		self._update_workspace_summary("기본 워크스페이스가 저장되었습니다.")

	def _update_model_status(self, prefix: str) -> None:
		self.model_status.setText(
			f"{prefix} · {self.model_selector.currentText()} · 창의성 {self.temperature_input.value():.1f} · "
			f"최대 {self.max_tokens_input.value()} tokens"
		)

	def _update_workspace_summary(self, prefix: str | None = None) -> None:
		workspace_id = self.workspace_selector.currentData()
		option = next((item for item in self._workspace_options if item.workspace_id == workspace_id), None)
		if option is None:
			self.workspace_summary.setText("선택 가능한 워크스페이스가 없습니다.")
			return

		lead = f"{prefix} · " if prefix else ""
		open_state = "시작 시 자동 적용" if self.open_on_launch.isChecked() else "필요 시 수동 적용"
		self.workspace_summary.setText(f"{lead}{option.name} · {option.detail} · {open_state}")
