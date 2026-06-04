from __future__ import annotations

import html

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QCursor, QDesktopServices
from PySide6.QtWidgets import (
	QApplication,
	QFrame,
	QLabel,
	QTextBrowser,
	QVBoxLayout,
	QWidget,
)

from ...api_common import current_workspace_id
from ...citation_links import linkify_citations, parse_citation_url
from ...components.badges import Badge
from ...components.cards import CardWidget
from ...controllers import AgentController, get_job_manager
from ..markdown_view import apply_markdown


class DocumentPage(QWidget):
	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._workspace_id = current_workspace_id()
		self._controller = AgentController()
		# Monotonic guard so an out-of-order summary fetch can't overwrite a
		# newer refresh (rapid page switches / workspace changes).
		self._summary_token = 0
		# Separate guard for citation lookups so a slow response from an earlier
		# click can't pop over a newer one.
		self._citation_token = 0
		self._popup: CitationPopup | None = None

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(12)

		summary_card = CardWidget("요약")

		summary_badge = Badge("요약본", "info")
		summary_card.layout.addWidget(summary_badge)

		# QTextBrowser (not QTextEdit) so the rendered citation links emit
		# anchorClicked. setOpenLinks(False) stops it from trying to navigate
		# the custom veritas-citation:// scheme itself — we handle the click.
		self.summary_text = QTextBrowser()
		self.summary_text.setObjectName("DocEditor")
		self.summary_text.setReadOnly(True)
		self.summary_text.setOpenLinks(False)
		self.summary_text.setOpenExternalLinks(False)
		self.summary_text.anchorClicked.connect(self._on_anchor_clicked)
		self.summary_text.setMinimumHeight(360)
		# Stretch the editor inside the card, and the card across the page, so
		# the summary fills the whole screen.
		summary_card.layout.addWidget(self.summary_text, 1)
		root.addWidget(summary_card, 1)

		self.refresh()

	def refresh(self) -> None:
		self._workspace_id = current_workspace_id()
		self._close_popup()
		self.summary_text.setPlainText("요약을 불러오는 중입니다...")

		# get_document_summary is a blocking HTTP call — run it off the UI
		# thread so navigating to this page never freezes. The token guards
		# against an out-of-order completion overwriting a newer refresh.
		self._summary_token += 1
		token = self._summary_token
		workspace_id = self._workspace_id
		controller = self._controller

		def _load() -> str:
			return controller.get_document_summary(workspace_id)

		def _apply(summary: object) -> None:
			if token != self._summary_token:
				return
			text = str(summary or "")
			if text.strip():
				# Linkify the [doc_NNN] markers before rendering so each becomes
				# a clickable citation; the final.md source is left untouched.
				apply_markdown(self.summary_text, linkify_citations(text))
			else:
				self.summary_text.setPlainText(
					"아직 표시할 final.md가 없습니다. 조사 섹션에서 AutoSurvey를 먼저 실행하세요."
				)

		def _failed(message: str) -> None:
			if token != self._summary_token:
				return
			self.summary_text.setPlainText(f"API 요청 실패: {message}")

		get_job_manager().run_detached(_load, on_success=_apply, on_error=_failed)

	# -- citation click handling --------------------------------------------
	def _on_anchor_clicked(self, url: QUrl) -> None:
		"""Resolve a clicked citation link to its source snippet in a popup.

		Non-citation links (a real http(s) URL in the report) open in the
		system browser, preserving normal link behaviour.
		"""
		parsed = parse_citation_url(url.toString(QUrl.FullyEncoded))
		if parsed is None:
			if url.scheme() in ("http", "https"):
				QDesktopServices.openUrl(url)
			return

		doc_id, claim = parsed
		# Capture the cursor position now; the popup opens there once the async
		# lookup returns.
		anchor_pos = QCursor.pos()
		self._citation_token += 1
		token = self._citation_token
		workspace_id = self._workspace_id
		controller = self._controller

		def _load() -> dict:
			return controller.get_document_citation(workspace_id, doc_id, claim)

		def _show(payload: object) -> None:
			if token != self._citation_token:
				return
			data = payload if isinstance(payload, dict) else {}
			self._open_popup(data, anchor_pos)

		def _error(message: str) -> None:
			if token != self._citation_token:
				return
			self._open_popup({"_error": message}, anchor_pos)

		get_job_manager().run_detached(_load, on_success=_show, on_error=_error)

	def _open_popup(self, payload: dict, global_pos) -> None:
		self._close_popup()
		popup = CitationPopup(self.window())
		popup.set_payload(payload)
		popup.show_at(global_pos)
		self._popup = popup

	def _close_popup(self) -> None:
		if self._popup is not None:
			self._popup.close()
			self._popup = None


class CitationPopup(QFrame):
	"""Small floating card that previews a citation's source paragraph.

	Built with ``Qt.Popup`` so Qt closes it automatically the moment the user
	clicks anywhere else in the app — no manual focus tracking needed. Capped
	in size and never blocks the UI (the HTTP lookup already ran off-thread).
	"""

	_HIGHLIGHT = "#fff3a3"

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
		self.setObjectName("CitationPopup")
		self.setMaximumSize(520, 360)
		self.setMinimumWidth(320)
		self.setStyleSheet(
			"#CitationPopup { background: #FFFFFF; border: 1px solid #D7DCE5;"
			" border-radius: 10px; }"
			" QLabel#citHeader { color: #0E1726; }"
			" QTextBrowser#citBody { border: none; background: transparent; }"
		)

		layout = QVBoxLayout(self)
		layout.setContentsMargins(14, 12, 14, 12)
		layout.setSpacing(8)

		self._header = QLabel()
		self._header.setObjectName("citHeader")
		self._header.setTextFormat(Qt.RichText)
		self._header.setWordWrap(True)
		self._header.setOpenExternalLinks(True)
		layout.addWidget(self._header)

		self._body = QTextBrowser()
		self._body.setObjectName("citBody")
		self._body.setOpenExternalLinks(True)
		layout.addWidget(self._body, 1)

	def set_payload(self, payload: dict) -> None:
		if payload.get("_error"):
			self._header.setText("<b>출처 미리보기</b>")
			self._body.setHtml(
				self._wrap(f'<p style="color:#B4232A;">불러오기 실패: '
				f"{html.escape(str(payload['_error']))}</p>")
			)
			return

		title = str(payload.get("title") or payload.get("docId") or "출처")
		domain = str(payload.get("domain") or "")
		url = str(payload.get("url") or "")
		doc_id = str(payload.get("docId") or "")

		meta_bits = [html.escape(doc_id)] if doc_id else []
		if domain:
			if url:
				meta_bits.append(
					f'<a href="{html.escape(url, quote=True)}"'
					f' style="color:#2E5BFF; text-decoration:none;">'
					f"{html.escape(domain)}</a>"
				)
			else:
				meta_bits.append(html.escape(domain))
		meta = " · ".join(meta_bits)
		self._header.setText(
			f'<div style="font-weight:700; font-size:14px;">{html.escape(title)}</div>'
			+ (f'<div style="color:#8A94A6; font-size:12px;">{meta}</div>' if meta else "")
		)

		self._body.setHtml(self._wrap(self._body_html(payload)))

	def _body_html(self, payload: dict) -> str:
		claim = str(payload.get("claim") or "")
		match = payload.get("match")
		parts: list[str] = []
		if claim:
			parts.append(
				f'<p style="color:#8A94A6; font-size:12px; margin:0 0 8px 0;">'
				f"인용: {html.escape(claim)}</p>"
			)

		if not isinstance(match, dict):
			# No reliable sentence-level anchor. When a document was still
			# resolved (metadata present), say so honestly rather than
			# highlighting an unrelated "closest" sentence.
			if str(payload.get("resolution") or "") == "document_only" and (
				payload.get("title") or payload.get("url")
			):
				parts.append(
					'<p style="color:#475569;">이 인용은 문서 수준 근거로 연결되었지만,'
					" 정확한 원문 문장 위치는 확정하지 못했습니다.</p>"
				)
			else:
				parts.append(
					'<p style="color:#475569;">원문 위치를 확정하지 못했습니다.</p>'
				)
			return "".join(parts)

		if str(match.get("matchSource") or "") == "batch_anchor":
			parts.append(
				'<p style="color:#8A94A6; font-size:12px; margin:0 0 6px 0;">'
				"이 인용을 뒷받침하는 가장 가까운 원문 근거 문장입니다.</p>"
			)
		elif str(match.get("confidence") or "low") == "low":
			parts.append(
				'<p style="color:#C2682B; font-size:12px; margin:0 0 6px 0;">'
				"정확한 원문 위치를 확정하지 못했습니다 — 가장 가까운 원문 후보입니다.</p>"
			)
		paragraph = str(match.get("paragraphText") or match.get("text") or "")
		parts.append(
			f'<div style="color:#1F2733; line-height:1.6;">'
			f"{self._highlight(paragraph, str(match.get('text') or ''))}</div>"
		)
		return "".join(parts)

	def _highlight(self, paragraph: str, sentence: str) -> str:
		"""HTML-escape *paragraph* and wrap the matched *sentence* in a mark."""
		if not paragraph:
			return ""
		escaped = html.escape(paragraph)
		needle = html.escape(sentence)
		if needle and needle in escaped:
			escaped = escaped.replace(
				needle,
				f'<span style="background-color:{self._HIGHLIGHT};">{needle}</span>',
				1,
			)
		return escaped

	def _wrap(self, inner: str) -> str:
		return (
			"<div style=\"font-family:'Segoe UI','Malgun Gothic',sans-serif;"
			' font-size:13px;">' + inner + "</div>"
		)

	def show_at(self, global_pos) -> None:
		"""Place the popup just below the click, clamped to the screen."""
		self.adjustSize()
		x = global_pos.x() + 12
		y = global_pos.y() + 12
		screen = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
		if screen is not None:
			geo = screen.availableGeometry()
			x = min(x, geo.right() - self.width() - 8)
			x = max(x, geo.left() + 8)
			y = min(y, geo.bottom() - self.height() - 8)
			y = max(y, geo.top() + 8)
		self.move(x, y)
		self.show()
