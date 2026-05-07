from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton, QWidget


class AppButton(QPushButton):
	def __init__(self, text: str, variant: str = "primary", parent: QWidget | None = None) -> None:
		super().__init__(text, parent)
		self.setCursor(Qt.PointingHandCursor)
		self.set_variant(variant)

	def set_variant(self, variant: str) -> None:
		if variant == "ghost":
			self.setObjectName("GhostButton")
		elif variant == "filter":
			self.setObjectName("FilterChip")
		elif variant == "send":
			self.setObjectName("SendButton")
		elif variant == "top":
			self.setObjectName("TopActionButton")
		else:
			self.setObjectName("PrimaryButton")
