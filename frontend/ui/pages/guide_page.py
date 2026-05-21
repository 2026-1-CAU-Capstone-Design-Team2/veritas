from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...components.badges import Badge
from ...components.cards import CardWidget

# --- Icons -----------------------------------------------------------------
# The guide reuses the same line icons as the sidebar so each feature is
# visually tied to its menu entry. The sidebar SVGs are drawn for the dark
# sidebar (light slate stroke), so here we recolor the stroke to the app's
# indigo accent and render onto white cards. The editor has no sidebar entry,
# so it gets an inline pen icon drawn in the same Lucide style.
_ICON_DIR = Path(__file__).resolve().parents[1] / "public" / "images" / "icons"
_ICON_COLOR = "#4F46E5"
_ICON_RENDER_SCALE = 3  # render oversized then downscale for crisp edges

_PEN_SVG = (
	'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
	'fill="none" stroke="#CBD5E1" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
	'<path d="M12 20h9"/>'
	'<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>'
	"</svg>"
)


def _icon_svg(key: str) -> str:
	if key == "pen":
		return _PEN_SVG
	path = _ICON_DIR / f"{key}.svg"
	if path.exists():
		return path.read_text(encoding="utf-8")
	return ""


_PIXMAP_CACHE: dict[tuple[str, int], QPixmap] = {}


def _icon_pixmap(key: str, size: int) -> QPixmap:
	cache_key = (key, size)
	cached = _PIXMAP_CACHE.get(cache_key)
	if cached is not None:
		return cached

	svg = _icon_svg(key).replace('stroke="#CBD5E1"', f'stroke="{_ICON_COLOR}"')
	if not svg:
		pixmap = QPixmap()
		_PIXMAP_CACHE[cache_key] = pixmap
		return pixmap

	renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
	scaled = size * _ICON_RENDER_SCALE
	pixmap = QPixmap(scaled, scaled)
	pixmap.fill(Qt.transparent)
	painter = QPainter(pixmap)
	painter.setRenderHint(QPainter.Antialiasing, True)
	renderer.render(painter, QRectF(0, 0, scaled, scaled))
	painter.end()
	pixmap.setDevicePixelRatio(_ICON_RENDER_SCALE)
	_PIXMAP_CACHE[cache_key] = pixmap
	return pixmap


# Page-scoped styling for the few elements the global stylesheet doesn't cover.
# Cards, titles, and body text reuse the global object names (CardWidget /
# CardTitle / CardSecondary).
_PAGE_QSS = """
QLabel#GuideStageNum {
	background-color: #4F46E5;
	color: #FFFFFF;
	border-radius: 13px;
	font-size: 13px;
	font-weight: 800;
}
QLabel#GuideStageTitle {
	color: #0F172A;
	font-size: 15px;
	font-weight: 800;
}
QLabel#GuideStageDesc {
	color: #64748B;
	font-size: 12px;
	font-weight: 600;
}
QLabel#GuideIconTile {
	background-color: #EEF2FF;
	border: 1px solid #C7D2FE;
	border-radius: 11px;
}
QLabel#GuideFeatName {
	color: #0F172A;
	font-size: 15px;
	font-weight: 800;
}
QLabel#GuideFeatSub {
	color: #64748B;
	font-size: 12px;
	font-weight: 600;
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
QFrame#GuideFlowChip {
	background-color: #EEF2FF;
	border: 1px solid #C7D2FE;
	border-radius: 15px;
}
QLabel#GuideFlowChipText {
	color: #3730A3;
	font-size: 12px;
	font-weight: 800;
	background-color: transparent;
	border: none;
}
QLabel#GuideFlowChipIcon {
	background-color: transparent;
	border: none;
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
	"내 컴퓨터에서 동작하는 AI 리서치·문서 작성 도우미예요. 알아보고 싶은 주제만 적으면 "
	"웹에서 자료를 모아 요약하고, 믿을 만한 자료인지 확인한 뒤, 그 내용으로 문서 초안까지 만들어 줍니다. "
	"아래 순서대로 따라가 보세요."
)

# Top strip: the core journey, left to right, with each step's own icon.
_FLOW_STEPS = [
	("collect", "조사"),
	("document", "요약"),
	("verify", "검증"),
	("draft", "초안"),
	("pen", "글쓰기"),
	("feedback", "피드백"),
]

# Shown right after the intro: the dashboard is the screen users land on when
# they open the app, so it is introduced before the step-by-step journey.
_DASHBOARD_FEATURE = {
	"icon": "dashboard",
	"name": "대시보드",
	"sub": "앱을 열면 가장 먼저 보이는 화면",
	"purpose": "작업 현황을 한눈에 보여 주는 홈 화면이에요. 왼쪽 메뉴에서 언제든 다시 돌아올 수 있어요.",
	"steps": [
		"처리한 문서 수, 검증 완료 수, 최근 작업 공간을 확인합니다.",
		"무엇부터 할지 모르겠다면 아래 흐름을 따라 <b>조사</b>부터 시작하세요.",
	],
	"tip": None,
}

_WORKSPACE_BULLETS = [
	"새 주제로 조사를 시작하면 작업 공간(워크스페이스)이 자동으로 새로 만들어져요.",
	"조사·요약·검증·채팅 기록이 작업 공간별로 따로 저장됩니다.",
	"이전 작업으로 돌아가려면 사이드바 맨 아래 <b>워크스페이스 전환</b>을 누르세요.",
]

# Features grouped into the three stages of a typical session, in the order a
# user actually moves through them.
_STAGES = [
	{
		"num": "1",
		"title": "자료 모으기",
		"desc": "먼저 믿을 만한 자료부터 모읍니다.",
		"features": [
			{
				"icon": "collect",
				"name": "조사",
				"sub": "주제 넣고 자료 모으기",
				"purpose": "가장 먼저 하는 단계예요. 알아보고 싶은 주제를 적으면 AI가 웹에서 자료를 모아 정리해 줍니다.",
				"steps": [
					"<b>조사 내용 입력</b> 칸에 주제를 문장으로 적어요. (예: 2026년 AI 규제 동향을 산업별로 조사해줘)",
					"꼭 참고할 사이트가 있다면 <b>레퍼런스 사이트</b>에 URL을 더해 줍니다.",
					"<b>최대 조사 문서 수</b>를 버튼으로 정합니다. (기본 15개)",
					"<b>조사 실행</b>을 누르면 자료가 실시간으로 한 줄씩 쌓여요.",
				],
				"tip": "다 되면 <b>이 보고서로 글쓰기</b> 버튼으로 바로 문서 작성으로 넘어갈 수 있어요.",
			},
			{
				"icon": "document",
				"name": "요약",
				"sub": "완성된 보고서 읽기",
				"purpose": "조사로 모은 자료를 하나로 합친 최종 보고서를 보기 좋게 보여줍니다.",
				"steps": [
					"왼쪽 메뉴에서 <b>요약</b>을 누르면 보고서가 자동으로 나타나요.",
					"스크롤하면서 정리된 내용을 확인합니다.",
				],
				"tip": "내용이 비어 있다면 먼저 <b>조사</b>를 실행해야 해요.",
			},
			{
				"icon": "verify",
				"name": "검증",
				"sub": "자료가 믿을 만한지 확인",
				"purpose": "모은 자료의 신뢰도와 보고서 구성을 AI가 자동으로 점검해 줍니다.",
				"steps": [
					"<b>검증 시작</b>을 누릅니다. (이미 했다면 <b>재검증</b>)",
					"끝나면 자료 수와 신뢰도 분포(높음·중간·낮음)가 정리돼요.",
					"각 자료의 <b>상세 보기</b>로 신뢰도 근거를 확인합니다.",
				],
				"tip": None,
			},
		],
	},
	{
		"num": "2",
		"title": "문서 만들기",
		"desc": "모은 자료로 글을 쓰고 다듬습니다.",
		"features": [
			{
				"icon": "draft",
				"name": "초안",
				"sub": "문서 뼈대 자동 생성",
				"purpose": "조사 내용을 바탕으로 문서의 첫 골격을 만들어 줍니다. 두 가지 방식 중에 골라요.",
				"steps": [
					"<b>양식 파일 사용</b>: 가진 양식(.md·.txt)을 올리면 제목 구조를 읽어 목차를 채워 줍니다.",
					"<b>직접 구성</b>: 문서 종류와 톤·분량을 고르고 목차를 정리합니다.",
					"만들어진 초안은 <b>초안 복사</b>로 가져가거나 <b>에디터에서 이어쓰기</b>로 넘어가요.",
				],
				"tip": None,
			},
			{
				"icon": "pen",
				"name": "에디터",
				"sub": "본격적으로 글쓰기",
				"purpose": "문서를 직접 쓰고 다듬는 작업 창이에요. 화면 위 글쓰기 버튼이나 조사·초안 화면에서 열 수 있어요.",
				"steps": [
					"가운데 본문 영역에 글을 씁니다. (글자·단어 수가 아래에 표시돼요)",
					"문장을 선택하고 <b>존댓말로·자연스럽게·짧게·근거 추가</b> 같은 버튼으로 다듬습니다.",
					"결과를 <b>본문에 대치</b>로 바꾸거나 <b>복사</b>해서 활용해요.",
				],
				"tip": None,
			},
			{
				"icon": "document_assist",
				"name": "문서 보조",
				"sub": "글 쓰는 동안 실시간 도움",
				"purpose": "글을 쓰는 동안 화면에 떠서 수정 제안을 실시간으로 보여 주는 작은 창이에요.",
				"steps": [
					"화면 위 <b>AI 보조창</b> 버튼(또는 Ctrl+Shift+A)으로 엽니다.",
					"<b>실시간 수정 결과</b>에 제안 카드가 쌓이고, 각 카드의 <b>복사</b>로 바로 활용해요.",
				],
				"tip": None,
			},
			{
				"icon": "write",
				"name": "채팅",
				"sub": "자료에 대해 질문하기",
				"purpose": "모아 둔 자료를 바탕으로 AI와 대화하며 궁금한 점을 물어봐요.",
				"steps": [
					"아래 입력칸에 질문을 적고 <b>전송</b>(또는 Enter)을 누릅니다.",
					"모드 버튼으로 <b>채팅</b>(자료 기반 답변)과 <b>조사</b>(새로 조사)를 바꿔요.",
				],
				"tip": "예: 이 문단 자연스러워? / 근거가 부족한 부분 찾아줘",
			},
		],
	},
	{
		"num": "3",
		"title": "점검하고 관리하기",
		"desc": "다 쓴 문서를 점검하고 작업 환경을 설정해요.",
		"features": [
			{
				"icon": "feedback",
				"name": "피드백",
				"sub": "내 문서 점검받기",
				"purpose": "이미 가진 문서 파일을 올려 AI에게 자동 평가를 받아요.",
				"steps": [
					"<b>문서 업로드</b>로 파일을 고릅니다. (txt·md·pdf·docx·pptx·hwp 지원)",
					"올리면 자동으로 분석되고, 목록에서 파일을 누르면 결과가 나타나요.",
					"결과는 <b>주요 피드백</b>(문제점)과 <b>개선 제안</b>으로 정리됩니다.",
				],
				"tip": None,
			},
			{
				"icon": "settings",
				"name": "설정",
				"sub": "AI 모델·참고 폴더 설정",
				"purpose": "AI 모델과 참고 폴더 등 작업 환경을 내게 맞게 설정해요.",
				"steps": [
					"<b>모델 설정</b>: 작을수록 빠르고, 클수록 똑똑한 모델을 골라요.",
					"<b>로컬 접근 폴더</b>: AI가 참고할 내 폴더를 추가합니다.",
				],
				"tip": None,
			},
		],
	},
]

_TIPS_BULLETS = [
	"<b>버튼이 회색이고 안 눌려요</b> — 다른 작업이 진행 중이면 잠깐 잠겨요. 끝나면 다시 켜집니다.",
	"<b>요약·검증 화면이 비어 있어요</b> — 그 작업 공간에서 조사를 먼저 실행하세요.",
	"<b>처음이라면</b> 조사 → 요약만 해 보고, 익숙해지면 검증·초안·피드백을 더해 가세요.",
]


class GuidePage(QWidget):
	"""Static, card-based usage guide. Reuses the app's CardWidget aesthetic and
	the sidebar's own icons so the page matches the rest of the UI; content is
	data-driven from the module tables above so it is easy to edit."""

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setStyleSheet(_PAGE_QSS)

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(14)

		root.addWidget(self._intro_card())
		root.addWidget(self._feature_card(**_DASHBOARD_FEATURE))
		root.addWidget(self._flow_card())
		root.addWidget(self._workspace_card())
		for stage in _STAGES:
			root.addWidget(self._stage_header(stage["num"], stage["title"], stage["desc"]))
			for feature in stage["features"]:
				root.addWidget(self._feature_card(**feature))
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
		for index, (icon_key, name) in enumerate(_FLOW_STEPS):
			chips_row.addWidget(self._flow_chip(icon_key, name), 0, Qt.AlignVCenter)
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

	def _flow_chip(self, icon_key: str, name: str) -> QFrame:
		chip = QFrame()
		chip.setObjectName("GuideFlowChip")
		row = QHBoxLayout(chip)
		row.setContentsMargins(11, 6, 13, 6)
		row.setSpacing(6)

		icon = QLabel()
		icon.setObjectName("GuideFlowChipIcon")
		icon.setPixmap(_icon_pixmap(icon_key, 15))
		row.addWidget(icon, 0, Qt.AlignVCenter)

		text = QLabel(name)
		text.setObjectName("GuideFlowChipText")
		row.addWidget(text, 0, Qt.AlignVCenter)
		return chip

	def _stage_header(self, num: str, title: str, desc: str) -> QWidget:
		wrapper = QWidget()
		row = QHBoxLayout(wrapper)
		row.setContentsMargins(4, 6, 4, 0)
		row.setSpacing(11)

		num_label = QLabel(num)
		num_label.setObjectName("GuideStageNum")
		num_label.setFixedSize(26, 26)
		num_label.setAlignment(Qt.AlignCenter)
		row.addWidget(num_label, 0, Qt.AlignVCenter)

		text_box = QVBoxLayout()
		text_box.setSpacing(1)
		title_label = QLabel(title)
		title_label.setObjectName("GuideStageTitle")
		desc_label = QLabel(desc)
		desc_label.setObjectName("GuideStageDesc")
		text_box.addWidget(title_label)
		text_box.addWidget(desc_label)
		row.addLayout(text_box, 1)
		return wrapper

	def _workspace_card(self) -> CardWidget:
		card = CardWidget("작업 공간(워크스페이스) = 작업 한 건")
		card.layout.addLayout(self._bullet_list(_WORKSPACE_BULLETS))
		return card

	def _tips_card(self) -> CardWidget:
		card = CardWidget("막힐 때 / 알아두면 좋은 점")
		card.layout.addLayout(self._bullet_list(_TIPS_BULLETS))
		return card

	def _feature_card(
		self,
		icon: str,
		name: str,
		sub: str,
		purpose: str,
		steps: list[str],
		tip: str | None = None,
	) -> CardWidget:
		card = CardWidget()

		header = QHBoxLayout()
		header.setSpacing(12)

		icon_tile = QLabel()
		icon_tile.setObjectName("GuideIconTile")
		icon_tile.setFixedSize(40, 40)
		icon_tile.setAlignment(Qt.AlignCenter)
		icon_tile.setPixmap(_icon_pixmap(icon, 22))
		header.addWidget(icon_tile, 0, Qt.AlignTop)

		title_box = QVBoxLayout()
		title_box.setSpacing(2)
		name_label = QLabel(name)
		name_label.setObjectName("GuideFeatName")
		sub_label = QLabel(sub)
		sub_label.setObjectName("GuideFeatSub")
		sub_label.setWordWrap(True)
		title_box.addWidget(name_label)
		title_box.addWidget(sub_label)
		header.addLayout(title_box, 1)
		card.layout.addLayout(header)

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
