"""Runtime theme state for the frontend.

A single :class:`ThemeManager` (the module-level ``theme`` singleton) owns the
current mode, persists the user's choice with :class:`QSettings`, and notifies
every styled widget through :attr:`ThemeManager.themeChanged` so they can rebuild
their stylesheets live when the user toggles light/dark.

Typical use::

    from frontend.theme import theme

    label.setStyleSheet(f"color: {theme.color('text.primary')};")
    theme.themeChanged.connect(self._apply_theme)   # re-style on toggle
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QSettings, Signal

from .palette import PALETTES

_VALID_MODES = ("light", "dark")
_SETTINGS_ORG = "VERITAS"
_SETTINGS_APP = "frontend"
_SETTINGS_KEY = "appearance/mode"


class ThemeManager(QObject):
	"""Holds the active palette and broadcasts theme changes.

	The instance is created once at import time and shared as ``theme``. It can
	be constructed before the :class:`QApplication` exists (it touches no
	widgets); :meth:`apply` is what actually pushes a stylesheet, and callers
	invoke it once the app and windows are alive.
	"""

	#: Emitted after the mode changes; carries the new mode ("light"|"dark").
	themeChanged = Signal(str)

	def __init__(self) -> None:
		super().__init__()
		self._mode = self._load_saved_mode()

	# -- persistence ------------------------------------------------------

	def _load_saved_mode(self) -> str:
		try:
			settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
			saved = str(settings.value(_SETTINGS_KEY, "light") or "light").lower()
		except Exception:
			saved = "light"
		return saved if saved in _VALID_MODES else "light"

	def _persist_mode(self) -> None:
		try:
			settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
			settings.setValue(_SETTINGS_KEY, self._mode)
		except Exception:
			# Persistence is best-effort; a failure here must never crash the UI.
			pass

	# -- query ------------------------------------------------------------

	def mode(self) -> str:
		return self._mode

	def is_dark(self) -> bool:
		return self._mode == "dark"

	def palette(self) -> dict[str, str]:
		"""The active token → colour map."""
		return PALETTES[self._mode]

	def color(self, token: str) -> str:
		"""Resolve a single token for the active mode.

		Falls back to the light palette (then to magenta) for an unknown token,
		so a typo is visible rather than crashing.
		"""
		active = PALETTES[self._mode]
		if token in active:
			return active[token]
		return PALETTES["light"].get(token, "#FF00FF")

	# -- mutation ---------------------------------------------------------

	def set_mode(self, mode: str) -> None:
		mode = (mode or "").lower()
		if mode not in _VALID_MODES or mode == self._mode:
			return
		self._mode = mode
		self._persist_mode()
		self.themeChanged.emit(self._mode)

	def toggle(self) -> None:
		self.set_mode("dark" if self._mode == "light" else "light")


#: Process-wide theme singleton. Import this, not the class.
theme = ThemeManager()
