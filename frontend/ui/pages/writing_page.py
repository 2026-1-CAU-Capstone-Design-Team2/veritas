from __future__ import annotations

from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from ..windows.document_assist_window import SuggestionList


class DocumentAssistPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("DocumentAssistPage")
		self._build_ui()
		self._load_demo_data()

	def _build_ui(self) -> None:
		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(0)

		panel = QFrame()
		panel.setObjectName("AssistPagePanel")
		panel_layout = QVBoxLayout(panel)
		panel_layout.setContentsMargins(12, 12, 12, 12)
		panel_layout.setSpacing(10)

		self.suggestion_list = SuggestionList()

		panel_layout.addWidget(self.suggestion_list, 1)

		root.addWidget(panel, 1)

	def _load_demo_data(self) -> None:
		self.suggestion_list.set_suggestions(
			[
				{
					"category": "수정",
					"text": "본 보고서는 2026년 AI 규제 변화가 기업 운영에 미치는 영향을 분석하고, 우선 대응이 필요한 리스크를 정리합니다.",
					"tone": "working",
				},
				{
					"category": "근거 보강",
					"text": "효율성 개선 효과는 내부 처리 시간 비교 데이터 또는 외부 벤치마크 수치를 함께 제시하면 더 설득력 있습니다.",
					"tone": "warning",
				},
			]
		)

	def update_assist_text(self, text: str) -> None:
		self.suggestion_list.set_suggestions([{"category": "수정", "text": text, "tone": "working"}])

	def append_assist_text(self, text: str) -> None:
		self.suggestion_list.add_suggestion("수정", text, "idle")
