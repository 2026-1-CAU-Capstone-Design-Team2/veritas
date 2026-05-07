from __future__ import annotations

from PySide6.QtCore import Qt
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
