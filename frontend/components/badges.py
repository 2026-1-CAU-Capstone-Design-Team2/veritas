from __future__ import annotations

from PySide6.QtWidgets import QLabel, QWidget


class Badge(QLabel):
	COLORS = {
		"neutral": ("#F3F4F6", "#4B5563", "#E5E7EB"),
		"success": ("#ECFDF3", "#166534", "#BBF7D0"),
		"warning": ("#FFF7ED", "#9A3412", "#FED7AA"),
		"danger": ("#FEF2F2", "#B32424", "#FECACA"),
		"info": ("#EEF2FF", "#2D2685", "#C7D2FE"),
	}

	def __init__(self, text: str, tone: str = "neutral", parent: QWidget | None = None) -> None:
		super().__init__(text, parent)
		self.setObjectName("Badge")
		bg, fg, border = self.COLORS.get(tone, self.COLORS["neutral"])
		self.setStyleSheet(
			f"""
			QLabel#Badge {{
				background-color: {bg};
				color: {fg};
				border: 1px solid {border};
				border-radius: 11px;
				padding: 4px 10px;
				font-size: 11px;
				font-weight: 700;
			}}
			"""
		)
