from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

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
	_reconcile_workspaces_with_runs()

	# Create the QApplication first so we can show a real error dialog if the
	# API server is unreachable, instead of silently spinning up a hidden
	# in-process backend (the old behavior, which masked port mismatches).
	app = QApplication([])
	app.setApplicationName("VERITAS")

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
