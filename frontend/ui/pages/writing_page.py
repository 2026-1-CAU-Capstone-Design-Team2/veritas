from __future__ import annotations

from PySide6.QtWidgets import QFrame, QSizePolicy, QVBoxLayout, QWidget

from ...controllers import (
	AgentController,
	format_screen_event,
	get_job_manager,
	get_screen_event_store,
)
from ..windows.document_assist_window import SuggestionList


class DocumentAssistPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("DocumentAssistPage")
		self._build_ui()
		# screen-monitoring 이벤트 broker 연결 — floating 보조창과 같은 데이터 공유.
		self._screen_store = get_screen_event_store()
		self._screen_store.eventsAppended.connect(self._on_screen_events_appended)
		# 워크스페이스 전환 시 store.clear() → cleared emit → hydrate로 위젯 reset.
		self._screen_store.cleared.connect(self._hydrate_screen_suggestions)
		self.suggestion_list.feedbackSubmitted.connect(self._on_screen_feedback)
		self._hydrate_screen_suggestions()

	def _build_ui(self) -> None:
		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(0)

		panel = QFrame()
		panel.setObjectName("AssistPagePanel")
		# Hug the result list instead of filling the page, so the box ends right
		# below the last card with no trailing blank.
		panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
		panel_layout = QVBoxLayout(panel)
		panel_layout.setContentsMargins(12, 12, 12, 12)
		panel_layout.setSpacing(10)

		self.suggestion_list = SuggestionList(hug_content=True)

		panel_layout.addWidget(self.suggestion_list, 0)

		root.addWidget(panel, 0)
		root.addStretch(1)

	def _hydrate_screen_suggestions(self) -> None:
		"""store history 전체로 suggestion_list 재구성. show 시점마다 호출되어 양쪽 위젯 동기화 보장."""
		history = self._screen_store.get_history()
		suggestions: list[dict[str, str]] = []
		for item in history:
			formatted = format_screen_event(item)
			if formatted is None:
				continue
			category, text, tone = formatted
			suggestions.append({
				"id": str(item.get("eventId") or ""),
				"category": category,
				"text": text,
				"tone": tone,
				"interventionType": str(item.get("interventionType") or ""),
			})
		self.suggestion_list.set_suggestions(suggestions)

	def _on_screen_events_appended(self, items: list) -> None:
		"""실시간 append — 보이는 동안만 add_suggestion, 숨김 동안은 다음 hydrate에서 일괄 보충."""
		if not self.isVisible():
			return
		for item in items:
			if not isinstance(item, dict):
				continue
			formatted = format_screen_event(item)
			if formatted is None:
				continue
			category, text, tone = formatted
			self.suggestion_list.upsert_suggestion(
				str(item.get("eventId") or ""),
				category,
				text,
				tone,
				intervention_type=str(item.get("interventionType") or ""),
			)

	def _on_screen_feedback(self, event_id: str, intervention_type: str, action: str) -> None:
		"""사용자의 좋아요/싫어요/복사를 보상 로그로 비동기 전송 (UI 스레드 비차단)."""
		if not event_id:
			return
		get_job_manager().run_detached(
			AgentController().submit_screen_feedback,
			event_id,
			intervention_type,
			action,
			on_error=lambda err: print(f"[screen_feedback][warn] {err}"),
		)

	def showEvent(self, event) -> None:  # type: ignore[override]
		super().showEvent(event)
		self._hydrate_screen_suggestions()

	def update_assist_text(self, text: str) -> None:
		self.suggestion_list.set_suggestions([{"category": "수정", "text": text, "tone": "working"}])

	def append_assist_text(self, text: str) -> None:
		self.suggestion_list.add_suggestion("수정", text, "idle")
