from __future__ import annotations

from typing import Any


TEXT_EXTRACTION_PROCESS_NAMES = frozenset(
    {
        "notepad.exe",
        "winword.exe",
        "excel.exe",
        "powerpnt.exe",
        "docs.exe",
        "notepad++.exe",
        "notion.exe",
        "word.exe",
        "hwp.exe",
        "code.exe",
        "devenv.exe",
        "pycharm64.exe",
    }
)

TEXT_EXTRACTION_TITLE_EXTENSIONS = (".txt", ".md", ".doc", ".hwp", ".ppt", ".pptx")


def is_text_extraction_target(window: Any) -> bool:
    process_name = (getattr(window, "process_name", "") or "").lower()
    if process_name in TEXT_EXTRACTION_PROCESS_NAMES:
        return True

    title = (getattr(window, "window_title", "") or "").lower()
    return any(
        title.endswith(ext) or ext in title
        for ext in TEXT_EXTRACTION_TITLE_EXTENSIONS
    )
