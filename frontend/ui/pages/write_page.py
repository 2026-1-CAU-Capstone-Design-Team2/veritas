from __future__ import annotations

from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from ..windows.document_assist_window import ChatInputBar, ChatPanel


class WritePage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._mode = "research"
		self._build_ui()
		self.chat_panel.add_message("VERITAS", "메시지를 입력하면 워크스페이스 기반 AI 답변을 제공합니다.", False)

	def _build_ui(self) -> None:
		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(0)

		panel = QFrame()
		panel.setObjectName("AssistPagePanel")
		panel_layout = QVBoxLayout(panel)
		panel_layout.setContentsMargins(12, 12, 12, 12)
		panel_layout.setSpacing(10)

		self.chat_panel = ChatPanel("문서 채팅")
		self.input_bar = ChatInputBar()
		self.input_bar.modeChanged.connect(self._set_mode)
		self.input_bar.sendRequested.connect(self._send_message)

		panel_layout.addWidget(self.chat_panel, 1)
		panel_layout.addWidget(self.input_bar)
		root.addWidget(panel, 1)

	def _set_mode(self, mode: str) -> None:
		self._mode = "rag" if mode == "rag" else "research"

	def _assistant_reply(self, text: str) -> str:
		lowered = text.lower()
		if self._mode == "rag":
			if "보고" in lowered or "브리프" in lowered:
				return "RAG 모드: 현재 워크스페이스에 저장된 요약본과 스크랩 합본을 기준으로 개요, 핵심 리스크, 실행 권고 순서로 답변하겠습니다."
			if "출처" in lowered or "근거" in lowered:
				return "RAG 모드: 저장된 문서 근거를 우선 확인하고, 답변에 사용할 근거 문장과 검토가 필요한 부분을 함께 정리하겠습니다."
			return "RAG 모드: 선택한 워크스페이스의 기존 문서와 검증 결과를 바탕으로 답변하겠습니다."

		if "보고" in lowered or "브리프" in lowered:
			return "자료조사 모드: 새로 조사할 쟁점과 확인할 출처 후보를 먼저 정리한 뒤, 경영진 보고용 구조로 답변하겠습니다."
		if "메일" in lowered or "안내" in lowered or "공지" in lowered:
			return "자료조사 모드: 필요한 배경 자료와 확인 포인트를 정리한 뒤, 수신자 중심의 짧고 명확한 문장으로 초안을 작성하겠습니다."
		if "3문단" in lowered or "문단" in lowered or "초안" in lowered:
			return "자료조사 모드: 요청한 길이에 맞춰 추가 조사가 필요한 항목과 바로 쓸 수 있는 초안 구조를 함께 제안하겠습니다."
		return "자료조사 모드: 질문에 맞는 조사 방향, 확인할 레퍼런스, 결과 정리 방식을 제안하겠습니다."

	def _send_message(self, message: str) -> None:
		text = message.rstrip("\n")
		if not text.strip():
			return

		self.chat_panel.add_message("나", text, True)
		self.chat_panel.add_message("VERITAS", self._assistant_reply(text), False)
