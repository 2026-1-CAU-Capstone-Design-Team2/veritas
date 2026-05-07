from __future__ import annotations

from PySide6.QtWidgets import QLabel, QTextEdit, QVBoxLayout, QWidget

from ...components.badges import Badge
from ...components.cards import CardWidget


class DocumentPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(12)

		summary_card = CardWidget("문서")
		summary_subtitle = QLabel("스크랩한 웹 콘텐츠의 합본과 요약본을 함께 확인합니다.")
		summary_subtitle.setObjectName("PageSubtitle")
		summary_card.layout.addWidget(summary_subtitle)

		summary_badge = Badge("요약본", "info")
		summary_card.layout.addWidget(summary_badge)

		summary_text = QTextEdit()
		summary_text.setObjectName("DocEditor")
		summary_text.setReadOnly(True)
		summary_text.setMinimumHeight(180)
		summary_text.setPlainText(
			"요약본\n\n"
			"1. APAC 지역은 AI 거버넌스에서 투명성, 감사 가능성, 책임 추적성을 우선 요구합니다.\n"
			"2. 엔터프라이즈 환경에서는 모델 위험평가와 데이터 출처 관리 체계가 필수로 강화되고 있습니다.\n"
			"3. 운영 단계에서는 보안 통제, 로그 보존, 인시던트 대응 절차를 문서화해야 규제 대응이 수월합니다.\n"
			"4. 실무 권고로는 검증-작성-피드백 전 단계에서 근거 문서의 상태와 신뢰도를 일관되게 관리해야 합니다."
		)
		summary_card.layout.addWidget(summary_text)
		root.addWidget(summary_card)

		merged_card = CardWidget("스크랩 합본")
		merged_badge = Badge("원문 합본", "neutral")
		merged_card.layout.addWidget(merged_badge)

		merged_text = QTextEdit()
		merged_text.setObjectName("DocEditor")
		merged_text.setReadOnly(True)
		merged_text.setMinimumHeight(380)
		merged_text.setPlainText(
			"스크랩 합본\n\n"
			"[문서 1] APAC 지역 AI 규제 동향\n"
			"- 주요 내용: 국가별 규제 프레임워크에서 고위험 AI 분류와 보고 의무가 확대되고 있습니다.\n"
			"- 핵심 포인트: 정책 준수 증빙을 위해 모델 개발/배포 이력의 추적성이 요구됩니다.\n\n"
			"[문서 2] 엔터프라이즈 LLM 벤치마크 2026\n"
			"- 주요 내용: 정확도뿐 아니라 안정성, 환각률, 재현성 지표가 도입되고 있습니다.\n"
			"- 핵심 포인트: 운영 단계 평가에서 데이터 거버넌스와 접근 제어가 성능만큼 중요합니다.\n\n"
			"[문서 3] AI 워크플로우 보안 가이드\n"
			"- 주요 내용: 검증/작성/피드백 파이프라인 전 구간에서 보안 통제와 감사 로그를 권고합니다.\n"
			"- 핵심 포인트: 보안 정책과 품질 검증 기준을 동일 파이프라인에서 관리해야 리스크를 낮출 수 있습니다.\n\n"
			"참고\n"
			"- 본 화면은 스크랩 결과를 합쳐서 읽을 수 있는 뷰어이며, 실제 연동 시 저장된 원문 전문을 그대로 렌더링하도록 확장할 수 있습니다."
		)
		merged_card.layout.addWidget(merged_text)
		root.addWidget(merged_card)

		root.addStretch(1)
