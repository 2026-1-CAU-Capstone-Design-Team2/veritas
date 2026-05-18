"""Verification page — drives the verification pipeline and shows its output.

Architecturally mirrors :mod:`research_page` so the two long-running operations
behave the same from a user perspective:

* a single **검증 시작 / 재검증** button submits the job through :class:`JobManager`
  (so the UI thread never blocks and other pages stay interactive);
* a background :class:`VerifyProgressPoller` (``QThread``) reads
  ``/api/v1/verify/progress`` with cursor semantics and pushes events to the
  shared :class:`ResearchProgressBar`;
* result rendering is split out of the page (:class:`VerifyResultsView`,
  :class:`VerifyDetailDialog`) so each piece has one job (§1.1 spirit).

The three view states — *no results yet*, *in flight*, *results loaded* —
swap a single content area so the header / progress bar stay continuous.
"""

from __future__ import annotations

from functools import partial
from math import ceil
from typing import Any

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
	QDialog,
	QDialogButtonBox,
	QFrame,
	QHBoxLayout,
	QLabel,
	QScrollArea,
	QSizePolicy,
	QToolButton,
	QVBoxLayout,
	QWidget,
)

from ...api_common import current_workspace_id
from ...components.badges import Badge
from ...components.buttons import AppButton
from ...components.cards import CardWidget, DocumentCard
from ...components.progress import ResearchProgressBar
from ...controllers import AgentController, JobCategory, get_job_manager

_FILTERS = ("전체", "높음", "중간", "낮음")
# Verification has three task pipelines; reaching "검증 완료" maps to 100%.
# Stages emitted in order: queued → start → sections → intent → consensus →
# persisting → completed. Hold a small floor for the prep stages so the bar
# is not pinned at 0 while we wait for the first task to finish.
_STAGE_PROGRESS = {
	"queued": 4.0,
	"start": 8.0,
	"sections": 35.0,
	"intent": 65.0,
	"consensus": 90.0,
	"persisting": 96.0,
	"completed": 100.0,
}


class VerifyProgressPoller(QThread):
	"""Polls ``/api/v1/verify/progress`` on a background thread.

	Same shape as :class:`ResearchProgressPoller`: cursor-based, primes on the
	first response (skipping whatever the previous run left in the buffer),
	and emits a ``reset_detected`` signal when the backend sequence rewinds
	(a new run starting).
	"""

	events = Signal(list)
	reset_detected = Signal()

	def __init__(self, parent: QObject | None = None) -> None:
		super().__init__(parent)
		self._cursor = 0
		self._stop = False
		self._sleep_ms = 800
		self._primed = False

	def request_stop(self) -> None:
		self._stop = True

	def reset(self) -> None:
		self._cursor = 0
		self._primed = False

	def run(self) -> None:  # type: ignore[override]
		while not self._stop:
			try:
				response = AgentController().get_verify_progress(
					since=self._cursor, limit=100
				)
				if isinstance(response, dict):
					latest_seq = int(response.get("latestSeq") or 0)
					if not self._primed:
						self._primed = True
						self._cursor = latest_seq
					else:
						if latest_seq < self._cursor:
							self._cursor = 0
							self.reset_detected.emit()
							response = AgentController().get_verify_progress(
								since=0, limit=100
							)
						items = response.get("items", []) if isinstance(response, dict) else []
						if isinstance(items, list) and items:
							self._cursor = int(
								response.get("nextCursor") or self._cursor
							)
							valid = [it for it in items if isinstance(it, dict)]
							if valid:
								self.events.emit(valid)
			except Exception:
				# Network blips are non-fatal — the next tick retries.
				pass
			elapsed = 0
			while not self._stop and elapsed < self._sleep_ms:
				self.msleep(100)
				elapsed += 100


class _ClickableLabel(QLabel):
	"""QLabel that emits ``clicked`` on left-click and uses a pointer cursor."""

	clicked = Signal()

	def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
		super().__init__(text, parent)
		self.setCursor(Qt.PointingHandCursor)

	def mousePressEvent(self, event) -> None:  # type: ignore[override]
		if event.button() == Qt.LeftButton:
			self.clicked.emit()
			event.accept()
			return
		super().mousePressEvent(event)


class _SummaryStripe(QFrame):
	"""Headline counts: 평균 일치율 · 신뢰도 분포 · 점검 필요 항목.

	The "점검 필요 항목" stat is clickable: clicking it asks the page to open
	the issues dialog. When no issues exist (or no verification has run yet)
	the click is a no-op and the styling stays subdued.
	"""

	issuesClicked = Signal()

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("VerifySummaryStripe")
		self.setStyleSheet(
			"QFrame#VerifySummaryStripe { background-color: #F8FAFC; "
			"border: 1px solid #E2E8F0; border-radius: 12px; }"
			"QLabel#SummaryCaption { color: #64748B; font-size: 11px; font-weight: 700; }"
			"QLabel#SummaryValue { color: #0F172A; font-size: 20px; font-weight: 800; }"
			"QLabel#SummaryValueClickable { color: #4F46E5; font-size: 20px; font-weight: 800; text-decoration: underline; }"
			"QLabel#SummaryHint { color: #64748B; font-size: 12px; font-weight: 600; }"
			"QLabel#SummaryActionHint { color: #4F46E5; font-size: 10px; font-weight: 700; }"
		)
		row = QHBoxLayout(self)
		row.setContentsMargins(16, 12, 16, 12)
		row.setSpacing(18)

		self._average = self._stat("평균 일치율", "—")
		self._high = self._stat("높음", "—")
		self._medium = self._stat("중간", "—")
		self._low = self._stat("낮음", "—")
		self._issues = self._stat("점검 필요 항목", "—", clickable=True, action_hint="자세히 보기")
		self._issues["value"].clicked.connect(self.issuesClicked)

		for stat in (self._average, self._high, self._medium, self._low, self._issues):
			row.addLayout(stat["layout"])
			row.addStretch(0)
		row.addStretch(1)

		self._hint = QLabel("")
		self._hint.setObjectName("SummaryHint")
		self._hint.setWordWrap(True)
		row.addWidget(self._hint, 0, Qt.AlignRight | Qt.AlignVCenter)

		self._issue_count = 0

	def _stat(
		self,
		caption: str,
		value: str,
		*,
		clickable: bool = False,
		action_hint: str = "",
	) -> dict[str, Any]:
		layout = QVBoxLayout()
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(2)
		caption_label = QLabel(caption)
		caption_label.setObjectName("SummaryCaption")
		if clickable:
			value_label: QLabel = _ClickableLabel(value)
			value_label.setObjectName("SummaryValueClickable")
		else:
			value_label = QLabel(value)
			value_label.setObjectName("SummaryValue")
		layout.addWidget(caption_label)
		layout.addWidget(value_label)
		action_label: QLabel | None = None
		if clickable and action_hint:
			action_label = QLabel(action_hint)
			action_label.setObjectName("SummaryActionHint")
			layout.addWidget(action_label)
		return {
			"layout": layout,
			"caption": caption_label,
			"value": value_label,
			"action": action_label,
		}

	def apply(self, summary: dict[str, Any] | None) -> None:
		if not summary or not summary.get("available"):
			for stat in (self._average, self._high, self._medium, self._low, self._issues):
				stat["value"].setText("—")
			self._hint.setText("")
			self._issue_count = 0
			self._update_issues_affordance()
			return
		avg = int(summary.get("averageMatchPercent") or 0)
		self._average["value"].setText(f"{avg}%")
		self._high["value"].setText(str(int(summary.get("highCount") or 0)))
		self._medium["value"].setText(str(int(summary.get("mediumCount") or 0)))
		self._low["value"].setText(str(int(summary.get("lowCount") or 0)))
		self._issue_count = (
			int(summary.get("underweightedSectionCount") or 0)
			+ int(summary.get("intentGapCount") or 0)
			+ int(summary.get("conflictCount") or 0)
		)
		self._issues["value"].setText(str(self._issue_count))
		updated = summary.get("updatedAt")
		if updated:
			self._hint.setText(f"마지막 검증: {str(updated).replace('T', ' ').split('.')[0]} UTC")
		else:
			self._hint.setText("")
		self._update_issues_affordance()

	def _update_issues_affordance(self) -> None:
		"""Show the '자세히 보기' hint only when there is something to drill into."""
		action = self._issues.get("action")
		if action is None:
			return
		if self._issue_count > 0:
			action.setText("자세히 보기")
			action.setVisible(True)
			self._issues["value"].setCursor(Qt.PointingHandCursor)
		else:
			action.setVisible(False)
			self._issues["value"].setCursor(Qt.ArrowCursor)


class VerifyDetailDialog(QDialog):
	"""Modal drill-down for one document.

	Splits the per-doc detail into three sections — overall · 섹션별 · 의도 facet
	— so the user can quickly see *which* report sections are weak and *which*
	intent facets are uncovered, without needing to read percentages on the card.
	"""

	def __init__(self, payload: dict[str, Any], parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("VerifyDetailDialog")
		self.setWindowTitle("검증 상세")
		self.setModal(True)
		self.resize(680, 600)
		self.setStyleSheet(
			"""
			QDialog#VerifyDetailDialog { background-color: #F8FAFC; }
			QFrame#VerifyDialogHeader {
				background-color: #FFFFFF; border: 1px solid #E2E8F0;
				border-radius: 12px;
			}
			QFrame#DetailSection {
				background-color: #FFFFFF; border: 1px solid #E5E7EB;
				border-radius: 10px;
			}
			QLabel#DetailSectionTitle {
				color: #0F172A; font-size: 13px; font-weight: 800;
			}
			QLabel#DetailRowTitle { color: #1F2937; font-size: 12px; font-weight: 700; }
			QLabel#DetailRowMeta  { color: #64748B; font-size: 11px; font-weight: 600; }
			QLabel#DetailScore    { color: #4F46E5; font-size: 12px; font-weight: 800; }
			QLabel#IssueNumber {
				background-color: #EEF2FF; border: 1px solid #C7D2FE;
				border-radius: 12px; color: #3730A3; font-size: 11px;
				font-weight: 800; padding: 3px 8px;
			}
			"""
		)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(18, 18, 18, 18)
		layout.setSpacing(12)

		layout.addWidget(self._header(payload))

		scroll = QScrollArea()
		scroll.setWidgetResizable(True)
		scroll.setFrameShape(QFrame.NoFrame)
		scroll.setObjectName("PageScroll")
		container = QWidget()
		container_layout = QVBoxLayout(container)
		container_layout.setContentsMargins(0, 0, 0, 0)
		container_layout.setSpacing(10)

		container_layout.addWidget(self._issue_block(payload))
		container_layout.addWidget(
			self._breakdown_block("보고서 섹션별 근거 강도", payload.get("sectionBreakdown") or [])
		)
		container_layout.addWidget(
			self._breakdown_block("의도 주제별 커버", payload.get("facetBreakdown") or [])
		)
		container_layout.addStretch(1)

		scroll.setWidget(container)
		layout.addWidget(scroll, 1)

		buttons = QDialogButtonBox(QDialogButtonBox.Close)
		buttons.rejected.connect(self.reject)
		buttons.accepted.connect(self.accept)
		layout.addWidget(buttons)

	def _header(self, payload: dict[str, Any]) -> QFrame:
		header = QFrame()
		header.setObjectName("VerifyDialogHeader")
		header_layout = QVBoxLayout(header)
		header_layout.setContentsMargins(16, 14, 16, 14)
		header_layout.setSpacing(8)

		title_row = QHBoxLayout()
		title_row.setSpacing(8)
		title_label = QLabel(str(payload.get("title") or payload.get("docId") or "문서"))
		title_label.setObjectName("CardTitle")
		title_label.setWordWrap(True)
		title_row.addWidget(title_label, 1)
		level = str(payload.get("level") or "")
		title_row.addWidget(Badge(level, _tone_for_level(level)), 0, Qt.AlignTop)
		header_layout.addLayout(title_row)

		meta_text = str(payload.get("matchRate") or "")
		meta = QLabel(meta_text)
		meta.setObjectName("CardSecondary")
		meta.setWordWrap(True)
		header_layout.addWidget(meta)

		summary = QLabel(_summary_for_level(level))
		summary.setObjectName("CardPrimary")
		summary.setWordWrap(True)
		header_layout.addWidget(summary)
		return header

	def _issue_block(self, payload: dict[str, Any]) -> QFrame:
		frame = QFrame()
		frame.setObjectName("DetailSection")
		layout = QVBoxLayout(frame)
		layout.setContentsMargins(14, 12, 14, 12)
		layout.setSpacing(8)
		title = QLabel("자동 점검 의견")
		title.setObjectName("DetailSectionTitle")
		layout.addWidget(title)

		issues = payload.get("issues") or []
		if not issues:
			empty = QLabel("특이사항 없이 통과되었습니다.")
			empty.setObjectName("CardSecondary")
			empty.setWordWrap(True)
			layout.addWidget(empty)
			return frame
		for index, text in enumerate(issues, start=1):
			row = QFrame()
			row_layout = QHBoxLayout(row)
			row_layout.setContentsMargins(0, 0, 0, 0)
			row_layout.setSpacing(10)
			number = QLabel(str(index))
			number.setObjectName("IssueNumber")
			body = QLabel(str(text))
			body.setObjectName("CardSecondary")
			body.setWordWrap(True)
			body.setTextInteractionFlags(
				Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
			)
			row_layout.addWidget(number, 0, Qt.AlignTop)
			row_layout.addWidget(body, 1)
			layout.addWidget(row)
		return frame

	def _breakdown_block(self, title_text: str, rows: list[dict[str, Any]]) -> QFrame:
		frame = QFrame()
		frame.setObjectName("DetailSection")
		layout = QVBoxLayout(frame)
		layout.setContentsMargins(14, 12, 14, 12)
		layout.setSpacing(8)
		title = QLabel(title_text)
		title.setObjectName("DetailSectionTitle")
		layout.addWidget(title)

		if not rows:
			empty = QLabel("분석 결과가 없습니다.")
			empty.setObjectName("CardSecondary")
			layout.addWidget(empty)
			return frame

		# Workspace-internal min/max for the small inline bar — visualizes
		# this row's score against the doc's own range so the user can spot
		# strong/weak rows without reading floats.
		scores = [float(row.get("score") or 0.0) for row in rows]
		hi = max(scores) if scores else 0.0
		for row in rows[:8]:
			layout.addWidget(self._breakdown_row(row, hi))
		return frame

	def _breakdown_row(self, row: dict[str, Any], hi: float) -> QFrame:
		frame = QFrame()
		row_layout = QVBoxLayout(frame)
		row_layout.setContentsMargins(0, 4, 0, 4)
		row_layout.setSpacing(4)
		labels = ", ".join(str(t) for t in (row.get("labels") or []) if str(t).strip())
		if not labels:
			labels = "—"
		head = QHBoxLayout()
		head.setContentsMargins(0, 0, 0, 0)
		head.setSpacing(8)
		title_label = QLabel(labels)
		title_label.setObjectName("DetailRowTitle")
		title_label.setWordWrap(True)
		head.addWidget(title_label, 1)
		score_value = float(row.get("score") or 0.0)
		score_label = QLabel(f"{int(round(_normalize_score(score_value, hi) * 100))}%")
		score_label.setObjectName("DetailScore")
		head.addWidget(score_label, 0, Qt.AlignRight)
		row_layout.addLayout(head)
		bar = _MiniBar(_normalize_score(score_value, hi))
		row_layout.addWidget(bar)
		return frame


class _MiniBar(QWidget):
	"""Tiny horizontal score bar (used inside the detail dialog)."""

	def __init__(self, ratio: float, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._ratio = max(0.0, min(1.0, float(ratio)))
		self.setFixedHeight(6)
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

	def paintEvent(self, event) -> None:  # type: ignore[override]
		from PySide6.QtGui import QColor, QPainter

		painter = QPainter(self)
		painter.setRenderHint(QPainter.Antialiasing, True)
		painter.fillRect(self.rect(), QColor("#E2E8F0"))
		if self._ratio > 0.0:
			fill_width = max(2, int(self.width() * self._ratio))
			painter.fillRect(0, 0, fill_width, self.height(), QColor("#6366F1"))


def _normalize_score(value: float, hi: float) -> float:
	if hi <= 0.0:
		return 0.0
	return max(0.0, min(1.0, value / hi))


# Per-issue palette: header chip + body tint. Tuned to match the badge tones
# used elsewhere on the verify page so the user reads the kind at a glance.
_ISSUE_PALETTE = {
	"underweighted_section": ("#FEF3C7", "#B45309", "#FDE68A", "근거 부족"),
	"conflict": ("#FEE2E2", "#B91C1C", "#FCA5A5", "출처 충돌"),
	"intent_gap": ("#E0E7FF", "#3730A3", "#C7D2FE", "의도 미커버"),
}

# Role chip palette for ordered flow sections (intro / body / conclusion).
# Body sits in neutral indigo, intro/conclusion get the warm/cool accents.
_ROLE_CHIP = {
	"intro": ("도입", "#DBEAFE", "#1D4ED8", "#BFDBFE"),
	"body": ("본문", "#EEF2FF", "#3730A3", "#C7D2FE"),
	"conclusion": ("마무리", "#DCFCE7", "#15803D", "#86EFAC"),
}


class VerifyIssuesDialog(QDialog):
	"""Modal listing every workspace-level "점검 필요 항목" in plain Korean.

	Three kinds are mixed in one ordered list (unmet must_cover → conflict →
	intent gap, per ``verify_view.issues_overview``). A leading chip tells the
	user what kind each row is without needing a legend.
	"""

	def __init__(self, issues: list[dict[str, Any]], parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("VerifyIssuesDialog")
		self.setWindowTitle("점검 필요 항목")
		self.setModal(True)
		self.resize(620, 540)
		self.setStyleSheet(
			"""
			QDialog#VerifyIssuesDialog { background-color: #F8FAFC; }
			QFrame#IssuesHeader {
				background-color: #FFFFFF; border: 1px solid #E2E8F0;
				border-radius: 12px;
			}
			QFrame#IssueCard {
				background-color: #FFFFFF; border: 1px solid #E5E7EB;
				border-radius: 10px;
			}
			QLabel#IssueKindChip {
				font-size: 11px; font-weight: 800;
				padding: 3px 10px; border-radius: 11px;
			}
			QLabel#IssueTitle   { color: #0F172A; font-size: 13px; font-weight: 800; }
			QLabel#IssueDetail  { color: #1F2937; font-size: 12px; font-weight: 600; }
			QLabel#IssueHint    { color: #64748B; font-size: 11px; font-weight: 600; }
			QLabel#IssueMetric  { color: #94A3B8; font-size: 11px; font-weight: 700; }
			"""
		)

		layout = QVBoxLayout(self)
		layout.setContentsMargins(18, 18, 18, 18)
		layout.setSpacing(12)

		header = QFrame()
		header.setObjectName("IssuesHeader")
		header_layout = QVBoxLayout(header)
		header_layout.setContentsMargins(16, 14, 16, 14)
		header_layout.setSpacing(6)
		title = QLabel("점검 필요 항목")
		title.setObjectName("CardTitle")
		caption = QLabel(
			"조사 결과에서 자동으로 감지된 점검 항목입니다. 각 항목은 자료 보강이"
			" 필요한 부분(근거 부족), 출처 간 입장 차이(출처 충돌), 또는 사용자 의도가"
			" 충분히 받쳐지지 않은 영역(의도 미커버)을 의미합니다."
		)
		caption.setObjectName("CardSecondary")
		caption.setWordWrap(True)
		header_layout.addWidget(title)
		header_layout.addWidget(caption)
		layout.addWidget(header)

		scroll = QScrollArea()
		scroll.setWidgetResizable(True)
		scroll.setFrameShape(QFrame.NoFrame)
		scroll.setObjectName("PageScroll")
		container = QWidget()
		container_layout = QVBoxLayout(container)
		container_layout.setContentsMargins(0, 0, 0, 0)
		container_layout.setSpacing(10)

		if not issues:
			empty = QLabel("점검이 필요한 항목이 발견되지 않았습니다.")
			empty.setObjectName("CardSecondary")
			empty.setAlignment(Qt.AlignCenter)
			empty.setWordWrap(True)
			container_layout.addWidget(empty)
		else:
			for issue in issues:
				container_layout.addWidget(self._issue_card(issue))
		container_layout.addStretch(1)

		scroll.setWidget(container)
		layout.addWidget(scroll, 1)

		buttons = QDialogButtonBox(QDialogButtonBox.Close)
		buttons.rejected.connect(self.reject)
		buttons.accepted.connect(self.accept)
		layout.addWidget(buttons)

	def _issue_card(self, issue: dict[str, Any]) -> QFrame:
		kind = str(issue.get("kind") or "")
		bg, fg, border, kind_label = _ISSUE_PALETTE.get(
			kind, ("#F1F5F9", "#475569", "#CBD5E1", "기타")
		)

		card = QFrame()
		card.setObjectName("IssueCard")
		layout = QVBoxLayout(card)
		layout.setContentsMargins(14, 12, 14, 12)
		layout.setSpacing(6)

		head = QHBoxLayout()
		head.setContentsMargins(0, 0, 0, 0)
		head.setSpacing(8)
		chip = QLabel(kind_label)
		chip.setObjectName("IssueKindChip")
		chip.setStyleSheet(
			f"QLabel#IssueKindChip {{ background-color: {bg}; color: {fg};"
			f" border: 1px solid {border}; }}"
		)
		head.addWidget(chip, 0, Qt.AlignLeft)
		metric = QLabel(str(issue.get("metric") or ""))
		metric.setObjectName("IssueMetric")
		head.addStretch(1)
		head.addWidget(metric, 0, Qt.AlignRight)
		layout.addLayout(head)

		title = QLabel(str(issue.get("title") or ""))
		title.setObjectName("IssueTitle")
		title.setWordWrap(True)
		layout.addWidget(title)

		detail = QLabel(str(issue.get("detail") or ""))
		detail.setObjectName("IssueDetail")
		detail.setWordWrap(True)
		detail.setTextInteractionFlags(
			Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
		)
		layout.addWidget(detail)

		hint = str(issue.get("hint") or "")
		if hint:
			hint_label = QLabel(hint)
			hint_label.setObjectName("IssueHint")
			hint_label.setWordWrap(True)
			layout.addWidget(hint_label)
		return card


class _SectionCard(QFrame):
	"""One ordered report-flow section, with a dedicated "자세히 보기" button.

	Renders:
	  * a left margin index chip (``1·2·…``) so the visual reading order
	    matches the report order;
	  * a role chip (``도입 / 본문 / 마무리``);
	  * the LLM-authored title + description (the labels the writer reads);
	  * a meta line with sentence and contributing-doc counts;
	  * an amber alert when too few sentences support this section.
	"""

	clicked = Signal(int)

	# A section under this many assigned sentences is flagged "근거 부족" so
	# the writer knows the outline asks for more than the corpus has.
	_UNDERWEIGHTED_MIN = 3

	def __init__(
		self,
		section: dict[str, Any],
		parent: QWidget | None = None,
	) -> None:
		super().__init__(parent)
		self._section_id = int(section.get("sectionId") or 0)
		self.setObjectName("SectionCard")
		self.setStyleSheet(
			"QFrame#SectionCard { background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 10px; }"
			"QFrame#SectionCard:hover { border-color: #6366F1; }"
			"QLabel#SectionOrderChip {"
			"  background-color: #F1F5F9; color: #1F2937;"
			"  border: 1px solid #CBD5E1; border-radius: 13px;"
			"  font-size: 12px; font-weight: 800; padding: 3px 10px;"
			"}"
			"QLabel#SectionRoleChip { font-size: 11px; font-weight: 800; padding: 3px 10px; border-radius: 11px; }"
			"QLabel#SectionTitle { color: #0F172A; font-size: 14px; font-weight: 800; }"
			"QLabel#SectionDescription { color: #475569; font-size: 12px; font-weight: 500; }"
			"QLabel#SectionMeta { color: #64748B; font-size: 11px; font-weight: 700; }"
			"QLabel#SectionAlert { color: #B45309; font-size: 11px; font-weight: 800; }"
		)
		outer = QHBoxLayout(self)
		outer.setContentsMargins(14, 12, 14, 12)
		outer.setSpacing(12)

		# Order number — visible reading order for the writer.
		order_chip = QLabel(str(int(section.get("order") or self._section_id) + 1))
		order_chip.setObjectName("SectionOrderChip")
		order_chip.setAlignment(Qt.AlignCenter)
		order_chip.setFixedWidth(36)
		outer.addWidget(order_chip, 0, Qt.AlignTop)

		body = QVBoxLayout()
		body.setContentsMargins(0, 0, 0, 0)
		body.setSpacing(4)

		# Title + role chip on one line.
		head = QHBoxLayout()
		head.setContentsMargins(0, 0, 0, 0)
		head.setSpacing(8)
		title = QLabel(str(section.get("title") or f"섹션 {self._section_id}").strip())
		title.setObjectName("SectionTitle")
		title.setWordWrap(True)
		head.addWidget(title, 1)
		role = str(section.get("role") or "body").lower()
		role_label_text, bg, fg, border = _ROLE_CHIP.get(role, _ROLE_CHIP["body"])
		role_chip = QLabel(role_label_text)
		role_chip.setObjectName("SectionRoleChip")
		role_chip.setStyleSheet(
			f"QLabel#SectionRoleChip {{ background-color: {bg}; color: {fg};"
			f" border: 1px solid {border}; }}"
		)
		head.addWidget(role_chip, 0, Qt.AlignTop)
		body.addLayout(head)

		desc_text = str(section.get("description") or "").strip()
		if desc_text:
			desc = QLabel(desc_text)
			desc.setObjectName("SectionDescription")
			desc.setWordWrap(True)
			body.addWidget(desc)

		sentence_count = int(section.get("sentenceCount") or 0)
		document_count = int(section.get("documentCount") or 0)
		meta = QLabel(f"배치된 문장 {sentence_count}개 · 관련 자료 {document_count}건")
		meta.setObjectName("SectionMeta")
		body.addWidget(meta)

		if sentence_count < self._UNDERWEIGHTED_MIN:
			alert = QLabel(
				"● 자료에서 충분히 확인되지 않은 섹션입니다 — 보강이 필요합니다."
			)
			alert.setObjectName("SectionAlert")
			alert.setWordWrap(True)
			body.addWidget(alert)

		outer.addLayout(body, 1)

		detail_btn = AppButton("자세히 보기", variant="ghost")
		detail_btn.setObjectName("VerifyDetailButton")
		detail_btn.setFixedHeight(28)
		detail_btn.setFixedWidth(92)
		detail_btn.clicked.connect(lambda: self.clicked.emit(self._section_id))
		outer.addWidget(detail_btn, 0, Qt.AlignRight | Qt.AlignVCenter)


class _SectionsPanel(CardWidget):
	"""'보고서 흐름 구조' 패널 — ordered report-flow outline from Task 1.

	Sits between the summary stripe and the per-doc results so the writer
	first reads the *report structure* (introduction → body → conclusion)
	before scrolling through per-doc ratings.
	"""

	sectionClicked = Signal(int)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(title="보고서 흐름 구조", parent=parent)
		self._caption = QLabel(
			"LLM 이 조사 의도·계획·자료 메타를 보고 정한 보고서 작성 순서입니다."
			" 각 섹션을 클릭하면 어느 자료의 어느 문장이 그 섹션에 배치되는지"
			" 확인할 수 있습니다."
		)
		self._caption.setObjectName("CardSecondary")
		self._caption.setWordWrap(True)
		self.layout.addWidget(self._caption)

		# Tiny fallback notice — switches on when ``flow_source != 'llm'``.
		self._fallback_notice = QLabel("")
		self._fallback_notice.setObjectName("CardSecondary")
		self._fallback_notice.setStyleSheet("color: #B45309; font-weight: 700;")
		self._fallback_notice.setWordWrap(True)
		self._fallback_notice.setVisible(False)
		self.layout.addWidget(self._fallback_notice)

		self._cards_layout = QVBoxLayout()
		self._cards_layout.setContentsMargins(0, 0, 0, 0)
		self._cards_layout.setSpacing(8)
		self.layout.addLayout(self._cards_layout)

	def apply(self, sections: list[dict[str, Any]], flow_source: str) -> None:
		"""Rebuild the card stack from the latest sections overview payload."""
		while self._cards_layout.count():
			item = self._cards_layout.takeAt(0)
			widget = item.widget() if item else None
			if widget is not None:
				widget.setParent(None)
				widget.deleteLater()

		if flow_source == "fallback":
			self._fallback_notice.setText(
				"⚠ LLM 흐름 계획에 실패하여 must_cover 기반 임시 구조로 대체되었습니다."
				" LLM 서버 상태를 확인 후 ‘재검증’ 을 눌러주세요."
			)
			self._fallback_notice.setVisible(True)
		else:
			self._fallback_notice.setVisible(False)

		if not sections:
			empty = QLabel("섹션 구조가 아직 분석되지 않았습니다.")
			empty.setObjectName("CardSecondary")
			empty.setAlignment(Qt.AlignCenter)
			empty.setWordWrap(True)
			self._cards_layout.addWidget(empty)
			return

		for section in sections:
			card = _SectionCard(section)
			card.clicked.connect(self.sectionClicked)
			self._cards_layout.addWidget(card)


class VerifySectionDetailDialog(QDialog):
	"""Modal showing one flow section's sentence-level evidence.

	Each row is a sentence assigned to this section — source title + the
	sentence text itself — so the writer can read the actual quote and
	decide whether to cite it directly.
	"""

	def __init__(
		self,
		section: dict[str, Any],
		doc_titles: dict[str, str],
		parent: QWidget | None = None,
	) -> None:
		super().__init__(parent)
		self.setObjectName("VerifySectionDialog")
		self.setWindowTitle("보고서 섹션 상세")
		self.setModal(True)
		self.resize(720, 600)
		self.setStyleSheet(
			"""
			QDialog#VerifySectionDialog { background-color: #F8FAFC; }
			QFrame#SectionDialogHeader {
				background-color: #FFFFFF; border: 1px solid #E2E8F0;
				border-radius: 12px;
			}
			QFrame#SentenceRow {
				background-color: #FFFFFF; border: 1px solid #E5E7EB;
				border-radius: 10px;
			}
			QLabel#SectionDialogTitle { color: #0F172A; font-size: 15px; font-weight: 800; }
			QLabel#SectionDialogDesc  { color: #475569; font-size: 12px; font-weight: 500; }
			QLabel#SectionDialogMeta  { color: #64748B; font-size: 11px; font-weight: 600; }
			QLabel#SentenceSource { color: #4F46E5; font-size: 11px; font-weight: 800; }
			QLabel#SentenceLocator { color: #94A3B8; font-size: 10px; font-weight: 700; }
			QLabel#SentenceBody { color: #1F2937; font-size: 13px; font-weight: 500; }
			QLabel#SectionDialogRoleChip { font-size: 11px; font-weight: 800; padding: 3px 10px; border-radius: 11px; }
			"""
		)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(18, 18, 18, 18)
		layout.setSpacing(12)

		layout.addWidget(self._header(section))

		body_title = QLabel("이 섹션에 배치된 문장들")
		body_title.setObjectName("CardTitle")
		layout.addWidget(body_title)

		caption = QLabel(
			"각 문장은 ‘출처 자료 → 문장 위치 → 본문’ 형태로 표시됩니다."
			" 보고서 작성 시 그대로 인용하거나 paraphrase 의 근거로 사용할 수 있습니다."
		)
		caption.setObjectName("CardSecondary")
		caption.setWordWrap(True)
		layout.addWidget(caption)

		scroll = QScrollArea()
		scroll.setWidgetResizable(True)
		scroll.setFrameShape(QFrame.NoFrame)
		scroll.setObjectName("PageScroll")
		container = QWidget()
		container_layout = QVBoxLayout(container)
		container_layout.setContentsMargins(0, 0, 0, 0)
		container_layout.setSpacing(8)

		assignments = section.get("sentenceAssignments") or []
		if not assignments:
			empty = QLabel("이 섹션에 배치된 문장이 없습니다. 자료 보강이 필요합니다.")
			empty.setObjectName("CardSecondary")
			empty.setWordWrap(True)
			container_layout.addWidget(empty)
		else:
			for index, assignment in enumerate(assignments, start=1):
				container_layout.addWidget(
					self._sentence_row(index, assignment, doc_titles)
				)
		container_layout.addStretch(1)
		scroll.setWidget(container)
		layout.addWidget(scroll, 1)

		buttons = QDialogButtonBox(QDialogButtonBox.Close)
		buttons.rejected.connect(self.reject)
		buttons.accepted.connect(self.accept)
		layout.addWidget(buttons)

	def _header(self, section: dict[str, Any]) -> QFrame:
		header = QFrame()
		header.setObjectName("SectionDialogHeader")
		header_layout = QVBoxLayout(header)
		header_layout.setContentsMargins(16, 14, 16, 14)
		header_layout.setSpacing(6)

		title_row = QHBoxLayout()
		title_row.setContentsMargins(0, 0, 0, 0)
		title_row.setSpacing(8)
		title = QLabel(str(section.get("title") or f"섹션 {section.get('sectionId')}"))
		title.setObjectName("SectionDialogTitle")
		title.setWordWrap(True)
		title_row.addWidget(title, 1)
		role = str(section.get("role") or "body").lower()
		role_label_text, bg, fg, border = _ROLE_CHIP.get(role, _ROLE_CHIP["body"])
		role_chip = QLabel(role_label_text)
		role_chip.setObjectName("SectionDialogRoleChip")
		role_chip.setStyleSheet(
			f"QLabel#SectionDialogRoleChip {{ background-color: {bg}; color: {fg};"
			f" border: 1px solid {border}; }}"
		)
		title_row.addWidget(role_chip, 0, Qt.AlignTop)
		header_layout.addLayout(title_row)

		desc_text = str(section.get("description") or "").strip()
		if desc_text:
			desc = QLabel(desc_text)
			desc.setObjectName("SectionDialogDesc")
			desc.setWordWrap(True)
			header_layout.addWidget(desc)

		meta = QLabel(
			f"순서 {int(section.get('order') or 0) + 1}"
			f" · 배치 문장 {int(section.get('sentenceCount') or 0)}개"
			f" · 관련 자료 {int(section.get('documentCount') or 0)}건"
		)
		meta.setObjectName("SectionDialogMeta")
		header_layout.addWidget(meta)
		return header

	def _sentence_row(
		self,
		index: int,
		assignment: dict[str, Any],
		doc_titles: dict[str, str],
	) -> QFrame:
		row = QFrame()
		row.setObjectName("SentenceRow")
		layout = QVBoxLayout(row)
		layout.setContentsMargins(12, 10, 12, 10)
		layout.setSpacing(4)

		head = QHBoxLayout()
		head.setContentsMargins(0, 0, 0, 0)
		head.setSpacing(8)
		number = QLabel(str(index))
		number.setObjectName("IssueNumber")
		head.addWidget(number, 0, Qt.AlignTop)
		doc_id = str(assignment.get("docId") or "")
		source = QLabel(doc_titles.get(doc_id, f"문서 {doc_id}"))
		source.setObjectName("SentenceSource")
		source.setWordWrap(True)
		head.addWidget(source, 1)
		locator = QLabel(
			f"문단 {int(assignment.get('paragraphIndex') or 0) + 1}"
			f" · 문장 {int(assignment.get('sentenceIndex') or 0) + 1}"
		)
		locator.setObjectName("SentenceLocator")
		head.addWidget(locator, 0, Qt.AlignRight | Qt.AlignTop)
		layout.addLayout(head)

		body = QLabel(str(assignment.get("text") or ""))
		body.setObjectName("SentenceBody")
		body.setWordWrap(True)
		body.setTextInteractionFlags(
			Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
		)
		layout.addWidget(body)
		return row


class _EmptyStateCard(CardWidget):
	"""Large, obvious 'no verification yet' card.

	Replaces the previous tiny PageSubtitle label so a user landing on the page
	for the first time immediately sees what to do (and why nothing else is
	rendered). Shown only when the workspace has no saved verification — once
	the run completes the card is dropped and the real results render.
	"""

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(title=None, parent=parent)
		self.setObjectName("VerifyEmptyState")
		self.setStyleSheet(
			"QFrame#VerifyEmptyState { background-color: #F1F5F9; "
			"border: 1px dashed #94A3B8; border-radius: 12px; }"
			"QLabel#VerifyEmptyTitle { color: #0F172A; font-size: 16px; font-weight: 800; }"
			"QLabel#VerifyEmptyBody  { color: #475569; font-size: 13px; font-weight: 500; }"
			"QLabel#VerifyEmptyHint  { color: #64748B; font-size: 12px; font-weight: 600; }"
		)
		title = QLabel("이 워크스페이스에는 아직 검증 결과가 없습니다.")
		title.setObjectName("VerifyEmptyTitle")
		title.setWordWrap(True)
		body = QLabel(
			"상단 ‘검증 시작’ 버튼을 누르면 조사 결과를 자동으로 분석합니다.\n"
			"분석은 보통 15초~1분 정도 걸리며, 그동안 다른 페이지를 자유롭게 사용할 수 있습니다."
		)
		body.setObjectName("VerifyEmptyBody")
		body.setWordWrap(True)
		hint = QLabel(
			"검증을 위해서는 조사가 이미 완료되어 ‘summary/’ 와 ‘chromadb/’ 가 만들어진"
			" 워크스페이스여야 합니다. 막 만든 워크스페이스에서 조사가 진행 중이라면"
			" 완료된 후 다시 시도해 주세요."
		)
		hint.setObjectName("VerifyEmptyHint")
		hint.setWordWrap(True)
		self.layout.addWidget(title)
		self.layout.addWidget(body)
		self.layout.addWidget(hint)


def _tone_for_level(level: str) -> str:
	if level == "높음":
		return "success"
	if level == "중간":
		return "warning"
	if level == "낮음":
		return "danger"
	return "default"


def _summary_for_level(level: str) -> str:
	if level == "높음":
		return "이 자료는 조사 의도와 맞물려 잘 활용할 수 있습니다."
	if level == "중간":
		return "일부 주제 커버가 약하므로 활용 시 추가 자료와 함께 확인하세요."
	if level == "낮음":
		return "신뢰도 보강이 필요합니다. 인용 전 추가 출처로 교차 확인을 권장합니다."
	return "검증 결과를 확인해 주세요."


class VerifyPage(QWidget):
	"""검증 페이지 — runs verification asynchronously, renders the saved results.

	Page life-cycle:
	1. ``showEvent`` (or :meth:`set_workspace`) loads the saved summary +
	   results for the current workspace; if none exist the empty-state hint
	   is shown.
	2. *"검증 시작 / 재검증"* dispatches the run through :class:`JobManager`
	   and starts the progress poller. The progress bar updates from the live
	   events.
	3. On success the page reloads from the persisted JSON — i.e. the final
	   render comes from the same code path as a workspace already verified.
	"""

	# Mirror of ResearchPage.workspaceChanged: emitted when the user switches
	# workspace from this page (none yet, but keeps the signature uniform if
	# we add it later).
	workspaceChanged = Signal(str)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._controller = AgentController()
		self._workspace_id = current_workspace_id()
		self._active_filter = "전체"
		self._page_size = 5
		self._current_page = 0
		self._items: list[dict[str, Any]] = []
		self._summary: dict[str, Any] | None = None
		self._progress_poller: VerifyProgressPoller | None = None
		# True between job submission and the controller's success/error callback.
		# Drives the run button label / availability.
		self._busy = False

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(12)

		# Header card with the run/re-run button + workspace label.
		header_card = CardWidget("검증")
		subtitle = QLabel(
			"조사 결과를 자동 분석하여 자료별 의도 일치율, 보고서 섹션 커버리지,"
			" 그리고 출처 간 합의/충돌을 확인합니다."
		)
		subtitle.setObjectName("PageSubtitle")
		subtitle.setWordWrap(True)
		header_card.layout.addWidget(subtitle)

		action_row = QHBoxLayout()
		action_row.setContentsMargins(0, 0, 0, 0)
		action_row.setSpacing(10)
		self._workspace_label = QLabel("")
		self._workspace_label.setObjectName("CardSecondary")
		action_row.addWidget(self._workspace_label, 1)
		self._run_button = AppButton("검증 시작", variant="primary")
		self._run_button.setObjectName("VerifyRunButton")
		self._run_button.clicked.connect(self._on_run_clicked)
		action_row.addWidget(self._run_button, 0, Qt.AlignRight)
		header_card.layout.addLayout(action_row)
		root.addWidget(header_card)

		# Live progress bar (reused from research) — invisible until a run starts.
		self._progress_bar = ResearchProgressBar()
		self._progress_bar.set_idle()
		root.addWidget(self._progress_bar)

		# Summary stripe (counts + averages). The "점검 필요 항목" stat is
		# clickable — :class:`VerifyIssuesDialog` opens with the full list.
		summary_caption = QLabel(
			"‘평균 일치율’ 은 이 워크스페이스의 가장 잘 매칭된 자료를 100% 로 두고"
			" 각 자료가 그 대비 얼마나 매칭되는지를 평균낸 값입니다 — 워크스페이스"
			" 자료들의 평균 품질에 비례합니다 (모든 자료가 균등하게 잘 매칭되면 100% 에"
			" 가깝고, 한두 자료만 강하게 매칭되면 낮아집니다)."
		)
		summary_caption.setObjectName("CardSecondary")
		summary_caption.setWordWrap(True)
		root.addWidget(summary_caption)

		self._summary_stripe = _SummaryStripe()
		self._summary_stripe.issuesClicked.connect(self._on_issues_clicked)
		root.addWidget(self._summary_stripe)

		# Large empty-state card — only visible when this workspace has no
		# saved verification yet. Hidden once results render.
		self._empty_state_card = _EmptyStateCard()
		root.addWidget(self._empty_state_card)

		# Auto-identified report sections (Task 1) — surfaced *between* the
		# summary stripe and per-doc cards so the user reads the structure
		# before scrolling through individual ratings.
		self._sections_panel = _SectionsPanel()
		self._sections_panel.sectionClicked.connect(self._on_section_clicked)
		root.addWidget(self._sections_panel)

		# Per-doc results header + filter chips.
		results_header = CardWidget("자료별 검증 결과")
		results_header_caption = QLabel(
			"각 자료의 ‘일치율 %’는 이 워크스페이스에서 가장 잘 매칭된 자료를 100% 로 두고"
			" 그 대비 비율을 보여줍니다.  ≥ 70% → 높음, 40 ~ 70% → 중간, < 40% → 낮음."
			" 아래 칩을 누르면 해당 단계의 자료만 추려서 볼 수 있고, ‘상세 보기’ 로"
			" 그 자료가 약하게 다룬 섹션·의도 주제를 확인할 수 있습니다."
		)
		results_header_caption.setObjectName("CardSecondary")
		results_header_caption.setWordWrap(True)
		results_header.layout.addWidget(results_header_caption)

		filter_row = QHBoxLayout()
		filter_row.setSpacing(8)
		self._filter_buttons: dict[str, QToolButton] = {}
		for label in _FILTERS:
			chip = QToolButton()
			chip.setObjectName("VerifyFilterChip")
			chip.setText(label)
			chip.setCheckable(True)
			chip.setCursor(Qt.PointingHandCursor)
			chip.setFocusPolicy(Qt.NoFocus)
			chip.clicked.connect(partial(self._set_filter, label))
			filter_row.addWidget(chip)
			self._filter_buttons[label] = chip
		filter_row.addStretch(1)
		# Chip stylesheet — checked = brand color, unchecked = subdued. Applied
		# on the filter row so chip count widgets inherit the same palette.
		results_header.setStyleSheet(
			"QToolButton#VerifyFilterChip {"
			"  background-color: #F1F5F9; color: #475569;"
			"  border: 1px solid #E2E8F0; border-radius: 14px;"
			"  padding: 5px 12px; font-size: 12px; font-weight: 700;"
			"}"
			"QToolButton#VerifyFilterChip:hover { border-color: #CBD5E1; }"
			"QToolButton#VerifyFilterChip:checked {"
			"  background-color: #4F46E5; color: #FFFFFF;"
			"  border: 1px solid #4338CA;"
			"}"
		)
		self._filter_buttons[self._active_filter].setChecked(True)
		results_header.layout.addLayout(filter_row)
		root.addWidget(results_header)
		self._results_header = results_header

		self._content_layout = QVBoxLayout()
		self._content_layout.setContentsMargins(0, 0, 0, 0)
		self._content_layout.setSpacing(10)
		root.addLayout(self._content_layout)

		self._empty_label = QLabel("결과를 불러오는 중...")
		self._empty_label.setObjectName("PageSubtitle")
		self._empty_label.setAlignment(Qt.AlignCenter)
		self._empty_label.setWordWrap(True)
		self._content_layout.addWidget(self._empty_label)

		# Pagination row.
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

		# Job-manager-driven enabled state so a research run elsewhere disables
		# this page's run button automatically.
		get_job_manager().busy_changed.connect(self._sync_run_button)
		self._sync_run_button()
		self._update_workspace_label()
		self._refresh_data()

	# -- public API ----------------------------------------------------------

	def set_workspace_by_name(self, name: str) -> None:
		"""Alias matching the other pages' ``set_workspace_by_name`` surface.

		Workspace name and id are interchangeable in this app (the sidebar
		uses the workspace directory name as the displayed label), so this is
		a thin pass-through.
		"""
		self.set_workspace(name)

	def set_workspace(self, workspace_id: str) -> None:
		"""External hook (main_window) called when the user switches workspace."""
		workspace_id = (workspace_id or "").strip()
		if workspace_id and workspace_id != self._workspace_id:
			self._workspace_id = workspace_id
			self._stop_poller()
			self._items = []
			self._summary = None
			self._current_page = 0
			self._progress_bar.set_idle()
			self._update_workspace_label()
			self._refresh_data()
		else:
			# Same workspace: still refresh in case external code persisted new results.
			self._refresh_data()

	def showEvent(self, event) -> None:  # type: ignore[override]
		super().showEvent(event)
		latest_workspace = current_workspace_id()
		if latest_workspace and latest_workspace != self._workspace_id:
			self.set_workspace(latest_workspace)
		else:
			self._refresh_data()

	def hideEvent(self, event) -> None:  # type: ignore[override]
		# Stop polling when the page is hidden so a long-running verify in
		# another workspace doesn't keep emitting on a stale view.
		super().hideEvent(event)
		self._stop_poller()

	# -- run lifecycle --------------------------------------------------------

	def _on_run_clicked(self) -> None:
		manager = get_job_manager()
		if manager.is_blocked(JobCategory.VERIFY):
			return
		workspace_id = (self._workspace_id or "").strip() or current_workspace_id()
		if not workspace_id or workspace_id == "default":
			self._progress_bar.start("검증할 워크스페이스가 없습니다.")
			self._progress_bar.mark_failed("먼저 조사 페이지에서 조사를 진행해 주세요.")
			return

		self._busy = True
		self._sync_run_button()
		self._progress_bar.start("검증 준비 중...")
		self._start_poller()

		submitted = manager.submit(
			JobCategory.VERIFY,
			self._controller.run_verification,
			workspace_id,
			None,
			on_success=self._on_run_success,
			on_error=self._on_run_error,
			on_done=self._on_run_done,
		)
		if not submitted:
			self._busy = False
			self._sync_run_button()
			self._stop_poller()
			self._progress_bar.mark_failed("다른 작업이 진행 중입니다. 잠시 후 다시 시도해 주세요.")

	def _on_run_success(self, result: Any) -> None:
		# The progress poller will deliver a `completed` event; mark the bar
		# completed here too in case the poller missed the very last tick.
		self._progress_bar.mark_completed(animate=True)
		self._refresh_data()

	def _on_run_error(self, message: str) -> None:
		clean = " ".join(str(message or "").split()).strip() or "알 수 없는 오류"
		self._progress_bar.mark_failed(clean)

	def _on_run_done(self) -> None:
		self._busy = False
		self._sync_run_button()
		self._stop_poller()

	# -- data loading ---------------------------------------------------------

	def _refresh_data(self) -> None:
		"""Reload summary + results for the active workspace.

		Runs synchronously on the UI thread because both endpoints are
		filesystem reads (no LLM, no embeddings) — they return in O(ms) once
		the artifacts are written.
		"""
		workspace_id = (self._workspace_id or "").strip() or current_workspace_id()
		if not workspace_id or workspace_id == "default":
			self._summary = None
			self._items = []
			self._summary_stripe.apply(None)
			self._render_results()
			return

		try:
			self._summary = self._controller.get_verify_summary(workspace_id)
		except Exception:
			self._summary = None
		try:
			response = self._controller.list_verify_results(
				workspace_id=workspace_id,
				page=1,
				page_size=200,
			)
		except Exception:
			response = {"items": [], "available": False}

		items = response.get("items") if isinstance(response, dict) else []
		self._items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
		self._summary_stripe.apply(self._summary)
		# Updating the workspace_id from the API response is the source of truth
		# (the runtime may have resolved "default" → most-recent workspace).
		if isinstance(response, dict) and response.get("workspaceId"):
			self._workspace_id = str(response["workspaceId"])
			self._update_workspace_label()

		# Toggle the empty-state card + sections panel + results header against
		# whatever the summary reports — one source of truth (``available``).
		available = bool(self._summary and self._summary.get("available"))
		self._empty_state_card.setVisible(not available)
		sections_overview = (
			list(self._summary.get("sectionsOverview") or [])
			if isinstance(self._summary, dict)
			else []
		)
		flow_source = (
			str(self._summary.get("flowSource") or "")
			if isinstance(self._summary, dict)
			else ""
		)
		self._sections_panel.setVisible(available)
		if available:
			self._sections_panel.apply(sections_overview, flow_source)
		self._results_header.setVisible(available)
		self._prev_btn.setVisible(available)
		self._next_btn.setVisible(available)
		self._page_label.setVisible(available)

		self._render_results()

	# -- rendering ------------------------------------------------------------

	def _render_results(self) -> None:
		# Tear down old widgets and rebuild from current state.
		while self._content_layout.count():
			item = self._content_layout.takeAt(0)
			widget = item.widget() if item else None
			if widget is not None:
				widget.setParent(None)
				widget.deleteLater()

		# Update every chip's label so the user sees per-level counts even
		# before clicking — answers "어떤 문서가 각 항목으로 평가되었는지".
		counts = {
			"전체": len(self._items),
			"높음": sum(1 for it in self._items if it.get("level") == "높음"),
			"중간": sum(1 for it in self._items if it.get("level") == "중간"),
			"낮음": sum(1 for it in self._items if it.get("level") == "낮음"),
		}
		for name, chip in self._filter_buttons.items():
			chip.setText(f"{name} ({counts.get(name, 0)})")

		filtered = [
			item for item in self._items
			if self._active_filter == "전체" or item.get("level") == self._active_filter
		]

		if not self._items:
			# The top-level _EmptyStateCard already explains the no-verification
			# case loudly, so the in-content message only fires when verification
			# *did* run but produced no items (rare — usually means no docs or no
			# intent queries).
			summary_available = bool(self._summary and self._summary.get("available"))
			if summary_available:
				empty = QLabel(
					"검증은 실행되었지만 표시할 자료가 없습니다. 조사 결과에"
					" plan.must_cover 또는 grounded_terms 가 비어 있는지 확인해 주세요."
				)
				empty.setObjectName("PageSubtitle")
				empty.setAlignment(Qt.AlignCenter)
				empty.setWordWrap(True)
				self._content_layout.addWidget(empty)
			self._page_label.setText("0 / 0")
			self._prev_btn.setEnabled(False)
			self._next_btn.setEnabled(False)
			return

		if not filtered:
			empty = QLabel(f"‘{self._active_filter}’ 단계의 자료가 없습니다.")
			empty.setObjectName("PageSubtitle")
			empty.setAlignment(Qt.AlignCenter)
			empty.setWordWrap(True)
			self._content_layout.addWidget(empty)
			self._page_label.setText("0 / 0")
			self._prev_btn.setEnabled(False)
			self._next_btn.setEnabled(False)
			return

		total_pages = max(1, ceil(len(filtered) / self._page_size))
		if self._current_page >= total_pages:
			self._current_page = total_pages - 1
		self._current_page = max(0, self._current_page)
		start = self._current_page * self._page_size
		end = start + self._page_size
		for item in filtered[start:end]:
			self._content_layout.addWidget(self._build_card(item))

		self._page_label.setText(f"{self._current_page + 1} / {total_pages}")
		self._prev_btn.setEnabled(self._current_page > 0)
		self._next_btn.setEnabled(self._current_page < total_pages - 1)

	def _build_card(self, item: dict[str, Any]) -> QWidget:
		level = str(item.get("level") or "")
		tone = _tone_for_level(level)

		right_panel = QWidget()
		right_panel.setAttribute(Qt.WA_StyledBackground, False)
		right_panel.setStyleSheet("background: transparent;")
		right_panel.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
		right_panel.setFixedWidth(104)
		right_layout = QVBoxLayout(right_panel)
		right_layout.setContentsMargins(0, 0, 0, 0)
		right_layout.setSpacing(6)
		right_layout.addWidget(Badge(level or "—", tone), 0, Qt.AlignRight)

		action = AppButton("상세 보기", variant="ghost")
		action.setObjectName("VerifyDetailButton")
		action.setFixedHeight(28)
		action.setFixedWidth(92)
		action.clicked.connect(partial(self._show_detail, item))
		right_layout.addWidget(action, 0, Qt.AlignRight)
		right_layout.addStretch(1)

		wrapper = QWidget()
		wrapper_layout = QVBoxLayout(wrapper)
		wrapper_layout.setContentsMargins(0, 0, 0, 0)
		wrapper_layout.setSpacing(0)
		issues = item.get("issues") or []
		footer = issues[0] if issues else "특이사항이 발견되지 않았습니다."
		wrapper_layout.addWidget(
			DocumentCard(
				title=str(item.get("title") or item.get("docId") or "문서"),
				subtitle=str(item.get("matchRate") or ""),
				right_widget=right_panel,
				footer=str(footer),
			)
		)
		return wrapper

	def _on_issues_clicked(self) -> None:
		"""Open the issues dialog with whatever the summary already carries."""
		if not isinstance(self._summary, dict) or not self._summary.get("available"):
			return
		issues = self._summary.get("issues") or []
		if not isinstance(issues, list):
			return
		dialog = VerifyIssuesDialog(issues, parent=self)
		dialog.exec()

	def _on_section_clicked(self, section_id: int) -> None:
		"""Open the section detail dialog for the clicked section card."""
		if not isinstance(self._summary, dict):
			return
		sections = self._summary.get("sectionsOverview") or []
		section = next(
			(s for s in sections if isinstance(s, dict) and int(s.get("sectionId") or -1) == section_id),
			None,
		)
		if section is None:
			return
		titles = {
			str(item.get("docId") or ""): str(item.get("title") or item.get("docId") or "")
			for item in self._items
			if isinstance(item, dict)
		}
		dialog = VerifySectionDetailDialog(section, titles, parent=self)
		dialog.exec()

	def _show_detail(self, summary_item: dict[str, Any]) -> None:
		"""Pull the full detail payload from the API and open the dialog."""
		doc_id = str(summary_item.get("docId") or "").strip()
		if not doc_id:
			return
		try:
			payload = self._controller.get_verify_detail(doc_id, self._workspace_id)
		except Exception as exc:
			# Fall back to the list-row data so the dialog still opens with
			# whatever we already have rather than failing silently.
			payload = dict(summary_item)
			payload["_error"] = str(exc)
		if not isinstance(payload, dict):
			payload = dict(summary_item)
		dialog = VerifyDetailDialog(payload, parent=self)
		dialog.exec()

	# -- progress poller ------------------------------------------------------

	def _start_poller(self) -> None:
		if self._progress_poller is not None and self._progress_poller.isRunning():
			return
		poller = VerifyProgressPoller(self)
		poller.events.connect(self._on_progress_events)
		poller.reset_detected.connect(self._on_progress_reset)
		poller.start()
		self._progress_poller = poller

	def _stop_poller(self) -> None:
		poller = self._progress_poller
		if poller is None:
			return
		poller.request_stop()
		poller.wait(2000)
		self._progress_poller = None

	def _on_progress_reset(self) -> None:
		self._progress_bar.start("검증 준비 중...")

	def _on_progress_events(self, events: list[dict[str, Any]]) -> None:
		# Use the latest event to drive the bar; earlier events are folded
		# into the same animation step naturally.
		if not events:
			return
		latest = events[-1]
		stage = str(latest.get("stage") or "")
		message = str(latest.get("message") or "")
		if stage == "failed":
			self._progress_bar.mark_failed(message or "검증에 실패했습니다.")
			return
		if stage == "completed":
			self._progress_bar.mark_completed(animate=True)
			return
		percent = _STAGE_PROGRESS.get(stage, 50.0)
		self._progress_bar.set_progress(percent, message or None)

	# -- helpers --------------------------------------------------------------

	def _set_filter(self, label: str) -> None:
		# A QToolButton in a non-exclusive group can stay un-checked when
		# clicked again. Force-check the active one (and un-check the others)
		# so the chip row always shows exactly one selected level.
		self._active_filter = label
		for name, chip in self._filter_buttons.items():
			chip.setChecked(name == label)
		self._current_page = 0
		self._render_results()

	def _go_prev_page(self) -> None:
		if self._current_page <= 0:
			return
		self._current_page -= 1
		self._render_results()

	def _go_next_page(self) -> None:
		self._current_page += 1
		self._render_results()

	def _update_workspace_label(self) -> None:
		name = (self._workspace_id or "").strip() or "(미선택)"
		self._workspace_label.setText(f"워크스페이스: {name}")

	def _sync_run_button(self) -> None:
		manager = get_job_manager()
		blocked_externally = manager.is_blocked(JobCategory.VERIFY) and not self._busy
		if self._busy:
			self._run_button.setText("검증 진행 중...")
			self._run_button.setEnabled(False)
			return
		# Has any saved verification result -> show "재검증", else "검증 시작".
		has_results = bool(self._summary and self._summary.get("available"))
		self._run_button.setText("재검증" if has_results else "검증 시작")
		self._run_button.setEnabled(not blocked_externally)
