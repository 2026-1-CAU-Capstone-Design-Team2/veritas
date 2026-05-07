from __future__ import annotations

from functools import partial
from math import ceil

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

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
		subtitle = QLabel("정합성 결과와 출처 일치성만 확인합니다.")
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
					"핵심 주장과 수치 근거가 일치하지만, 3장 결론에 출처 표기 형식이 혼재되어 있습니다.",
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
			if level == "높음":
				tone = "success"
			elif level == "중간":
				tone = "warning"
			else:
				tone = "danger"

			right_panel = QWidget()
			right_panel.setAttribute(Qt.WA_StyledBackground, False)
			right_panel.setStyleSheet("background: transparent;")
			right_panel.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
			right_panel.setFixedWidth(96)
			right_layout = QVBoxLayout(right_panel)
			right_layout.setContentsMargins(0, 0, 0, 0)
			right_layout.setSpacing(4)

			badge = Badge(level, tone)
			action = AppButton("상세 보기", variant="ghost")
			action.setObjectName("VerifyDetailButton")
			action.setFixedHeight(26)
			action.setFixedWidth(86)
			action.clicked.connect(partial(self._show_issue_dialog, title_text, detail, level, issues))

			right_layout.addWidget(badge, 0, Qt.AlignRight)
			right_layout.addWidget(action, 0, Qt.AlignRight)
			right_layout.addStretch(1)

			wrapper = QWidget()
			wrapper_layout = QVBoxLayout(wrapper)
			wrapper_layout.setContentsMargins(0, 0, 0, 0)
			wrapper_layout.setSpacing(0)
			wrapper_layout.addWidget(
				DocumentCard(title=title_text, subtitle=detail, right_widget=right_panel)
			)

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
		dialog.setWindowTitle("상세 보기")
		dialog.setModal(True)
		dialog.resize(500, 320)

		layout = QVBoxLayout(dialog)
		layout.setContentsMargins(16, 14, 16, 14)
		layout.setSpacing(10)

		title_label = QLabel(title)
		title_label.setObjectName("CardPrimary")

		meta = QLabel(f"등급: {level}  |  {detail}")
		meta.setObjectName("CardSecondary")
		meta.setWordWrap(True)

		section = QLabel("문제 지점")
		section.setObjectName("CardTitle")

		issue_text = "\n".join([f"{idx}. {text}" for idx, text in enumerate(issues, start=1)])
		issue_label = QLabel(issue_text)
		issue_label.setObjectName("CardSecondary")
		issue_label.setWordWrap(True)
		issue_label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)

		buttons = QDialogButtonBox(QDialogButtonBox.Close)
		buttons.rejected.connect(dialog.reject)
		buttons.accepted.connect(dialog.accept)

		layout.addWidget(title_label)
		layout.addWidget(meta)
		layout.addWidget(section)
		layout.addWidget(issue_label)
		layout.addStretch(1)
		layout.addWidget(buttons)

		dialog.exec()
