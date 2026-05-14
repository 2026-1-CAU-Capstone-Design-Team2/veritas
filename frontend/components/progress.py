from __future__ import annotations

from PySide6.QtCore import (
	Property,
	QEasingCurve,
	QPropertyAnimation,
	QRectF,
	Qt,
	Signal,
)
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import (
	QFrame,
	QHBoxLayout,
	QLabel,
	QSizePolicy,
	QVBoxLayout,
	QWidget,
)


# Per-state palette: fill gradient (start, end) + status chip (bg, fg, border).
_STATE_COLORS = {
	"running": ("#6366F1", "#3B82F6", "#E0E7FF", "#3730A3", "#C7D2FE"),
	"completed": ("#34D399", "#10B981", "#DCFCE7", "#15803D", "#86EFAC"),
	"partial": ("#FBBF24", "#F59E0B", "#FEF3C7", "#B45309", "#FCD34D"),
	"failed": ("#F87171", "#EF4444", "#FEE2E2", "#B91C1C", "#FCA5A5"),
}
_TRACK_BG = "#E8EDF4"


class _ProgressTrack(QWidget):
	"""Custom-painted rounded progress bar.

	Two animatable Qt properties drive the visuals:
	- ``ratio``   — 0.0..1.0 fill amount, eased whenever a new target arrives.
	- ``shimmer`` — 0.0..1.0 looping highlight sweep, only while running.
	"""

	ratioChanged = Signal(float)

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self._ratio = 0.0
		self._shimmer = 0.0
		self._state = "running"
		self.setFixedHeight(14)
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

		self._fill_anim = QPropertyAnimation(self, b"ratio", self)
		self._fill_anim.setDuration(450)
		self._fill_anim.setEasingCurve(QEasingCurve.OutCubic)

		self._shimmer_anim = QPropertyAnimation(self, b"shimmer", self)
		self._shimmer_anim.setDuration(1400)
		self._shimmer_anim.setStartValue(0.0)
		self._shimmer_anim.setEndValue(1.0)
		self._shimmer_anim.setLoopCount(-1)

	def get_ratio(self) -> float:
		return self._ratio

	def set_ratio(self, value: float) -> None:
		clamped = max(0.0, min(1.0, float(value)))
		if clamped == self._ratio:
			return
		self._ratio = clamped
		self.ratioChanged.emit(clamped)
		self.update()

	ratio = Property(float, get_ratio, set_ratio)

	def get_shimmer(self) -> float:
		return self._shimmer

	def set_shimmer(self, value: float) -> None:
		self._shimmer = float(value)
		if self._state == "running":
			self.update()

	shimmer = Property(float, get_shimmer, set_shimmer)

	def animate_to(self, ratio: float) -> None:
		"""Ease the fill toward ``ratio`` from wherever it currently sits."""
		target = max(0.0, min(1.0, float(ratio)))
		self._fill_anim.stop()
		self._fill_anim.setStartValue(self._ratio)
		self._fill_anim.setEndValue(target)
		self._fill_anim.start()

	def set_ratio_immediate(self, ratio: float) -> None:
		self._fill_anim.stop()
		self.set_ratio(ratio)

	def set_state(self, state: str) -> None:
		self._state = state
		if state == "running":
			if self._shimmer_anim.state() != QPropertyAnimation.Running:
				self._shimmer_anim.start()
		else:
			self._shimmer_anim.stop()
		self.update()

	def paintEvent(self, event) -> None:  # type: ignore[override]
		painter = QPainter(self)
		painter.setRenderHint(QPainter.Antialiasing, True)
		rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
		radius = rect.height() / 2.0

		track_path = QPainterPath()
		track_path.addRoundedRect(rect, radius, radius)
		painter.fillPath(track_path, QColor(_TRACK_BG))

		if self._ratio <= 0.0:
			return

		fill_start, fill_end, *_chip = _STATE_COLORS.get(
			self._state, _STATE_COLORS["running"]
		)
		# Keep at least a pill-width sliver visible so tiny percentages still read.
		fill_width = max(rect.height(), rect.width() * self._ratio)
		fill_rect = QRectF(rect.x(), rect.y(), fill_width, rect.height())

		fill_path = QPainterPath()
		fill_path.addRoundedRect(fill_rect, radius, radius)
		painter.setClipPath(fill_path)

		gradient = QLinearGradient(fill_rect.topLeft(), fill_rect.topRight())
		gradient.setColorAt(0.0, QColor(fill_start))
		gradient.setColorAt(1.0, QColor(fill_end))
		painter.fillRect(fill_rect, gradient)

		# Soft glossy band along the top edge for a little depth.
		gloss = QLinearGradient(fill_rect.topLeft(), fill_rect.bottomLeft())
		gloss.setColorAt(0.0, QColor(255, 255, 255, 78))
		gloss.setColorAt(0.55, QColor(255, 255, 255, 0))
		painter.fillRect(fill_rect, gloss)

		# Moving shimmer sweep — only meaningful while work is in flight.
		if self._state == "running" and self._ratio < 1.0:
			band = fill_rect.width() * 0.35 + rect.height()
			span = fill_rect.width() + band * 2
			center = fill_rect.x() - band + self._shimmer * span
			shimmer = QLinearGradient(center - band, 0.0, center + band, 0.0)
			shimmer.setColorAt(0.0, QColor(255, 255, 255, 0))
			shimmer.setColorAt(0.5, QColor(255, 255, 255, 95))
			shimmer.setColorAt(1.0, QColor(255, 255, 255, 0))
			painter.fillRect(fill_rect, shimmer)

		painter.setClipping(False)


class ResearchProgressBar(QFrame):
	"""Result-card progress indicator: status chip, percentage, animated bar
	and a single-line caption for the latest backend message.

	States: ``idle`` (hidden), ``running``, ``completed``, ``failed``. When
	failed the whole widget becomes clickable and emits :attr:`errorClicked`
	so the page can surface the error detail.
	"""

	errorClicked = Signal()

	_STATE_LABEL = {
		"running": "진행 중",
		"completed": "완료",
		"partial": "일부 오류 발생",
		"failed": "오류",
	}

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("ResearchProgressBar")
		self.setStyleSheet(
			"QFrame#ResearchProgressBar { background-color: #F8FAFC; "
			"border: 1px solid #E2E8F0; border-radius: 12px; }"
		)
		self._state = "idle"
		self._error_message = ""

		layout = QVBoxLayout(self)
		layout.setContentsMargins(14, 12, 14, 12)
		layout.setSpacing(9)

		header = QHBoxLayout()
		header.setContentsMargins(0, 0, 0, 0)
		header.setSpacing(8)
		self._chip = QLabel()
		self._chip.setObjectName("ResearchProgressChip")
		header.addWidget(self._chip, 0, Qt.AlignLeft)
		header.addStretch(1)
		self._percent = QLabel("0%")
		self._percent.setStyleSheet(
			"color: #0F172A; font-size: 16px; font-weight: 800;"
		)
		header.addWidget(self._percent, 0, Qt.AlignRight)
		layout.addLayout(header)

		self._track = _ProgressTrack()
		self._track.ratioChanged.connect(self._on_ratio_changed)
		layout.addWidget(self._track)

		self._caption = QLabel("")
		self._caption.setWordWrap(False)
		self._caption.setTextFormat(Qt.PlainText)
		self._caption.setStyleSheet(
			"color: #64748B; font-size: 12px; font-weight: 600;"
		)
		layout.addWidget(self._caption)

		self.set_idle()

	# -- state transitions ------------------------------------------------

	def set_idle(self) -> None:
		self._state = "idle"
		self._error_message = ""
		self.setCursor(Qt.ArrowCursor)
		self.setVisible(False)

	def start(self, caption: str = "조사 준비 중...") -> None:
		"""Reset to a fresh running state at 0%."""
		self._state = "running"
		self._error_message = ""
		self.setCursor(Qt.ArrowCursor)
		self.setVisible(True)
		self._track.set_state("running")
		self._track.set_ratio_immediate(0.0)
		self._apply_chip("running")
		self._apply_caption(caption, "#64748B")

	def set_progress(self, percent: float, caption: str | None = None) -> None:
		"""Ease the bar toward ``percent`` (0..100) while running."""
		if self._state != "running":
			self._state = "running"
			self.setVisible(True)
			self._track.set_state("running")
			self._apply_chip("running")
		self._track.animate_to(max(0.0, min(100.0, float(percent))) / 100.0)
		if caption:
			self._apply_caption(caption, "#64748B")

	def set_caption(self, caption: str) -> None:
		color = "#B91C1C" if self._state == "failed" else "#64748B"
		self._apply_caption(caption, color)

	def mark_completed(self, animate: bool = True) -> None:
		self._state = "completed"
		self._error_message = ""
		self.setCursor(Qt.ArrowCursor)
		self.setVisible(True)
		self._track.set_state("completed")
		if animate:
			self._track.animate_to(1.0)
		else:
			self._track.set_ratio_immediate(1.0)
		self._apply_chip("completed")
		self._apply_caption("조사가 완료되었습니다.", "#15803D")

	def mark_partial(self, animate: bool = True) -> None:
		"""Run finished, but some documents failed to summarize.

		The bar fills to 100% in amber and stays clickable so the page can
		surface the per-document failure list via :attr:`errorClicked`.
		"""
		self._state = "partial"
		self._error_message = ""
		self.setCursor(Qt.PointingHandCursor)
		self.setVisible(True)
		self._track.set_state("partial")
		if animate:
			self._track.animate_to(1.0)
		else:
			self._track.set_ratio_immediate(1.0)
		self._apply_chip("partial")
		self._apply_caption(
			"일부 문서 요약에 실패했습니다. 클릭하면 실패한 문서를 확인할 수 있습니다.",
			"#B45309",
		)

	def mark_failed(self, error_message: str = "") -> None:
		self._state = "failed"
		self._error_message = error_message or ""
		self.setCursor(Qt.PointingHandCursor)
		self.setVisible(True)
		self._track.set_state("failed")
		self._apply_chip("failed")
		self._apply_caption("클릭하면 오류 메시지를 확인할 수 있습니다.", "#B91C1C")

	def restore_running(self, percent: float, caption: str = "") -> None:
		"""Show a persisted in-flight job without the count-up animation."""
		self._state = "running"
		self._error_message = ""
		self.setCursor(Qt.ArrowCursor)
		self.setVisible(True)
		self._track.set_state("running")
		self._track.set_ratio_immediate(max(0.0, min(100.0, float(percent))) / 100.0)
		self._apply_chip("running")
		self._apply_caption(caption or "조사가 진행 중입니다.", "#64748B")

	# -- internals --------------------------------------------------------

	def _on_ratio_changed(self, ratio: float) -> None:
		self._percent.setText(f"{int(round(ratio * 100))}%")

	def _apply_chip(self, state: str) -> None:
		_s, _e, bg, fg, border = _STATE_COLORS.get(state, _STATE_COLORS["running"])
		self._chip.setText(f"● {self._STATE_LABEL.get(state, '')}")
		self._chip.setStyleSheet(
			f"QLabel#ResearchProgressChip {{ background-color: {bg}; color: {fg}; "
			f"border: 1px solid {border}; border-radius: 11px; padding: 3px 11px; "
			f"font-size: 12px; font-weight: 800; }}"
		)

	def _apply_caption(self, text: str, color: str) -> None:
		clean = " ".join(str(text or "").split())
		if len(clean) > 200:
			clean = clean[:197] + "..."
		self._caption.setText(clean)
		self._caption.setToolTip(clean)
		self._caption.setStyleSheet(
			f"color: {color}; font-size: 12px; font-weight: 600;"
		)

	def mousePressEvent(self, event) -> None:  # type: ignore[override]
		# Both "failed" (error message) and "partial" (failed-document list)
		# are clickable; the page decides which detail to surface.
		if event.button() == Qt.LeftButton and self._state in ("failed", "partial"):
			self.errorClicked.emit()
			event.accept()
			return
		super().mousePressEvent(event)
