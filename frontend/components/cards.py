from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
	QFrame,
	QGraphicsDropShadowEffect,
	QHBoxLayout,
	QLabel,
	QVBoxLayout,
	QWidget,
)


class CardWidget(QFrame):
	def __init__(self, title: str | None = None, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("CardWidget")

		shadow = QGraphicsDropShadowEffect(self)
		shadow.setBlurRadius(20)
		shadow.setXOffset(0)
		shadow.setYOffset(8)
		shadow.setColor(QColor(31, 41, 55, 10))
		self.setGraphicsEffect(shadow)

		self.layout = QVBoxLayout(self)
		self.layout.setContentsMargins(18, 16, 18, 16)
		self.layout.setSpacing(12)

		if title:
			title_label = QLabel(title)
			title_label.setObjectName("CardTitle")
			self.layout.addWidget(title_label)


class StatTile(CardWidget):
	def __init__(self, label: str, value: str, delta: str, parent: QWidget | None = None) -> None:
		super().__init__(parent=parent)
		self.setObjectName("StatTile")

		label_widget = QLabel(label)
		label_widget.setObjectName("StatLabel")

		value_widget = QLabel(value)
		value_widget.setObjectName("StatValue")

		delta_widget = QLabel(delta)
		delta_widget.setObjectName("StatDelta")

		self.layout.addWidget(label_widget)
		self.layout.addWidget(value_widget)
		self.layout.addWidget(delta_widget)


class _CollapsibleCardHeader(QFrame):
	"""Clickable header row for :class:`CollapsibleCard`.

	Layout: ``[chevron] [title] ........ [status badge]``. The status badge
	stays visible while the body is collapsed, so a completed long-running
	task (e.g. verification) can summarize its result without forcing the
	user to expand the card.
	"""

	clicked = Signal()

	def __init__(self, title: str, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("CollapsibleCardHeader")
		self.setCursor(Qt.PointingHandCursor)

		row = QHBoxLayout(self)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(8)

		self.chevron = QLabel("▶")
		self.chevron.setObjectName("CollapsibleChevron")
		self.chevron.setFixedWidth(16)
		row.addWidget(self.chevron)

		self.title_label = QLabel(title)
		self.title_label.setObjectName("CardTitle")
		row.addWidget(self.title_label)

		row.addStretch(1)

		self.status_label = QLabel("")
		self.status_label.setObjectName("CollapsibleStatus")
		self.status_label.setVisible(False)
		self.status_label.setWordWrap(True)
		row.addWidget(self.status_label, 2, Qt.AlignRight | Qt.AlignVCenter)

	def mousePressEvent(self, event) -> None:  # type: ignore[override]
		if event.button() == Qt.LeftButton:
			self.clicked.emit()
			event.accept()
			return
		super().mousePressEvent(event)


class CollapsibleCard(CardWidget):
	"""A CardWidget whose body collapses behind a clickable header.

	Collapsed-by-default sections keep long pages short; the header's status
	badge (``set_status``) keeps the headline result visible while collapsed.
	Widgets go into ``body_layout`` (not ``layout``) so they fold together.
	"""

	toggled = Signal(bool)

	def __init__(
		self,
		title: str,
		expanded: bool = False,
		parent: QWidget | None = None,
	) -> None:
		super().__init__(parent=parent)

		self._header = _CollapsibleCardHeader(title)
		self._header.clicked.connect(self.toggle)
		self.layout.addWidget(self._header)

		self._body = QWidget()
		self.body_layout = QVBoxLayout(self._body)
		self.body_layout.setContentsMargins(0, 6, 0, 0)
		self.body_layout.setSpacing(12)
		self.layout.addWidget(self._body)

		self._expanded = bool(expanded)
		self._body.setVisible(self._expanded)
		self._update_chevron()

	# -- public API --------------------------------------------------------

	def set_status(self, text: str, *, tone: str = "success") -> None:
		"""Show a status badge in the header (visible while collapsed).

		``tone`` picks the badge color: "success" (default), "warning",
		"danger", or "neutral". Pass an empty ``text`` to hide the badge.
		"""
		label = self._header.status_label
		text = str(text or "").strip()
		label.setText(text)
		label.setVisible(bool(text))
		label.setProperty("tone", tone)
		# Re-polish so a stylesheet (or inline style) keyed on the property
		# updates immediately.
		label.style().unpolish(label)
		label.style().polish(label)

	def set_expanded(self, expanded: bool) -> None:
		expanded = bool(expanded)
		if expanded == self._expanded:
			return
		self._expanded = expanded
		self._body.setVisible(expanded)
		self._update_chevron()
		self.toggled.emit(expanded)

	def toggle(self) -> None:
		self.set_expanded(not self._expanded)

	def is_expanded(self) -> bool:
		return self._expanded

	# -- internals ----------------------------------------------------------

	def _update_chevron(self) -> None:
		self._header.chevron.setText("▼" if self._expanded else "▶")


class DocumentCard(CardWidget):
	def __init__(
		self,
		title: str,
		subtitle: str,
		right_widget: QWidget | None = None,
		footer: str | None = None,
		parent: QWidget | None = None,
	) -> None:
		super().__init__(parent=parent)

		title_row = QHBoxLayout()
		title_row.setSpacing(12)

		title_label = QLabel(title)
		title_label.setObjectName("CardPrimary")
		title_label.setWordWrap(True)
		title_row.addWidget(title_label, 1)

		if right_widget is not None:
			title_row.addWidget(right_widget, 0, Qt.AlignRight | Qt.AlignTop)

		subtitle_label = QLabel(subtitle)
		subtitle_label.setObjectName("CardSecondary")
		subtitle_label.setWordWrap(True)

		self.layout.addLayout(title_row)
		self.layout.addWidget(subtitle_label)

		if footer:
			footer_label = QLabel(footer)
			footer_label.setObjectName("CardFooter")
			footer_label.setWordWrap(True)
			self.layout.addWidget(footer_label)
