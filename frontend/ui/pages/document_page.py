from __future__ import annotations

import html
import re

from PySide6.QtWidgets import QLabel, QTextBrowser, QTextEdit, QVBoxLayout, QWidget

from ...api_common import ApiError, current_workspace_id
from ...components.badges import Badge
from ...components.cards import CardWidget
from ...controllers import AgentController
from ..markdown_view import apply_markdown


# Bare http(s) URLs in the collected-document list are turned into clickable
# links; trailing sentence punctuation is kept out of the linked span.
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_URL_TRAIL = ".,;:!?"


def _linkify_plain_text(text: str) -> str:
	"""Render a plain-text block as HTML with bare http(s) URLs made clickable.

	Whitespace and newlines are preserved (``white-space: pre-wrap``) so the
	list still reads exactly like the original plain text — only the addresses
	become links.
	"""
	out: list[str] = []
	cursor = 0
	for match in _URL_RE.finditer(text):
		out.append(html.escape(text[cursor:match.start()]))
		url = match.group(0)
		trail = ""
		while url and url[-1] in _URL_TRAIL:
			trail = url[-1] + trail
			url = url[:-1]
		href = html.escape(url, quote=True)
		out.append(f'<a href="{href}">{html.escape(url)}</a>{html.escape(trail)}')
		cursor = match.end()
	out.append(html.escape(text[cursor:]))
	return f'<div style="white-space: pre-wrap;">{"".join(out)}</div>'


class DocumentPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._workspace_id = current_workspace_id()
		self._controller = AgentController()

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(12)

		summary_card = CardWidget("문서")
		summary_subtitle = QLabel("AutoSurvey가 생성한 최종 보고서(final.md)를 markdown 형태로 확인합니다.")
		summary_subtitle.setObjectName("PageSubtitle")
		summary_card.layout.addWidget(summary_subtitle)

		summary_badge = Badge("요약본", "info")
		summary_card.layout.addWidget(summary_badge)

		self.summary_text = QTextEdit()
		self.summary_text.setObjectName("DocEditor")
		self.summary_text.setReadOnly(True)
		self.summary_text.setMinimumHeight(360)
		summary_card.layout.addWidget(self.summary_text)
		root.addWidget(summary_card)

		merged_card = CardWidget("수집 문서")
		merged_badge = Badge("제목 및 링크", "neutral")
		merged_card.layout.addWidget(merged_badge)

		# QTextBrowser (not QTextEdit) so the collected-document addresses can be
		# rendered as links that open in the system browser when clicked.
		self.merged_text = QTextBrowser()
		self.merged_text.setObjectName("DocEditor")
		self.merged_text.setReadOnly(True)
		self.merged_text.setOpenExternalLinks(True)
		self.merged_text.setMinimumHeight(220)
		merged_card.layout.addWidget(self.merged_text)
		root.addWidget(merged_card)

		root.addStretch(1)
		self.refresh()

	def refresh(self) -> None:
		self._workspace_id = current_workspace_id()
		try:
			summary = self._controller.get_document_summary(self._workspace_id)
			merged = self._controller.get_document_merged(self._workspace_id)
		except ApiError as e:
			self.summary_text.setPlainText(f"API 요청 실패: {e}")
			self.merged_text.clear()
			return

		if summary.strip():
			apply_markdown(self.summary_text, summary)
		else:
			self.summary_text.setPlainText("아직 표시할 final.md가 없습니다. 조사 섹션에서 AutoSurvey를 먼저 실행하세요.")
		self.merged_text.setHtml(_linkify_plain_text(merged or "아직 수집 문서 목록이 없습니다."))
