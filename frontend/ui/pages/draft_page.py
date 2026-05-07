from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
	QComboBox,
	QHBoxLayout,
	QLabel,
	QPlainTextEdit,
	QVBoxLayout,
	QWidget,
)

from ...components.buttons import AppButton
from ...components.cards import CardWidget


class DraftPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._workspace_options = [
			(
				"기후 정책 검증 워크스페이스",
				"웹 조사 12건 · 검증 완료 8건 · 초안 적합도 높음",
			),
			(
				"AI 안전성 브리프 워크스페이스",
				"웹 조사 9건 · 검증 완료 7건 · 경영진 보고용",
			),
			(
				"규제 대응 메모 워크스페이스",
				"웹 조사 15건 · 검증 완료 11건 · 안내문/공지문 적합",
			),
		]
		self._selected_workspace_index = 0

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(12)

		header = CardWidget("초안 생성")
		subtitle = QLabel("VERITAS에서 웹 조사와 검증을 마친 워크스페이스를 선택하고, 자연어 요청으로 초안을 생성합니다.")
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		header.layout.addWidget(subtitle)

		workspace_row = QHBoxLayout()
		workspace_row.setSpacing(8)

		self.workspace_selector = QComboBox()
		self.workspace_selector.setObjectName("DraftWorkspaceSelector")
		for name, detail in self._workspace_options:
			self.workspace_selector.addItem(name, detail)
		self.workspace_selector.currentIndexChanged.connect(self._on_workspace_changed)

		self.workspace_label = QLabel()
		self.workspace_label.setObjectName("DraftWorkspaceSummary")
		self.workspace_label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)

		workspace_row.addWidget(self.workspace_selector, 1)
		header.layout.addLayout(workspace_row)
		header.layout.addWidget(self.workspace_label)
		root.addWidget(header)

		input_card = CardWidget("요청 입력")
		input_hint = QLabel("예: 이 워크스페이스 기준으로 고객 보고용 3문단 초안을 작성해줘")
		input_hint.setObjectName("PageSubtitle")
		input_hint.setWordWrap(True)

		self.prompt_edit = QPlainTextEdit()
		self.prompt_edit.setPlaceholderText("어떤 초안을 어떻게 생성할지 자연어로 입력하세요.")
		self.prompt_edit.setMinimumHeight(160)
		self.prompt_edit.setObjectName("DraftPrompt")

		action_row = QHBoxLayout()
		action_row.addStretch(1)

		generate_button = AppButton("초안 생성")
		generate_button.clicked.connect(self._generate_draft)

		action_row.addWidget(generate_button)

		input_card.layout.addWidget(input_hint)
		input_card.layout.addWidget(self.prompt_edit)
		input_card.layout.addLayout(action_row)
		root.addWidget(input_card)

		output_card = CardWidget("초안 결과")
		output_hint = QLabel("아래 결과는 바로 선택해서 복사할 수 있습니다.")
		output_hint.setObjectName("PageSubtitle")
		output_hint.setWordWrap(True)

		self.output = QPlainTextEdit()
		self.output.setReadOnly(True)
		self.output.setMinimumHeight(320)
		self.output.setObjectName("DraftOutput")

		output_actions = QHBoxLayout()
		output_actions.addStretch(1)

		copy_button = AppButton("초안 복사", variant="ghost")
		copy_button.clicked.connect(self._copy_output)

		output_actions.addWidget(copy_button)

		output_card.layout.addWidget(output_hint)
		output_card.layout.addWidget(self.output)
		output_card.layout.addLayout(output_actions)
		root.addWidget(output_card, 1)

		self._on_workspace_changed(0)
		self._generate_draft()

	def _on_workspace_changed(self, index: int) -> None:
		self._selected_workspace_index = index
		name, detail = self._workspace_options[index]
		self.workspace_label.setText(f"선택된 워크스페이스: {name} · {detail}")
		self._generate_draft()

	def _copy_output(self) -> None:
		self.output.selectAll()
		self.output.copy()
		cursor = self.output.textCursor()
		cursor.clearSelection()
		self.output.setTextCursor(cursor)

	def _generate_draft(self) -> None:
		prompt = self.prompt_edit.toPlainText().strip()
		if not prompt:
			prompt = "워크스페이스 맥락을 반영한 보고용 초안을 작성해줘"

		self.output.setPlainText(self._build_draft_text(prompt))

	def _build_draft_text(self, prompt: str) -> str:
		workspace_name, workspace_detail = self._workspace_options[self._selected_workspace_index]
		if any(keyword in prompt for keyword in ["보고", "브리프", "요약"]):
			body = [
				"1. 배경",
				"   - 선택한 워크스페이스의 검증 완료 자료와 핵심 주제를 기준으로 정리합니다.",
				"2. 핵심 내용",
				"   - 현재 맥락에서 중요한 리스크와 기회를 간단히 정리합니다.",
				"3. 권고안",
				"   - 바로 실행 가능한 다음 조치를 항목별로 제안합니다.",
			]
		elif any(keyword in prompt for keyword in ["메일", "안내", "공지"]):
			body = [
				"1. 목적",
				"   - 수신자가 바로 이해할 수 있도록 핵심 메시지를 먼저 제시합니다.",
				"2. 본문",
				"   - 필요한 배경과 요청 사항을 짧고 명확하게 정리합니다.",
				"3. 마무리",
				"   - 확인 요청과 다음 행동을 분명히 남깁니다.",
			]
		else:
			body = [
				"1. 개요",
				"   - 요청한 주제와 워크스페이스 맥락을 연결해 초안을 시작합니다.",
				"2. 본문",
				"   - 핵심 주장, 근거, 예외 사항을 순서대로 정리합니다.",
				"3. 마무리",
				"   - 검토 포인트와 다음 작업을 정리합니다.",
			]

		return (
			f"VERITAS 초안\n\n"
			f"워크스페이스\n{workspace_name}\n{workspace_detail}\n\n"
			f"요청\n{prompt}\n\n"
			f"초안\n"
			f"아래 내용은 바로 복사해 사용할 수 있는 초안 형태입니다.\n\n"
			f"제목: 요청 주제에 맞춘 초안\n"
			f"핵심 요약: 선택한 워크스페이스의 검증 완료 문맥을 반영해 요청 의도를 우선 정리했습니다.\n\n"
			+ "\n".join(body)
			+ "\n\n"
			f"정리\n"
			f"- 필요하면 이 초안을 기반으로 톤, 길이, 대상 독자를 더 세밀하게 조정할 수 있습니다."
		)
