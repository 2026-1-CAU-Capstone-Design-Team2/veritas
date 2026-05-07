from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

from .models import BoundingBox, WindowContext


class WindowContextReader:
    """Win32 API로 foreground window, PID, title, process path를 읽습니다."""

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32

    def read_foreground(self) -> WindowContext:
        try:
            hwnd = self._user32.GetForegroundWindow()
            if not hwnd:
                return WindowContext(error="No foreground window.")

            pid = wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            title = self._read_window_title(hwnd)
            rect = self._read_window_rect(hwnd)
            process_path = self._read_process_path(pid.value)

            return WindowContext(
                hwnd=int(hwnd),
                pid=int(pid.value),
                process_name=Path(process_path).name if process_path else "",
                process_path=process_path,
                window_title=title,
                rect=rect,
            )
        except Exception as exc:
            return WindowContext(error=str(exc))

    def _read_window_title(self, hwnd: int) -> str:
        length = self._user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def _read_window_rect(self, hwnd: int) -> BoundingBox:
        rect = wintypes.RECT()
        self._user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return BoundingBox(
            x=int(rect.left),
            y=int(rect.top),
            width=int(rect.right - rect.left),
            height=int(rect.bottom - rect.top),
        )

    def _read_process_path(self, pid: int) -> str:
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = self._kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""

        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            ok = self._kernel32.QueryFullProcessImageNameW(
                handle,
                0,
                buffer,
                ctypes.byref(size),
            )
            return buffer.value if ok else ""
        finally:
            self._kernel32.CloseHandle(handle)
