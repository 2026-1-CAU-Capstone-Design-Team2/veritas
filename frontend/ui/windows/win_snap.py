"""Windows 10/11 Snap Layouts · Aero Snap for our frameless windows.

VERITAS' three top-level windows (메인화면 · 보조창 · 문서 에디터) all use
``Qt.FramelessWindowHint`` + a custom ``VeritasTitleBar`` so they share one
rounded, floating look. The catch: ``FramelessWindowHint`` strips the native
``WS_CAPTION`` / ``WS_THICKFRAME`` styles, and without those Windows shows

  * no **Snap Layouts** flyout when you hover the maximise button (Win11), and
  * no **Aero Snap / Snap Assist** when you drag the window to a screen edge or
    the top (Win10 + Win11).

``WindowsSnapMixin`` puts those back. It re-adds the native frame styles so the
OS treats the window as a real, snappable, resizable window, then takes over the
non-client hit-testing (``WM_NCHITTEST``) so the *custom* chrome behaves like a
real title bar: dragging the empty title-bar area snaps, hovering the maximise
button shows the Win11 layout flyout (we report ``HTMAXBUTTON``), and the OS
handles edge-resizing + the maximise animation. ``WM_NCCALCSIZE`` is handled to
keep the frame invisible (our QSS panel draws the chrome instead).

The mixin is a **no-op on non-Windows platforms** and degrades gracefully if any
Win32 call is unavailable, so the existing pure-Qt drag/resize handlers in each
window stay as the cross-platform fallback.

Usage — mix in *before* the Qt base class and install once the window is built::

    class MyWindow(WindowsSnapMixin, QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
            ...                       # build self.title_bar (a VeritasTitleBar)
            self._install_snap_layout()

By default the mixin discovers the chrome through ``self.title_bar`` and
``self.title_bar.maximize_button`` (the VeritasTitleBar contract); override
``_snap_title_bar`` / ``_snap_max_button`` only for a different layout.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QAbstractButton

IS_WINDOWS = sys.platform == "win32"

# --- Win32 messages / styles / hit-test results -------------------------------
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONDBLCLK = 0x00A3

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
SWP_FRAMECHANGED = 0x0020

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

SM_CXSIZEFRAME = 32
SM_CYSIZEFRAME = 33
SM_CXPADDEDBORDER = 92

# WM_NCHITTEST results
HTCLIENT = 1
HTCAPTION = 2
HTMAXBUTTON = 9
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

# Auto-hide taskbar (SHAppBarMessage) — so a maximised window leaves the 1px the
# bar needs to re-appear.
ABM_GETSTATE = 0x00000004
ABM_GETTASKBARPOS = 0x00000005
ABS_AUTOHIDE = 0x01
ABE_LEFT = 0
ABE_TOP = 1
ABE_RIGHT = 2
ABE_BOTTOM = 3


if IS_WINDOWS:
	_user32 = ctypes.windll.user32
	_shell32 = ctypes.windll.shell32

	# *Ptr variants on 64-bit so the LONG_PTR-wide style is not truncated.
	try:
		_get_window_long = _user32.GetWindowLongPtrW
		_set_window_long = _user32.SetWindowLongPtrW
	except AttributeError:  # 32-bit interpreter
		_get_window_long = _user32.GetWindowLongW
		_set_window_long = _user32.SetWindowLongW
	_get_window_long.restype = ctypes.c_ssize_t
	_get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
	_set_window_long.restype = ctypes.c_ssize_t
	_set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]

	_user32.SetWindowPos.restype = wintypes.BOOL
	_user32.SetWindowPos.argtypes = [
		wintypes.HWND, wintypes.HWND,
		ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
	]
	_user32.GetSystemMetrics.restype = ctypes.c_int
	_user32.GetSystemMetrics.argtypes = [ctypes.c_int]

	class _RECT(ctypes.Structure):
		_fields_ = [
			("left", ctypes.c_long),
			("top", ctypes.c_long),
			("right", ctypes.c_long),
			("bottom", ctypes.c_long),
		]

	_user32.GetWindowRect.restype = wintypes.BOOL
	_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]

	class _MSG(ctypes.Structure):
		_fields_ = [
			("hwnd", wintypes.HWND),
			("message", wintypes.UINT),
			("wParam", wintypes.WPARAM),
			("lParam", wintypes.LPARAM),
			("time", wintypes.DWORD),
			("pt", wintypes.POINT),
		]

	class _NCCALCSIZE_PARAMS(ctypes.Structure):
		_fields_ = [
			("rgrc", _RECT * 3),
			("lppos", ctypes.c_void_p),
		]

	class _APPBARDATA(ctypes.Structure):
		_fields_ = [
			("cbSize", wintypes.DWORD),
			("hWnd", wintypes.HWND),
			("uCallbackMessage", wintypes.UINT),
			("uEdge", wintypes.UINT),
			("rc", _RECT),
			("lParam", wintypes.LPARAM),
		]

	def _frame_border() -> tuple[int, int]:
		"""Resize-frame thickness (physical px) a WS_THICKFRAME window reserves."""
		pad = _user32.GetSystemMetrics(SM_CXPADDEDBORDER)
		cx = _user32.GetSystemMetrics(SM_CXSIZEFRAME) + pad
		cy = _user32.GetSystemMetrics(SM_CYSIZEFRAME) + pad
		return cx, cy

	def _autohide_taskbar_edge() -> int | None:
		"""Return the screen edge an auto-hide taskbar lives on, else ``None``."""
		try:
			data = _APPBARDATA()
			data.cbSize = ctypes.sizeof(_APPBARDATA)
			state = _shell32.SHAppBarMessage(ABM_GETSTATE, ctypes.byref(data))
			if not (state & ABS_AUTOHIDE):
				return None
			pos = _APPBARDATA()
			pos.cbSize = ctypes.sizeof(_APPBARDATA)
			if not _shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(pos)):
				return None
			rc = pos.rc
			if rc.right - rc.left > rc.bottom - rc.top:
				return ABE_TOP if rc.top <= 0 else ABE_BOTTOM
			return ABE_LEFT if rc.left <= 0 else ABE_RIGHT
		except Exception:
			return None


def set_window_topmost(hwnd: int, enable: bool) -> None:
	"""Toggle a window's always-on-top (WS_EX_TOPMOST) z-band via Win32, without
	recreating the window the way Qt's ``setWindowFlag(WindowStaysOnTopHint)``
	would. Used so the always-on-top assist window can briefly *yield* the topmost
	band while the editor is brought up (a normal window can't otherwise rise above
	a topmost sibling). No-op off Windows / on a falsy handle."""
	if not IS_WINDOWS or not hwnd:
		return
	try:
		_user32.SetWindowPos(
			hwnd, HWND_TOPMOST if enable else HWND_NOTOPMOST,
			0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
		)
	except Exception:
		pass


class WindowsSnapMixin:
	"""Adds Windows 10/11 Snap Layouts / Aero Snap to a frameless Qt window.

	Mix in *before* ``QMainWindow`` / ``QWidget`` and call
	:meth:`_install_snap_layout` after the title bar exists.
	"""

	# ---------------------------------------------------------------- install

	def _install_snap_layout(self) -> None:
		"""Re-add the native frame styles so the OS provides snap + resize.

		Idempotent and a no-op off Windows. Forces native window creation so the
		styles stick before the first show; once applied, Qt keeps them (only a
		fresh ``setWindowFlags`` would clobber them, which the windows never do).
		"""
		self._snap_enabled = False
		if not IS_WINDOWS:
			return
		try:
			hwnd = int(self.winId())
			style = _get_window_long(hwnd, GWL_STYLE)
			_set_window_long(
				hwnd,
				GWL_STYLE,
				style | WS_CAPTION | WS_THICKFRAME | WS_MAXIMIZEBOX | WS_MINIMIZEBOX,
			)
			_user32.SetWindowPos(
				hwnd, 0, 0, 0, 0, 0,
				SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
				| SWP_NOOWNERZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
			)
			self._snap_enabled = True
		except Exception:
			# Any Win32 hiccup: leave the pure-Qt drag/resize fallback in place.
			self._snap_enabled = False

	# ------------------------------------------------------------ chrome hooks

	def _snap_title_bar(self):
		"""The draggable title bar widget (override for a non-default layout)."""
		return getattr(self, "title_bar", None)

	def _snap_max_button(self):
		"""The maximise/restore button, hovering which shows the snap flyout."""
		return getattr(self._snap_title_bar(), "maximize_button", None)

	def _snap_resize_border(self) -> int:
		"""Edge-grab thickness in logical px (defaults to the window's margin)."""
		return getattr(self, "_resize_margin", 8)

	def _snap_toggle_maximize(self) -> None:
		title_bar = self._snap_title_bar()
		toggle = getattr(title_bar, "_toggle_max_restore", None)
		if callable(toggle):
			toggle()
		elif self.isMaximized():
			self.showNormal()
		else:
			self.showMaximized()

	# --------------------------------------------------------- native events

	def nativeEvent(self, eventType, message):  # type: ignore[override]
		if getattr(self, "_snap_enabled", False):
			try:
				et = bytes(eventType)
			except Exception:
				et = eventType
			if et == b"windows_generic_MSG":
				handled, result = self._snap_handle_message(_MSG.from_address(int(message)))
				if handled:
					return True, result
		return super().nativeEvent(eventType, message)

	def _snap_handle_message(self, msg) -> tuple[bool, int]:
		message = msg.message
		if message == WM_NCCALCSIZE:
			if msg.wParam:
				# Claim the whole window as client area (no native frame drawn);
				# the maximised inset keeps content on-screen.
				self._snap_adjust_nccalcsize(msg)
				return True, 0
			return False, 0
		if message == WM_NCHITTEST:
			return True, self._snap_hit_test(msg)
		if message in (WM_NCLBUTTONDOWN, WM_NCLBUTTONDBLCLK) and msg.wParam == HTMAXBUTTON:
			# A click on our custom maximise button (the snap flyout itself is
			# handled by DWM) — toggle and swallow so DefWindowProc adds nothing.
			self._snap_toggle_maximize()
			return True, 0
		return False, 0

	def _snap_adjust_nccalcsize(self, msg) -> None:
		if not (self.isMaximized() and not self.isFullScreen()):
			return
		# A maximised WS_THICKFRAME window is positioned `border` px off every
		# edge; inset the client rect so the panel isn't clipped off-screen.
		params = _NCCALCSIZE_PARAMS.from_address(int(msg.lParam))
		rc = params.rgrc[0]
		bx, by = _frame_border()
		rc.left += bx
		rc.top += by
		rc.right -= bx
		rc.bottom -= by
		edge = _autohide_taskbar_edge()
		if edge == ABE_TOP:
			rc.top += 1
		elif edge == ABE_BOTTOM:
			rc.bottom -= 1
		elif edge == ABE_LEFT:
			rc.left += 1
		elif edge == ABE_RIGHT:
			rc.right -= 1

	def _snap_hit_test(self, msg) -> int:
		# Cursor in physical screen px (signed 16-bit pair packed in lParam).
		lp = int(msg.lParam)
		x = lp & 0xFFFF
		if x >= 0x8000:
			x -= 0x10000
		y = (lp >> 16) & 0xFFFF
		if y >= 0x8000:
			y -= 0x10000

		rect = _RECT()
		_user32.GetWindowRect(msg.hwnd, ctypes.byref(rect))
		px = x - rect.left
		py = y - rect.top
		pw = rect.right - rect.left
		ph = rect.bottom - rect.top

		dpr = self.devicePixelRatioF() or 1.0
		border = max(1, int(round(self._snap_resize_border() * dpr)))
		maximized = self.isMaximized() or self.isFullScreen()

		on_left = (not maximized) and px < border
		on_right = (not maximized) and px >= pw - border
		on_top = (not maximized) and py < border
		on_bottom = (not maximized) and py >= ph - border

		if on_top and on_left:
			return HTTOPLEFT
		if on_top and on_right:
			return HTTOPRIGHT
		if on_bottom and on_left:
			return HTBOTTOMLEFT
		if on_bottom and on_right:
			return HTBOTTOMRIGHT
		if on_left:
			return HTLEFT
		if on_right:
			return HTRIGHT
		if on_top:
			return HTTOP
		if on_bottom:
			return HTBOTTOM

		# Title-bar band: empty area = caption (OS drag → Aero Snap); the
		# maximise button = HTMAXBUTTON (→ Win11 snap-layout flyout); any other
		# button stays HTCLIENT so Qt keeps its click.
		title_bar = self._snap_title_bar()
		if title_bar is not None and title_bar.isVisible():
			origin = title_bar.mapTo(self, QPoint(0, 0))
			lx = px / dpr - origin.x()
			ly = py / dpr - origin.y()
			if 0 <= lx < title_bar.width() and 0 <= ly < title_bar.height():
				local = QPoint(int(lx), int(ly))
				max_btn = self._snap_max_button()
				if (
					max_btn is not None
					and max_btn.isVisible()
					and QRect(max_btn.mapTo(title_bar, QPoint(0, 0)), max_btn.size()).contains(local)
				):
					return HTMAXBUTTON
				child = title_bar.childAt(local)
				if isinstance(child, QAbstractButton):
					return HTCLIENT
				return HTCAPTION

		return HTCLIENT
