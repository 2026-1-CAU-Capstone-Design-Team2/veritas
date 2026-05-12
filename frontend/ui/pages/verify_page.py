from __future__ import annotations

from functools import partial
from math import ceil

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
	QDialog,
	QDialogButtonBox,
	QFrame,
	QHBoxLayout,
	QLabel,
	QScrollArea,
	QSizePolicy,
	QVBoxLayout,
	QWidget,
)

from ...components.badges import Badge
from ...components.buttons import AppButton
from ...components.cards import CardWidget, DocumentCard


class VerifyPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._cards: list[dict[str, str | QWidget]] = []
		self._active_filter = "전체"
		self._page_size = 5
		self._current_page = 0

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(12)

		header = CardWidget("검증")
		subtitle = QLabel("정합성 결과와 출처 일치성을 확인합니다.")
		subtitle.setObjectName("PageSubtitle")
		header.layout.addWidget(subtitle)

		filter_row = QHBoxLayout()
		filter_row.setSpacing(8)
		for label in ["전체", "높음", "중간", "낮음"]:
			chip = AppButton(label, variant="filter")
			chip.clicked.connect(partial(self._set_filter, label))
			filter_row.addWidget(chip)

		filter_row.addStretch(1)

		header.layout.addLayout(filter_row)
		root.addWidget(header)

		docs = [
			(
				"AI 안전성 백서",
				"교차 출처 일치율: 92%",
				"높음",
				[
					"핵심 주장과 수치 근거가 일치하지만, 3장 결론의 출처 표기 형식이 혼재되어 있습니다.",
					"표 2-1의 기준 연도가 본문 표현과 다를 수 있으니 최종본에서 통일이 필요합니다.",
				],
			),
			(
				"오픈모델 리스크 노트",
				"교차 출처 일치율: 71%",
				"중간",
				[
					"리스크 레벨 분류 기준이 본문 중간에서 바뀌어 해석 혼선이 발생합니다.",
					"외부 인용 2건이 최신 버전 문서와 문구 차이가 있어 재검증이 필요합니다.",
				],
			),
			(
				"포럼 스냅샷",
				"교차 출처 일치율: 39%",
				"낮음",
				[
					"주요 주장에 대한 신뢰 가능한 1차 출처가 확인되지 않았습니다.",
					"수치 인용이 캡처 기반이라 원문 링크를 통한 사실 검증이 필요합니다.",
					"결론 문단의 표현이 단정적이므로 조건부 표현으로 완화가 필요합니다.",
				],
			),
		]

		for title_text, detail, level, issues in docs:
			tone = self._tone_for_level(level)

			right_panel = QWidget()
			right_panel.setAttribute(Qt.WA_StyledBackground, False)
			right_panel.setStyleSheet("background: transparent;")
			right_panel.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
			right_panel.setFixedWidth(104)
			right_layout = QVBoxLayout(right_panel)
			right_layout.setContentsMargins(0, 0, 0, 0)
			right_layout.setSpacing(6)

			badge = Badge(level, tone)
			action = AppButton("상세 보기", variant="ghost")
			action.setObjectName("VerifyDetailButton")
			action.setFixedHeight(28)
			action.setFixedWidth(92)
			action.clicked.connect(partial(self._show_issue_dialog, title_text, detail, level, issues))

			right_layout.addWidget(badge, 0, Qt.AlignRight)
			right_layout.addWidget(action, 0, Qt.AlignRight)
			right_layout.addStretch(1)

			wrapper = QWidget()
			wrapper_layout = QVBoxLayout(wrapper)
			wrapper_layout.setContentsMargins(0, 0, 0, 0)
			wrapper_layout.setSpacing(0)
			wrapper_layout.addWidget(DocumentCard(title=title_text, subtitle=detail, right_widget=right_panel))

			self._cards.append({"level": level, "widget": wrapper})
			root.addWidget(wrapper)

		pagination_row = QHBoxLayout()
		pagination_row.setContentsMargins(0, 0, 0, 0)
		pagination_row.setSpacing(8)

		self._prev_btn = AppButton("이전", variant="ghost")
		self._prev_btn.clicked.connect(self._go_prev_page)

		self._page_label = QLabel("0 / 0")
		self._page_label.setObjectName("PageSubtitle")

		self._next_btn = AppButton("다음", variant="ghost")
		self._next_btn.clicked.connect(self._go_next_page)

		pagination_row.addStretch(1)
		pagination_row.addWidget(self._prev_btn)
		pagination_row.addWidget(self._page_label)
		pagination_row.addWidget(self._next_btn)
		root.addLayout(pagination_row)

		root.addStretch(1)
		self._refresh_cards()

	def _set_filter(self, label: str) -> None:
		self._active_filter = label
		self._current_page = 0
		self._refresh_cards()

	def _refresh_cards(self) -> None:
		matched_indices = [
			i
			for i, item in enumerate(self._cards)
			if self._active_filter == "전체" or item["level"] == self._active_filter
		]

		if not matched_indices:
			for item in self._cards:
				item["widget"].setVisible(False)  # type: ignore[union-attr]
			self._page_label.setText("0 / 0")
			self._prev_btn.setEnabled(False)
			self._next_btn.setEnabled(False)
			return

		total_pages = ceil(len(matched_indices) / self._page_size)
		if self._current_page >= total_pages:
			self._current_page = total_pages - 1

		start = self._current_page * self._page_size
		end = start + self._page_size
		visible_set = set(matched_indices[start:end])

		for i, item in enumerate(self._cards):
			item["widget"].setVisible(i in visible_set)  # type: ignore[union-attr]

		self._page_label.setText(f"{self._current_page + 1} / {total_pages}")
		self._prev_btn.setEnabled(self._current_page > 0)
		self._next_btn.setEnabled(self._current_page < total_pages - 1)

	def _go_prev_page(self) -> None:
		if self._current_page <= 0:
			return
		self._current_page -= 1
		self._refresh_cards()

	def _go_next_page(self) -> None:
		self._current_page += 1
		self._refresh_cards()

	def _show_issue_dialog(self, title: str, detail: str, level: str, issues: list[str]) -> None:
		dialog = QDialog(self)
		dialog.setObjectName("VerifyDetailDialog")
		dialog.setWindowTitle("검증 상세")
		dialog.setModal(True)
		dialog.resize(620, 520)
		dialog.setStyleSheet(
			"""
			QDialog#VerifyDetailDialog {
				background-color: #F8FAFC;
			}
			QFrame#VerifyDialogHeader {
				background-color: #FFFFFF;
				border: 1px solid #E2E8F0;
				border-radius: 12px;
			}
			QFrame#IssueItem {
				background-color: #FFFFFF;
				border: 1px solid #E5E7EB;
				border-radius: 10px;
			}
			QLabel#IssueNumber {
				background-color: #EEF2FF;
				border: 1px solid #C7D2FE;
				border-radius: 12px;
				color: #3730A3;
				font-size: 11px;
				font-weight: 800;
				padding: 3px 8px;
			}
			"""
		)

		layout = QVBoxLayout(dialog)
		layout.setContentsMargins(18, 18, 18, 18)
		layout.setSpacing(12)

		header = QFrame()
		header.setObjectName("VerifyDialogHeader")
		header_layout = QVBoxLayout(header)
		header_layout.setContentsMargins(16, 14, 16, 14)
		header_layout.setSpacing(8)

		title_row = QHBoxLayout()
		title_row.setSpacing(8)

		title_label = QLabel(title)
		title_label.setObjectName("CardTitle")
		title_label.setWordWrap(True)

		badge = Badge(level, self._tone_for_level(level))

		title_row.addWidget(title_label, 1)
		title_row.addWidget(badge, 0, Qt.AlignTop)

		meta = QLabel(detail)
		meta.setObjectName("CardSecondary")
		meta.setWordWrap(True)

		summary = QLabel(self._summary_for_level(level))
		summary.setObjectName("CardPrimary")
		summary.setWordWrap(True)

		header_layout.addLayout(title_row)
		header_layout.addWidget(meta)
		header_layout.addWidget(summary)

		issue_title = QLabel("확인해야 할 항목")
		issue_title.setObjectName("CardTitle")

		scroll = QScrollArea()
		scroll.setWidgetResizable(True)
		scroll.setFrameShape(QFrame.NoFrame)
		scroll.setObjectName("PageScroll")

		issue_container = QWidget()
		issue_layout = QVBoxLayout(issue_container)
		issue_layout.setContentsMargins(0, 0, 0, 0)
		issue_layout.setSpacing(8)

		for index, issue in enumerate(issues, start=1):
			issue_layout.addWidget(self._issue_item(index, issue))

		recommendation = QLabel("권장 조치: 출처 표기 형식과 기준 연도를 먼저 통일한 뒤, 재검증이 필요한 인용을 우선 확인하세요.")
		recommendation.setObjectName("WarningSummary")
		recommendation.setWordWrap(True)
		issue_layout.addWidget(recommendation)
		issue_layout.addStretch(1)

		scroll.setWidget(issue_container)

		buttons = QDialogButtonBox(QDialogButtonBox.Close)
		buttons.rejected.connect(dialog.reject)
		buttons.accepted.connect(dialog.accept)

		layout.addWidget(header)
		layout.addWidget(issue_title)
		layout.addWidget(scroll, 1)
		layout.addWidget(buttons)

		dialog.exec()

	def _issue_item(self, index: int, text: str) -> QFrame:
		item = QFrame()
		item.setObjectName("IssueItem")

		layout = QHBoxLayout(item)
		layout.setContentsMargins(12, 10, 12, 10)
		layout.setSpacing(10)

		number = QLabel(str(index))
		number.setObjectName("IssueNumber")

		body = QLabel(text)
		body.setObjectName("CardSecondary")
		body.setWordWrap(True)
		body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)

		layout.addWidget(number, 0, Qt.AlignTop)
		layout.addWidget(body, 1)
		return item

	def _tone_for_level(self, level: str) -> str:
		if level == "높음":
			return "success"
		if level == "중간":
			return "warning"
		return "danger"

	def _summary_for_level(self, level: str) -> str:
		if level == "높음":
			return "대부분의 근거가 일치합니다. 최종본 품질을 위해 표기와 기준만 정리하면 됩니다."
		if level == "중간":
			return "핵심 흐름은 유지되지만 일부 인용과 분류 기준은 재확인이 필요합니다."
		return "신뢰도 보강이 필요합니다. 원문 링크와 1차 출처를 먼저 확보하세요."
