from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
	QButtonGroup,
	QCheckBox,
	QComboBox,
	QFileDialog,
	QGridLayout,
	QHBoxLayout,
	QLabel,
	QLineEdit,
	QListWidget,
	QListWidgetItem,
	QPlainTextEdit,
	QStackedWidget,
	QVBoxLayout,
	QWidget,
)

from ...api_common import current_workspace_id
from ...components.buttons import AppButton
from ...components.cards import CardWidget
from ...components.stepper import WorkflowStepper
from ...controllers import AgentController, JobCategory, get_job_manager

__all__ = ["DraftPage"]


# ----------------------------------------------------------------- 카테고리 데이터
# 대분류 → 소분류 → 기본 섹션 골격. (프런트엔드 보유 — 백엔드 레지스트리로 이전 예정)
CATEGORIES: list[dict] = [
	{
		"key": "report",
		"label": "보고/분석",
		"subtypes": [
			{"key": "weekly", "label": "주간 보고", "sections": ["요약", "주요 진행 사항", "이슈 / 리스크", "다음 주 계획"]},
			{"key": "result", "label": "결과 보고", "sections": ["개요", "추진 배경", "수행 내용", "결과 및 성과", "결론 및 제언"]},
			{"key": "status", "label": "현황 분석", "sections": ["분석 개요", "현황", "문제점", "원인 분석", "개선 방향"]},
		],
	},
	{
		"key": "proposal",
		"label": "제안/기획",
		"subtypes": [
			{"key": "business", "label": "사업 제안서", "sections": ["제안 배경", "제안 내용", "기대 효과", "추진 일정", "예산"]},
			{"key": "plan", "label": "기획안", "sections": ["기획 의도", "목표", "주요 내용", "실행 계획", "기대 효과"]},
			{"key": "marketing", "label": "마케팅 플랜", "sections": ["시장 분석", "타깃", "전략", "채널 / 실행", "성과 지표"]},
		],
	},
	{
		"key": "record",
		"label": "기록/정리",
		"subtypes": [
			{"key": "minutes", "label": "회의록", "sections": ["회의 개요", "참석자", "안건", "논의 내용", "결정 사항", "후속 조치"]},
			{"key": "memo", "label": "업무 메모", "sections": ["목적", "핵심 내용", "참고 사항", "To-Do"]},
			{"key": "research", "label": "리서치 요약", "sections": ["조사 목적", "조사 방법", "주요 발견", "시사점"]},
		],
	},
	{
		"key": "notice",
		"label": "안내/공지",
		"subtypes": [
			{"key": "internal", "label": "사내 공지", "sections": ["제목", "공지 배경", "주요 내용", "유의 사항", "문의처"]},
			{"key": "customer", "label": "고객 안내", "sections": ["인사말", "안내 내용", "적용 일정", "유의 사항", "문의 안내"]},
			{"key": "event", "label": "이벤트 안내", "sections": ["이벤트 개요", "참여 방법", "혜택", "기간", "유의 사항"]},
		],
	},
	{
		"key": "academic",
		"label": "학술/조사",
		"subtypes": [
			{"key": "paper", "label": "조사 보고서", "sections": ["서론", "연구 방법", "결과", "논의", "결론", "참고문헌"]},
			{"key": "review", "label": "리뷰 / 고찰", "sections": ["개요", "배경", "주요 논점", "비교 분석", "결론"]},
			{"key": "abstract", "label": "초록", "sections": ["연구 목적", "방법", "결과", "결론"]},
		],
	},
]

TONES = ["격식체", "중립", "캐주얼"]
LENGTHS = ["짧게", "보통", "길게"]

_TONE_GUIDE = {"격식체": "격식 있고 공식적인 문체", "중립": "중립적이고 명료한 문체", "캐주얼": "부드럽고 친근한 문체"}
_LENGTH_GUIDE = {"짧게": "핵심 위주로 간결하게", "보통": "보통 수준의 분량으로", "길게": "충분히 상세하게"}

FILE_STEPS = ["소스", "양식 분석", "목차 확정", "초안"]
CUSTOM_STEPS = ["소스", "대분류", "소분류", "구성", "목차 확정", "초안"]

FILE_FILTER = "문서 (*.docx *.pdf *.md *.txt *.pptx *.ppt *.hwp *.hwpx);;모든 파일 (*.*)"
_TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".log"}


class DraftPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._workspace_id = current_workspace_id()
		self._controller = AgentController()

		# -- 위저드 상태 --------------------------------------------------------
		self._source: str | None = None  # "file" | "custom"
		self._category: dict | None = None
		self._subtype: dict | None = None
		self._uploaded_path: Path | None = None
		self._section_checks: list[QCheckBox] = []
		self._last_draft_text = ""

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(12)

		root.addWidget(self._build_header())

		self._stepper_holder = QWidget()
		self._stepper_holder_layout = QVBoxLayout(self._stepper_holder)
		self._stepper_holder_layout.setContentsMargins(0, 0, 0, 0)
		self._stepper_holder.setVisible(False)
		root.addWidget(self._stepper_holder)

		self.stack = QStackedWidget()
		self._idx: dict[str, int] = {}
		self._add_step("source", self._build_source_page())
		self._add_step("upload", self._build_upload_page())
		self._add_step("category", self._build_category_page())
		self._add_step("subtype", self._build_subtype_page())
		self._add_step("customize", self._build_customize_page())
		self._add_step("outline", self._build_outline_page())
		self._add_step("result", self._build_result_page())
		root.addWidget(self.stack, 1)

		get_job_manager().busy_changed.connect(self._sync_busy_state)
		self._show_source()
		self._sync_busy_state()

	# ------------------------------------------------------------------ 헤더
	def _build_header(self) -> QWidget:
		header = CardWidget("초안 생성")
		subtitle = QLabel("양식 파일이 있으면 업로드해 맞추고, 없으면 카테고리를 따라 구성을 만들어 초안을 생성합니다.")
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		header.layout.addWidget(subtitle)

		row = QHBoxLayout()
		self.workspace_label = QLabel(f"현재 워크스페이스: {self._workspace_id}")
		self.workspace_label.setObjectName("DraftWorkspaceSummary")
		self.workspace_label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
		row.addWidget(self.workspace_label, 1)

		self.restart_button = AppButton("처음부터", variant="ghost")
		self.restart_button.clicked.connect(self._reset)
		row.addWidget(self.restart_button, 0)
		header.layout.addLayout(row)
		return header

	# --------------------------------------------------------------- 스텝 0: 소스
	def _build_source_page(self) -> QWidget:
		page = QWidget()
		layout = QVBoxLayout(page)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)

		card = CardWidget("어떻게 시작할까요?")
		hint = QLabel("작성할 초안의 시작 방식을 선택하세요.")
		hint.setObjectName("PageSubtitle")
		hint.setWordWrap(True)
		card.layout.addWidget(hint)

		choices = QHBoxLayout()
		choices.setSpacing(12)
		choices.addWidget(self._source_choice(
			"양식 파일 사용",
			"기존 양식(docx·pdf·md 등)을 업로드하면\n그 구조에 맞춰 초안을 만듭니다.",
			lambda: self._choose_source("file"),
		))
		choices.addWidget(self._source_choice(
			"직접 구성",
			"카테고리를 따라 단계별로\n문서 구성을 직접 만듭니다.",
			lambda: self._choose_source("custom"),
		))
		card.layout.addLayout(choices)
		layout.addWidget(card)
		layout.addStretch(1)
		return page

	def _source_choice(self, title: str, desc: str, slot) -> QWidget:
		box = CardWidget()
		box.setObjectName("StatTile")
		title_label = QLabel(title)
		title_label.setObjectName("CardPrimary")
		desc_label = QLabel(desc)
		desc_label.setObjectName("CardSecondary")
		desc_label.setWordWrap(True)
		button = AppButton("선택")
		button.clicked.connect(slot)
		box.layout.addWidget(title_label)
		box.layout.addWidget(desc_label)
		box.layout.addStretch(1)
		box.layout.addWidget(button)
		return box

	# ----------------------------------------------------- 스텝 A: 양식 파일 업로드
	def _build_upload_page(self) -> QWidget:
		page = QWidget()
		layout = QVBoxLayout(page)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)

		card = CardWidget("양식 파일")
		hint = QLabel("초안의 형식을 가져올 양식 파일을 선택하세요. (docx · pdf · md · txt · pptx · hwp)")
		hint.setObjectName("PageSubtitle")
		hint.setWordWrap(True)
		card.layout.addWidget(hint)

		pick_row = QHBoxLayout()
		self.pick_button = AppButton("파일 선택", variant="ghost")
		self.pick_button.clicked.connect(self._pick_file)
		self.file_label = QLabel("선택된 파일이 없습니다.")
		self.file_label.setObjectName("CardSecondary")
		self.file_label.setWordWrap(True)
		pick_row.addWidget(self.pick_button, 0)
		pick_row.addWidget(self.file_label, 1)
		card.layout.addLayout(pick_row)

		self.analyze_note = QLabel(
			"· .md / .txt 양식은 제목 구조를 자동으로 읽어 목차로 채웁니다.\n"
			"· 그 외 포맷은 기본 골격을 제시하니 목차 단계에서 직접 편집하세요."
		)
		self.analyze_note.setObjectName("CardFooter")
		self.analyze_note.setWordWrap(True)
		card.layout.addWidget(self.analyze_note)
		layout.addWidget(card)
		layout.addStretch(1)

		nav = QHBoxLayout()
		back = AppButton("이전", variant="ghost")
		back.clicked.connect(self._show_source)
		self.analyze_button = AppButton("양식 분석 → 목차")
		self.analyze_button.setEnabled(False)
		self.analyze_button.clicked.connect(self._analyze_format)
		nav.addWidget(back, 0)
		nav.addStretch(1)
		nav.addWidget(self.analyze_button, 0)
		layout.addLayout(nav)
		return page

	# ------------------------------------------------------ 스텝 B①: 대분류
	def _build_category_page(self) -> QWidget:
		page = QWidget()
		layout = QVBoxLayout(page)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)

		card = CardWidget("① 대분류 선택")
		hint = QLabel("작성할 문서의 큰 갈래를 고르세요.")
		hint.setObjectName("PageSubtitle")
		hint.setWordWrap(True)
		card.layout.addWidget(hint)

		grid = QGridLayout()
		grid.setSpacing(8)
		self._category_group = QButtonGroup(self)
		self._category_group.setExclusive(True)
		options = [(c["key"], c["label"]) for c in CATEGORIES]
		self._populate_chip_grid(grid, options, self._category_group, self._on_category_clicked)
		card.layout.addLayout(grid)
		layout.addWidget(card)
		layout.addStretch(1)

		nav = QHBoxLayout()
		back = AppButton("이전", variant="ghost")
		back.clicked.connect(self._show_source)
		self.category_next = AppButton("다음")
		self.category_next.setEnabled(False)
		self.category_next.clicked.connect(self._show_subtype)
		nav.addWidget(back, 0)
		nav.addStretch(1)
		nav.addWidget(self.category_next, 0)
		layout.addLayout(nav)
		return page

	# ------------------------------------------------------ 스텝 B②: 소분류
	def _build_subtype_page(self) -> QWidget:
		page = QWidget()
		layout = QVBoxLayout(page)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)

		self._subtype_card = CardWidget("② 세부 유형 선택")
		hint = QLabel("선택한 대분류 안에서 구체적인 문서 유형을 고르세요.")
		hint.setObjectName("PageSubtitle")
		hint.setWordWrap(True)
		self._subtype_card.layout.addWidget(hint)

		self._subtype_grid = QGridLayout()
		self._subtype_grid.setSpacing(8)
		self._subtype_group = QButtonGroup(self)
		self._subtype_group.setExclusive(True)
		self._subtype_card.layout.addLayout(self._subtype_grid)
		layout.addWidget(self._subtype_card)
		layout.addStretch(1)

		nav = QHBoxLayout()
		back = AppButton("이전", variant="ghost")
		back.clicked.connect(self._show_category)
		self.subtype_next = AppButton("다음")
		self.subtype_next.setEnabled(False)
		self.subtype_next.clicked.connect(self._enter_customize)
		nav.addWidget(back, 0)
		nav.addStretch(1)
		nav.addWidget(self.subtype_next, 0)
		layout.addLayout(nav)
		return page

	# ------------------------------------------------------ 스텝 B③: 구성 커스텀
	def _build_customize_page(self) -> QWidget:
		page = QWidget()
		layout = QVBoxLayout(page)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)

		sections_card = CardWidget("③ 구성 커스텀 — 포함할 섹션")
		shint = QLabel("기본 섹션에서 포함할 항목을 선택하고, 필요하면 직접 추가하세요.")
		shint.setObjectName("PageSubtitle")
		shint.setWordWrap(True)
		sections_card.layout.addWidget(shint)

		self._sections_box = QVBoxLayout()
		self._sections_box.setSpacing(4)
		sections_card.layout.addLayout(self._sections_box)

		add_row = QHBoxLayout()
		self.section_input = QLineEdit()
		self.section_input.setObjectName("SettingsInput")
		self.section_input.setPlaceholderText("추가할 섹션 이름")
		self.section_input.returnPressed.connect(self._add_custom_section)
		add_section_btn = AppButton("＋ 섹션 추가", variant="ghost")
		add_section_btn.clicked.connect(self._add_custom_section)
		add_row.addWidget(self.section_input, 1)
		add_row.addWidget(add_section_btn, 0)
		sections_card.layout.addLayout(add_row)
		layout.addWidget(sections_card)

		opt_card = CardWidget("작성 옵션")
		opt_grid = QGridLayout()
		opt_grid.setHorizontalSpacing(12)
		opt_grid.setVerticalSpacing(8)

		opt_grid.addWidget(self._field_label("톤"), 0, 0)
		self.tone_combo = QComboBox()
		self.tone_combo.setObjectName("SettingsInput")
		self.tone_combo.addItems(TONES)
		self.tone_combo.setCurrentText("중립")
		opt_grid.addWidget(self.tone_combo, 0, 1)

		opt_grid.addWidget(self._field_label("분량"), 0, 2)
		self.length_combo = QComboBox()
		self.length_combo.setObjectName("SettingsInput")
		self.length_combo.addItems(LENGTHS)
		self.length_combo.setCurrentText("보통")
		opt_grid.addWidget(self.length_combo, 0, 3)

		opt_grid.addWidget(self._field_label("대상 독자"), 1, 0)
		self.audience_input = QLineEdit()
		self.audience_input.setObjectName("SettingsInput")
		self.audience_input.setPlaceholderText("예: 팀 리더 / 고객사 담당자")
		opt_grid.addWidget(self.audience_input, 1, 1, 1, 3)
		opt_card.layout.addLayout(opt_grid)

		opt_card.layout.addWidget(self._field_label("핵심 내용 / 추가 지시"))
		self.keypoints_input = QPlainTextEdit()
		self.keypoints_input.setObjectName("DocEditor")
		self.keypoints_input.setPlaceholderText("초안에 꼭 담겨야 할 핵심 내용을 적어주세요.")
		self.keypoints_input.setMinimumHeight(90)
		opt_card.layout.addWidget(self.keypoints_input)
		layout.addWidget(opt_card)
		layout.addStretch(1)

		nav = QHBoxLayout()
		back = AppButton("이전", variant="ghost")
		back.clicked.connect(self._show_subtype)
		self.make_outline_button = AppButton("목차 만들기 →")
		self.make_outline_button.clicked.connect(self._build_outline_from_customize)
		nav.addWidget(back, 0)
		nav.addStretch(1)
		nav.addWidget(self.make_outline_button, 0)
		layout.addLayout(nav)
		return page

	# ------------------------------------------------------ 공통 ④: 목차 확정
	def _build_outline_page(self) -> QWidget:
		page = QWidget()
		layout = QVBoxLayout(page)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)

		card = CardWidget("④ 목차 확정")
		hint = QLabel("생성될 초안의 구성입니다. 항목을 더블클릭해 수정하거나 추가·삭제·순서를 조정하세요.")
		hint.setObjectName("PageSubtitle")
		hint.setWordWrap(True)
		card.layout.addWidget(hint)

		body = QHBoxLayout()
		self.outline_list = QListWidget()
		self.outline_list.setObjectName("SettingsFolderList")
		self.outline_list.setMinimumHeight(220)
		body.addWidget(self.outline_list, 1)

		side = QVBoxLayout()
		side.setSpacing(6)
		for label, slot in (
			("위로", self._outline_up),
			("아래로", self._outline_down),
			("삭제", self._outline_remove),
		):
			button = AppButton(label, variant="ghost")
			button.clicked.connect(slot)
			side.addWidget(button)
		side.addStretch(1)
		body.addLayout(side, 0)
		card.layout.addLayout(body)

		add_row = QHBoxLayout()
		self.outline_input = QLineEdit()
		self.outline_input.setObjectName("SettingsInput")
		self.outline_input.setPlaceholderText("추가할 목차 항목")
		self.outline_input.returnPressed.connect(self._outline_add)
		add_btn = AppButton("＋ 항목 추가", variant="ghost")
		add_btn.clicked.connect(self._outline_add)
		add_row.addWidget(self.outline_input, 1)
		add_row.addWidget(add_btn, 0)
		card.layout.addLayout(add_row)
		layout.addWidget(card)
		layout.addStretch(1)

		nav = QHBoxLayout()
		self.outline_back = AppButton("이전", variant="ghost")
		self.outline_back.clicked.connect(self._outline_back)
		self.generate_button = AppButton("이 구성으로 초안 생성")
		self.generate_button.clicked.connect(self._generate_draft)
		nav.addWidget(self.outline_back, 0)
		nav.addStretch(1)
		nav.addWidget(self.generate_button, 0)
		layout.addLayout(nav)
		return page

	# ----------------------------------------------------------- 공통: 결과
	def _build_result_page(self) -> QWidget:
		page = QWidget()
		layout = QVBoxLayout(page)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)

		card = CardWidget("초안 결과")
		hint = QLabel("agent가 생성한 초안입니다.")
		hint.setObjectName("PageSubtitle")
		hint.setWordWrap(True)
		card.layout.addWidget(hint)

		self.output = QPlainTextEdit()
		self.output.setReadOnly(True)
		self.output.setObjectName("DraftOutput")
		self.output.setMinimumHeight(320)
		card.layout.addWidget(self.output)

		actions = QHBoxLayout()
		self.copy_button = AppButton("초안 복사", variant="ghost")
		self.copy_button.clicked.connect(self._copy_output)
		self.editor_button = AppButton("에디터에서 이어쓰기")
		self.editor_button.setEnabled(False)
		self.editor_button.setToolTip("백엔드 연동 후 활성화됩니다 — 생성된 초안을 에디터로 전달합니다.")
		actions.addWidget(self.copy_button, 0)
		actions.addStretch(1)
		actions.addWidget(self.editor_button, 0)
		card.layout.addLayout(actions)
		layout.addWidget(card, 1)

		nav = QHBoxLayout()
		back = AppButton("목차로 돌아가기", variant="ghost")
		back.clicked.connect(self._show_outline)
		nav.addWidget(back, 0)
		nav.addStretch(1)
		layout.addLayout(nav)
		return page

	# ------------------------------------------------------------------ 헬퍼
	def _add_step(self, key: str, widget: QWidget) -> None:
		self._idx[key] = self.stack.addWidget(widget)

	def _field_label(self, text: str) -> QLabel:
		label = QLabel(text)
		label.setObjectName("FieldLabel")
		return label

	def _populate_chip_grid(self, grid: QGridLayout, options, group: QButtonGroup, slot, columns: int = 3) -> None:
		for i, (key, label) in enumerate(options):
			chip = AppButton(label, variant="filter")
			chip.setCheckable(True)
			chip.setProperty("optionKey", key)
			group.addButton(chip)
			grid.addWidget(chip, i // columns, i % columns)
		group.buttonClicked.connect(slot)

	def _clear_layout(self, layout) -> None:
		while layout.count():
			item = layout.takeAt(0)
			widget = item.widget()
			if widget is not None:
				widget.setParent(None)
				widget.deleteLater()

	def _set_steps(self, steps: list[str], active: int) -> None:
		self._clear_layout(self._stepper_holder_layout)
		stepper = WorkflowStepper(steps)
		stepper.set_current_step(active)
		self._stepper_holder_layout.addWidget(stepper)
		self._stepper_holder.setVisible(True)

	# --------------------------------------------------------------- 네비게이션
	def _show_source(self) -> None:
		self._stepper_holder.setVisible(False)
		self.stack.setCurrentIndex(self._idx["source"])

	def _show_upload(self) -> None:
		self._set_steps(FILE_STEPS, 1)
		self.stack.setCurrentIndex(self._idx["upload"])

	def _show_category(self) -> None:
		self._set_steps(CUSTOM_STEPS, 1)
		self.stack.setCurrentIndex(self._idx["category"])

	def _show_subtype(self) -> None:
		if self._category is None:
			return
		self._rebuild_subtypes()
		self._set_steps(CUSTOM_STEPS, 2)
		self.stack.setCurrentIndex(self._idx["subtype"])

	def _show_customize(self) -> None:
		self._set_steps(CUSTOM_STEPS, 3)
		self.stack.setCurrentIndex(self._idx["customize"])

	def _show_outline(self) -> None:
		if self._source == "file":
			self._set_steps(FILE_STEPS, 2)
		else:
			self._set_steps(CUSTOM_STEPS, 4)
		self.stack.setCurrentIndex(self._idx["outline"])

	def _show_result(self) -> None:
		if self._source == "file":
			self._set_steps(FILE_STEPS, 3)
		else:
			self._set_steps(CUSTOM_STEPS, 5)
		self.stack.setCurrentIndex(self._idx["result"])

	def _outline_back(self) -> None:
		if self._source == "file":
			self._show_upload()
		else:
			self._show_customize()

	# ------------------------------------------------------------------ 액션
	def _choose_source(self, source: str) -> None:
		self._source = source
		if source == "file":
			self._show_upload()
		else:
			self._show_category()

	def _pick_file(self) -> None:
		path_str, _ = QFileDialog.getOpenFileName(self, "양식 파일 선택", "", FILE_FILTER)
		if not path_str:
			return
		self._uploaded_path = Path(path_str)
		self.file_label.setText(self._uploaded_path.name)
		self.analyze_button.setEnabled(True)

	def _analyze_format(self) -> None:
		"""양식 파일에서 목차 골격을 도출. .md/.txt는 헤딩 파싱, 그 외는 기본 골격."""
		sections: list[str] = []
		path = self._uploaded_path
		if path is not None and path.suffix.lower() in _TEXT_SUFFIXES:
			try:
				text = path.read_text(encoding="utf-8", errors="ignore")
				sections = self._parse_headings(text)
			except Exception:
				sections = []
		if not sections:
			sections = ["제목", "개요", "본문", "결론"]
		self._set_outline(sections)
		self._show_outline()

	@staticmethod
	def _parse_headings(text: str) -> list[str]:
		sections: list[str] = []
		for line in text.splitlines():
			stripped = line.strip()
			heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
			if heading:
				title = heading.group(1).strip()
			else:
				numbered = re.match(r"^(\d+(?:\.\d+)*)[.)]\s+(.+)$", stripped)
				title = numbered.group(2).strip() if numbered else ""
			if title:
				sections.append(title)
		# 중복 제거(순서 유지) 후 상한.
		seen: set[str] = set()
		unique = [s for s in sections if not (s in seen or seen.add(s))]
		return unique[:30]

	def _on_category_clicked(self, button) -> None:
		key = button.property("optionKey")
		self._category = next((c for c in CATEGORIES if c["key"] == key), None)
		self._subtype = None
		self.category_next.setEnabled(self._category is not None)

	def _rebuild_subtypes(self) -> None:
		self._clear_layout(self._subtype_grid)
		self._subtype_group = QButtonGroup(self)
		self._subtype_group.setExclusive(True)
		self.subtype_next.setEnabled(False)
		options = [(s["key"], s["label"]) for s in (self._category["subtypes"] if self._category else [])]
		self._populate_chip_grid(self._subtype_grid, options, self._subtype_group, self._on_subtype_clicked)

	def _on_subtype_clicked(self, button) -> None:
		key = button.property("optionKey")
		subtypes = self._category["subtypes"] if self._category else []
		self._subtype = next((s for s in subtypes if s["key"] == key), None)
		self.subtype_next.setEnabled(self._subtype is not None)

	def _enter_customize(self) -> None:
		if self._subtype is None:
			return
		self._clear_layout(self._sections_box)
		self._section_checks = []
		for name in self._subtype.get("sections", []):
			self._add_section_checkbox(name, checked=True)
		self._show_customize()

	def _add_section_checkbox(self, name: str, checked: bool) -> None:
		checkbox = QCheckBox(name)
		checkbox.setChecked(checked)
		self._section_checks.append(checkbox)
		self._sections_box.addWidget(checkbox)

	def _add_custom_section(self) -> None:
		name = self.section_input.text().strip()
		if not name:
			return
		self._add_section_checkbox(name, checked=True)
		self.section_input.clear()

	def _build_outline_from_customize(self) -> None:
		sections = [cb.text() for cb in self._section_checks if cb.isChecked()]
		if not sections:
			sections = ["서론", "본문", "결론"]
		self._set_outline(sections)
		self._show_outline()

	# -- 목차 리스트 조작 -----------------------------------------------------
	def _set_outline(self, sections: list[str]) -> None:
		self.outline_list.clear()
		for name in sections:
			self._append_outline_item(name)

	def _append_outline_item(self, name: str) -> None:
		item = QListWidgetItem(name)
		item.setFlags(item.flags() | Qt.ItemIsEditable)
		self.outline_list.addItem(item)

	def _outline_add(self) -> None:
		name = self.outline_input.text().strip()
		if not name:
			return
		self._append_outline_item(name)
		self.outline_input.clear()

	def _outline_remove(self) -> None:
		row = self.outline_list.currentRow()
		if row >= 0:
			self.outline_list.takeItem(row)

	def _outline_up(self) -> None:
		self._move_outline(-1)

	def _outline_down(self) -> None:
		self._move_outline(1)

	def _move_outline(self, delta: int) -> None:
		row = self.outline_list.currentRow()
		target = row + delta
		if row < 0 or target < 0 or target >= self.outline_list.count():
			return
		item = self.outline_list.takeItem(row)
		self.outline_list.insertItem(target, item)
		self.outline_list.setCurrentRow(target)

	def _outline_sections(self) -> list[str]:
		return [self.outline_list.item(i).text().strip() for i in range(self.outline_list.count()) if self.outline_list.item(i).text().strip()]

	# -- 생성 -----------------------------------------------------------------
	def _compose_prompt(self) -> str:
		sections = self._outline_sections()
		if self._source == "file":
			doc_type = f"업로드 양식 기반 ({self._uploaded_path.name})" if self._uploaded_path else "업로드 양식 기반"
			tone = "중립"
			length = "보통"
			audience = ""
			key_points = ""
		else:
			category = self._category["label"] if self._category else ""
			subtype = self._subtype["label"] if self._subtype else ""
			doc_type = f"{category} > {subtype}".strip(" >")
			tone = self.tone_combo.currentText()
			length = self.length_combo.currentText()
			audience = self.audience_input.text().strip()
			key_points = self.keypoints_input.toPlainText().strip()

		outline_text = "\n".join(f"{i}. {name}" for i, name in enumerate(sections, start=1))
		lines = [
			"다음 구성을 따라 바로 사용할 수 있는 한국어 초안을 작성해 주세요.",
			"",
			f"[문서 유형] {doc_type}",
			f"[톤] {_TONE_GUIDE.get(tone, tone)}",
			f"[분량] {_LENGTH_GUIDE.get(length, length)}",
		]
		if audience:
			lines.append(f"[대상 독자] {audience}")
		if key_points:
			lines.append("[핵심 내용]")
			lines.append(key_points)
		lines += [
			"",
			"[목차]",
			outline_text,
			"",
			"각 목차 항목을 마크다운 제목(##)으로 두고 그 아래에 내용을 채워 주세요.",
		]
		return "\n".join(lines)

	def _generate_draft(self) -> None:
		if not self._outline_sections():
			self.output.setPlainText("목차에 항목을 1개 이상 추가하세요.")
			self._show_result()
			return
		prompt = self._compose_prompt()
		started = get_job_manager().submit(
			JobCategory.DRAFT,
			self._controller.generate_draft,
			self._workspace_id,
			prompt,
			on_success=self._on_draft_generated,
			on_error=self._on_draft_failed,
		)
		if not started:
			return
		self.output.setPlainText("agent가 초안을 생성하는 중입니다...")
		self._show_result()

	def _on_draft_generated(self, response) -> None:
		if isinstance(response, dict):
			text = str(response.get("content") or "")
		else:
			text = str(response or "")
		self._last_draft_text = text
		self.output.setPlainText(text)

	def _on_draft_failed(self, message: str) -> None:
		self.output.setPlainText(f"API 요청 실패: {message}")

	def _copy_output(self) -> None:
		self.output.selectAll()
		self.output.copy()
		cursor = self.output.textCursor()
		cursor.clearSelection()
		self.output.setTextCursor(cursor)

	def _reset(self) -> None:
		self._source = None
		self._category = None
		self._subtype = None
		self._uploaded_path = None
		self._section_checks = []
		self._last_draft_text = ""
		self.file_label.setText("선택된 파일이 없습니다.")
		self.analyze_button.setEnabled(False)
		self.category_next.setEnabled(False)
		self.subtype_next.setEnabled(False)
		if self._category_group.checkedButton() is not None:
			self._category_group.setExclusive(False)
			self._category_group.checkedButton().setChecked(False)
			self._category_group.setExclusive(True)
		self.outline_list.clear()
		self.output.clear()
		self._show_source()

	# ------------------------------------------------------------------ 외부 API
	def set_workspace_by_name(self, workspace_name: str) -> None:
		self.workspace_label.setText(f"현재 워크스페이스: {workspace_name or self._workspace_id}")

	def _sync_busy_state(self) -> None:
		blocked = get_job_manager().is_blocked(JobCategory.DRAFT)
		self.generate_button.setEnabled(not blocked)
