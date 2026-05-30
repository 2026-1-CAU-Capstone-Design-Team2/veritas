"""Centralised theming for the VERITAS frontend.

Import the shared ``theme`` singleton and the stylesheet builders from here::

    from frontend.theme import theme, build_main_window_qss

    widget.setStyleSheet(build_main_window_qss(theme.palette()))
    theme.themeChanged.connect(self._apply_theme)

The ``LIGHT`` palette reproduces the app's original colours exactly, so turning
the theme system on is a no-op in light mode; ``DARK`` is the slate-based dark
counterpart. Toggle with ``theme.toggle()`` / ``theme.set_mode("dark")``.
"""

from __future__ import annotations

from .manager import ThemeManager, theme
from .palette import DARK, LIGHT, PALETTES
from .stylesheet import (
	build_assist_window_qss,
	build_editor_qss,
	build_main_window_qss,
	chat_qss,
	chat_surface_gradient,
	titlebar_qss,
)

__all__ = [
	"theme",
	"ThemeManager",
	"LIGHT",
	"DARK",
	"PALETTES",
	"build_main_window_qss",
	"build_editor_qss",
	"build_assist_window_qss",
	"chat_qss",
	"titlebar_qss",
	"chat_surface_gradient",
]
