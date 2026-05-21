from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from core.stdio_utf8 import force_utf8_stdio

from ..api_common import API_BASE_URL, load_bootstrap_state
from ..api_connection import ApiUnavailableError, ensure_api_connection
from .main_window import MainWindow


def _reconcile_workspaces_with_runs() -> None:
	"""Prune SQLite workspace rows whose runs/<id>/ folder was deleted
	while the app was offline. Called once at app launch so the dashboard
	(which reads directly from the local DB) starts with a consistent view.
	"""
	try:
		from db.workspace_sync import reconcile_workspaces_with_disk

		runs_root = Path(os.getenv("VERITAS_OUTPUT_DIR", "runs")).expanduser().resolve()
		reconcile_workspaces_with_disk(runs_root)
	except Exception as e:
		print(f"[workspace][reconcile][warn] {e}")


def main() -> None:
	# Korean Windows defaults piped stdout/stderr to cp949; force UTF-8 so any
	# console log (incl. web-scraped text with em-dashes) cannot crash a print.
	force_utf8_stdio()
	_reconcile_workspaces_with_runs()

	# Create the QApplication first so we can show a real error dialog if the
	# API server is unreachable, instead of silently spinning up a hidden
	# in-process backend (the old behavior, which masked port mismatches).
	app = QApplication([])
	app.setApplicationName("VERITAS")
	# App-wide icon → every window and dialog (QMessageBox 정보 / 단축키 / 단어수 …)
	# shows the Veritas logo in its title bar.
	_icon_path = Path(__file__).resolve().parent / "public" / "images" / "veritas_logo.ico"
	if _icon_path.exists():
		app.setWindowIcon(QIcon(str(_icon_path)))

	try:
		ensure_api_connection(API_BASE_URL)
	except ApiUnavailableError as exc:
		print(f"[api][error] {exc}")
		QMessageBox.critical(None, "API 서버에 연결할 수 없습니다", str(exc))
		return

	try:
		load_bootstrap_state()
	except Exception:
		pass

	window = MainWindow()
	window.show()

	app.exec()


if __name__ == "__main__":
	main()
