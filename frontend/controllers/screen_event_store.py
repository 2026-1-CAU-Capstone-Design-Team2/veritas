"""Screen-monitoring 이벤트 broker — 단일 push source + history 보존.

polling worker가 raw item dict 리스트를 append하면 eventsAppended signal로
모든 subscriber(보조창 + DocumentAssistPage)가 동시에 알림 받음. history는
누적 보관되어 늦게 등장한 위젯도 get_history()로 그동안의 이벤트 조회 가능.

이벤트 item 스키마: backend의 GET /screen-monitoring/events 응답 items 그대로
(`answer`, `triggerText`, ... 등 dict). 가공은 위젯 측 책임 — format_screen_event helper 제공.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal


def format_screen_event(item: dict[str, Any]) -> tuple[str, str, str] | None:
	"""screen-monitoring item을 SuggestionList.add_suggestion 인자로 변환.
	반환: (category, text, tone) 또는 answer가 비어있으면 None.
	"""
	answer = str(item.get("answer") or "").strip()
	if not answer:
		return None
	trigger = str(item.get("triggerText") or "").strip()
	category = "실시간 보조"
	text = f"{trigger}\n→ {answer}" if trigger else answer
	return (category, text, "working")


class ScreenEventStore(QObject):
	"""Screen-monitoring 이벤트의 단일 broker — append + history + subscriber 알림."""

	eventsAppended = Signal(list)  # 새로 append된 raw item dict 리스트
	cleared = Signal()  # history 초기화 — 구독자는 자기 위젯도 reset

	def __init__(self, parent: QObject | None = None) -> None:
		super().__init__(parent)
		self._history: list[dict[str, Any]] = []

	def append(self, items: list) -> None:
		"""items를 history에 누적 + eventsAppended emit. dict 아닌 항목은 무시."""
		if not items:
			return
		valid = [item for item in items if isinstance(item, dict)]
		if not valid:
			return
		self._history.extend(valid)
		self.eventsAppended.emit(valid)

	def get_history(self) -> list[dict[str, Any]]:
		"""누적된 이벤트 사본 반환."""
		return list(self._history)

	def clear(self) -> None:
		"""history 비움 + cleared emit. 워크스페이스 전환 시 양쪽 위젯 동시 reset 트리거."""
		self._history.clear()
		self.cleared.emit()


_store: ScreenEventStore | None = None


def get_screen_event_store() -> ScreenEventStore:
	"""프로세스 단위 singleton — 모든 위젯이 같은 store 인스턴스 공유."""
	global _store
	if _store is None:
		_store = ScreenEventStore()
	return _store
