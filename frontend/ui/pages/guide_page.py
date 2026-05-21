from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...components.badges import Badge
from ...components.cards import CardWidget

# Page-scoped styling for the few elements the global stylesheet doesn't cover
# (step number chips, flow chips/arrows, note boxes). Cards, titles, and body
# text reuse the global object names (CardWidget / CardTitle / CardSecondary).
_PAGE_QSS = """
QLabel#GuideSectionTitle {
	color: #0F172A;
	font-size: 15px;
	font-weight: 800;
}
QLabel#GuideStepNum {
	background-color: #EEF2FF;
	color: #3730A3;
	border: 1px solid #C7D2FE;
	border-radius: 10px;
	font-size: 11px;
	font-weight: 800;
}
QLabel#GuideStepText {
	color: #1F2937;
	font-size: 13px;
	font-weight: 600;
}
QLabel#GuideFlowChip {
	background-color: #EEF2FF;
	color: #3730A3;
	border: 1px solid #C7D2FE;
	border-radius: 13px;
	padding: 6px 14px;
	font-size: 12px;
	font-weight: 800;
}
QLabel#GuideFlowArrow {
	color: #94A3B8;
	font-size: 15px;
	font-weight: 800;
}
QLabel#GuideNote {
	background-color: #F8FAFC;
	border: 1px solid #E2E8F0;
	border-radius: 10px;
	color: #475569;
	padding: 9px 11px;
	font-size: 12px;
	font-weight: 700;
}
QLabel#GuideBullet {
	color: #1F2937;
	font-size: 13px;
	font-weight: 600;
}
"""

_INTRO_TEXT = (
	"내 컴퓨터에서 동작하는 AI 리서치·문서 작성 도우미입니다. 조사 주제만 입력하면 "
	"웹에서 자료를 모아 요약하고, 신뢰도를 검증한 뒤, 그 내용으로 문서 초안까지 만들어 줍니다."
)

_FLOW_STEPS = ["조사", "요약", "검증", "초안", "피드백"]

_WORKSPACE_BULLETS = [
	"새 주제로 조사를 시작하면 워크스페이스가 자동으로 새로 만들어집니다.",
	"조사·요약·검증·채팅 기록이 워크스페이스별로 따로 저장됩니다.",
	"이전 작업으로 돌아가려면 사이드바 맨 아래 <b>워크스페이스 전환</b>을 누르세요.",
]

_FEATURE_SECTIONS = [
	{
		"title": "① 조사 — 주제 넣고 자료 모으기",
		"purpose": "가장 먼저 하는 단계예요. 알아보고 싶은 주제를 입력하면 AI가 웹에서 자료를 모아 정리합니다.",
		"steps": [
			"<b>조사 내용 입력</b> 칸에 주제를 문장으로 적습니다. (예: 2026년 AI 규제 동향을 산업별로 조사해줘)",
			"필요하면 <b>레퍼런스 사이트</b>에 꼭 참고할 URL을 추가합니다.",
			"<b>최대 조사 문서 수</b>를 조절 버튼으로 정합니다. (기본 15개)",
			"<b>조사 실행</b>을 누르면 자료가 실시간으로 한 줄씩 쌓입니다.",
		],
		"tip": "다 끝나면 <b>이 보고서로 글쓰기</b> 버튼으로 바로 문서 작성으로 넘어갈 수 있어요.",
	},
	{
		"title": "② 요약 — 완성된 보고서 읽기",
		"purpose": "조사로 모은 자료를 하나로 합친 최종 보고서를 보기 좋게 보여줍니다.",
		"steps": [
			"왼쪽 메뉴에서 <b>요약</b>을 누르면 보고서가 자동으로 나타납니다.",
			"스크롤하며 정리된 내용을 확인합니다.",
		],
		"tip": "보고서가 비어 있으면 먼저 <b>조사</b>를 실행해야 합니다.",
	},
	{
		"title": "③ 검증 — 자료가 믿을 만한지 확인",
		"purpose": "모은 자료의 신뢰도와 보고서 구성을 자동으로 점검합니다.",
		"steps": [
			"<b>검증 시작</b>을 누릅니다. (이미 했다면 <b>재검증</b>)",
			"끝나면 자료 수와 신뢰도 분포(높음·중간·낮음)가 요약됩니다.",
			"각 자료의 <b>상세 보기</b>로 신뢰도 근거를 확인합니다.",
		],
		"tip": None,
	},
	{
		"title": "④ 초안 — 문서 초안 자동 생성",
		"purpose": "조사 내용을 바탕으로 문서의 첫 골격을 만들어 줍니다. 두 가지 방식 중 고릅니다.",
		"steps": [
			"<b>양식 파일 사용</b>: 가진 양식(.md·.txt)을 올리면 제목 구조를 읽어 목차를 채웁니다.",
			"<b>직접 구성</b>: 문서 종류와 톤·분량을 고르고 목차를 정리합니다.",
			"생성된 초안은 <b>초안 복사</b>로 가져가거나 <b>에디터에서 이어쓰기</b>로 넘어갑니다.",
		],
		"tip": None,
	},
	{
		"title": "⑤ 에디터 — 본격적으로 글쓰기",
		"purpose": "문서를 직접 작성하고 다듬는 작업 창입니다. 화면 위 글쓰기 버튼이나 조사·초안 화면에서 열 수 있어요.",
		"steps": [
			"가운데 본문 영역에 글을 씁니다. (글자·단어 수가 아래에 표시됩니다)",
			"문장을 선택하고 <b>존댓말로·자연스럽게·짧게·근거 추가</b> 같은 버튼으로 다듬습니다.",
			"결과를 <b>본문에 대치</b>로 교체하거나 <b>복사</b>합니다.",
		],
		"tip": None,
	},
	{
		"title": "⑥ 문서 보조 (AI 보조창) — 실시간 도움",
		"purpose": "글을 쓰는 동안 화면에 떠서 수정 제안을 실시간으로 보여주는 작은 창입니다.",
		"steps": [
			"화면 위 <b>AI 보조창</b> 버튼(또는 Ctrl+Shift+A)으로 엽니다.",
			"<b>실시간 수정 결과</b>에 제안 카드가 쌓이고, 각 카드의 <b>복사</b>로 바로 활용합니다.",
		],
		"tip": None,
	},
	{
		"title": "⑦ 채팅 — 자료에 대해 질문하기",
		"purpose": "모아 둔 자료를 바탕으로 AI와 대화하며 궁금한 점을 물어봅니다.",
		"steps": [
			"아래 입력칸에 질문을 적고 <b>전송</b>(또는 Enter)을 누릅니다.",
			"모드 버튼으로 <b>채팅</b>(자료 기반 답변)과 <b>조사</b>(새로 조사)를 바꿉니다.",
		],
		"tip": "예: 이 문단 자연스러워? / 근거가 부족한 부분 찾아줘",
	},
	{
		"title": "⑧ 피드백 — 내 문서 점검받기",
		"purpose": "이미 가진 문서 파일을 올려 자동 평가를 받습니다.",
		"steps": [
			"<b>문서 업로드</b>로 파일을 고릅니다. (txt·md·pdf·docx·pptx·hwp 지원)",
			"올리면 자동 분석되고, 목록에서 파일을 누르면 결과가 나타납니다.",
			"결과는 <b>주요 피드백</b>(문제점)과 <b>개선 제안</b>으로 정리됩니다.",
		],
		"tip": None,
	},
	{
		"title": "⑨ 대시보드 · 설정",
		"purpose": "작업 현황을 한눈에 보거나, AI 모델과 참고 폴더 등을 설정합니다.",
		"steps": [
			"<b>대시보드</b>: 처리 문서 수, 검증 완료 수, 최근 작업 워크스페이스를 봅니다.",
			"<b>설정 → 모델 설정</b>: 작을수록 빠르고, 클수록 똑똑한 모델을 고릅니다.",
			"<b>설정 → 로컬 접근 폴더</b>: AI가 참고할 내 폴더를 추가합니다.",
		],
		"tip": None,
	},
]

_TIPS_BULLETS = [
	"<b>버튼이 회색이고 안 눌려요</b> — 다른 작업이 진행 중이면 잠깁니다. 끝나면 다시 활성화됩니다.",
	"<b>요약·검증 화면이 비어 있어요</b> — 그 워크스페이스에서 조사를 먼저 실행하세요.",
	"<b>처음이라면</b> 조사 → 요약만 해보고, 익숙해지면 검증·초안·피드백을 더해 가세요.",
]


class GuidePage(QWidget):
	"""Static, card-based usage guide. Reuses the app's CardWidget aesthetic so
	the page matches the rest of the UI; content is data-driven from the module
	tables above so it is easy to edit."""

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setStyleSheet(_PAGE_QSS)

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(14)

		root.addWidget(self._intro_card())
		root.addWidget(self._flow_card())
		root.addWidget(self._workspace_card())
		for section in _FEATURE_SECTIONS:
			root.addWidget(self._feature_card(**section))
		root.addWidget(self._tips_card())
		root.addStretch(1)

	def _intro_card(self) -> CardWidget:
		card = CardWidget()

		header = QHBoxLayout()
		title = QLabel("VERITAS는 무엇인가요?")
		title.setObjectName("CardTitle")
		header.addWidget(title, 0, Qt.AlignLeft | Qt.AlignVCenter)
		header.addStretch(1)
		header.addWidget(Badge("처음 오셨나요?", "info"), 0, Qt.AlignRight | Qt.AlignVCenter)
		card.layout.addLayout(header)

		desc = QLabel(_INTRO_TEXT)
		desc.setObjectName("CardSecondary")
		desc.setWordWrap(True)
		card.layout.addWidget(desc)
		return card

	def _flow_card(self) -> CardWidget:
		card = CardWidget("전체 작업 흐름")

		chips_row = QHBoxLayout()
		chips_row.setSpacing(8)
		for index, name in enumerate(_FLOW_STEPS):
			chip = QLabel(name)
			chip.setObjectName("GuideFlowChip")
			chips_row.addWidget(chip, 0, Qt.AlignVCenter)
			if index < len(_FLOW_STEPS) - 1:
				arrow = QLabel("→")
				arrow.setObjectName("GuideFlowArrow")
				chips_row.addWidget(arrow, 0, Qt.AlignVCenter)
		chips_row.addStretch(1)
		card.layout.addLayout(chips_row)

		note = QLabel("전부 다 쓸 필요는 없어요. <b>조사 → 요약</b>만 해도 자료 조사 보고서가 완성됩니다.")
		note.setObjectName("GuideNote")
		note.setWordWrap(True)
		note.setTextFormat(Qt.RichText)
		card.layout.addWidget(note)
		return card

	def _workspace_card(self) -> CardWidget:
		card = CardWidget("워크스페이스 = 작업 한 건")
		card.layout.addLayout(self._bullet_list(_WORKSPACE_BULLETS))
		return card

	def _tips_card(self) -> CardWidget:
		card = CardWidget("막힐 때 / 알아두면 좋은 점")
		card.layout.addLayout(self._bullet_list(_TIPS_BULLETS))
		return card

	def _feature_card(
		self,
		title: str,
		purpose: str,
		steps: list[str],
		tip: str | None = None,
	) -> CardWidget:
		card = CardWidget()

		title_label = QLabel(title)
		title_label.setObjectName("GuideSectionTitle")
		title_label.setWordWrap(True)
		card.layout.addWidget(title_label)

		purpose_label = QLabel(purpose)
		purpose_label.setObjectName("CardSecondary")
		purpose_label.setWordWrap(True)
		card.layout.addWidget(purpose_label)

		card.layout.addLayout(self._numbered_steps(steps))

		if tip:
			tip_label = QLabel("안내 · " + tip)
			tip_label.setObjectName("GuideNote")
			tip_label.setWordWrap(True)
			tip_label.setTextFormat(Qt.RichText)
			card.layout.addWidget(tip_label)
		return card

	def _numbered_steps(self, steps: list[str]) -> QVBoxLayout:
		box = QVBoxLayout()
		box.setContentsMargins(0, 2, 0, 2)
		box.setSpacing(8)
		for number, text in enumerate(steps, start=1):
			row = QHBoxLayout()
			row.setSpacing(10)

			num_label = QLabel(str(number))
			num_label.setObjectName("GuideStepNum")
			num_label.setFixedSize(20, 20)
			num_label.setAlignment(Qt.AlignCenter)
			row.addWidget(num_label, 0, Qt.AlignTop)

			text_label = QLabel(text)
			text_label.setObjectName("GuideStepText")
			text_label.setWordWrap(True)
			text_label.setTextFormat(Qt.RichText)
			row.addWidget(text_label, 1)

			box.addLayout(row)
		return box

	def _bullet_list(self, items: list[str]) -> QVBoxLayout:
		box = QVBoxLayout()
		box.setSpacing(7)
		for text in items:
			label = QLabel("•  " + text)
			label.setObjectName("GuideBullet")
			label.setWordWrap(True)
			label.setTextFormat(Qt.RichText)
			box.addWidget(label)
		return box
