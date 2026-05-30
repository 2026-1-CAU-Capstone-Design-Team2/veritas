from __future__ import annotations

from PySide6.QtWidgets import QLabel, QWidget

from ..theme import theme


class Badge(QLabel):
	# tone → (background, foreground, border) theme tokens. The semantic tones
	# keep their meaning in both light and dark; only the tint/contrast adapts.
	_TONE_TOKENS = {
		"neutral": ("surface.muted2", "text.subtle", "border.gray"),
		"success": ("success.bg", "success.fg", "success.border"),
		"warning": ("warning.bg", "warning.fg", "warning.border"),
		"danger": ("danger.bg", "danger.fg", "danger.border"),
		"info": ("accent.subtle.bg", "accent.text", "accent.subtle.border"),
	}

	def __init__(self, text: str, tone: str = "neutral", parent: QWidget | None = None) -> None:
		super().__init__(text, parent)
		self.setObjectName("Badge")
		self._tone = tone if tone in self._TONE_TOKENS else "neutral"
		self._apply_theme()
		theme.themeChanged.connect(self._apply_theme)

	def _apply_theme(self, *args) -> None:
		bg, fg, border = self._TONE_TOKENS[self._tone]
		self.setStyleSheet(
			"QLabel#Badge {"
			f" background-color: {theme.color(bg)};"
			f" color: {theme.color(fg)};"
			f" border: 1px solid {theme.color(border)};"
			" border-radius: 11px; padding: 4px 10px; font-size: 11px; font-weight: 700; }"
		)
