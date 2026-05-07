from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...components.cards import CardWidget, StatTile


class DashboardPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(14)

		stats_row = QHBoxLayout()
		stats_row.setSpacing(12)
		stats_row.addWidget(StatTile("처리 문서", "48", "이번 주 +12"))
		stats_row.addWidget(StatTile("검증 완료 워크스페이스", "6", "이번 주 +2"))
		stats_row.addWidget(StatTile("피드백 완료율", "98%", "지난주 대비 +8%"))
		root.addLayout(stats_row)

		recent = CardWidget("최근 작업")
		recent_subtitle = QLabel("최근 워크스페이스 진행 현황과 문서 작업 이력을 확인하세요.")
		recent_subtitle.setObjectName("PageSubtitle")
		recent_subtitle.setWordWrap(True)
		recent.layout.addWidget(recent_subtitle)

		workspace_card = CardWidget("최근 작업 워크스페이스")
		workspace_data = [
			("AI 안전성 브리프 워크스페이스", "마지막 작업: 10분 전"),
			("규제 대응 메모 워크스페이스", "마지막 작업: 35분 전"),
			("기후 정책 검증 워크스페이스", "마지막 작업: 1시간 전"),
		]
		for name, detail in workspace_data:
			row = QHBoxLayout()
			row.setSpacing(8)
			text_col = QVBoxLayout()
			text_col.setSpacing(2)

			name_label = QLabel(name)
			name_label.setObjectName("CardPrimary")
			detail_label = QLabel(detail)
			detail_label.setObjectName("CardSecondary")

			text_col.addWidget(name_label)
			text_col.addWidget(detail_label)

			row.addLayout(text_col, 1)
			workspace_card.layout.addLayout(row)

		doc_card = CardWidget("최근 문서/피드백")
		doc_data = [
			("최근 피드백 완료 문서", "2026_Q2_리스크_브리프.docx"),
			("최근 초안 생성", "규제 대응 메모 초안 v3"),
			("최근 업로드 문서", "시장동향_요약_0406.pdf"),
			("검토 필요", "출처 불일치 2건, 근거 부족 1건"),
		]
		for name, detail in doc_data:
			row = QHBoxLayout()
			row.setSpacing(8)
			text_col = QVBoxLayout()
			text_col.setSpacing(2)

			name_label = QLabel(name)
			name_label.setObjectName("CardPrimary")
			detail_label = QLabel(detail)
			detail_label.setObjectName("CardSecondary")

			text_col.addWidget(name_label)
			text_col.addWidget(detail_label)

			row.addLayout(text_col, 1)
			doc_card.layout.addLayout(row)

		recent.layout.addWidget(workspace_card)
		recent.layout.addWidget(doc_card)

		root.addWidget(recent)
		root.addStretch(1)
