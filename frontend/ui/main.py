from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import QApplication

from ..api_common import API_BASE_URL, load_bootstrap_state
from ..backend_server import ensure_api_server
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
	ensure_api_server(API_BASE_URL)
	try:
		load_bootstrap_state()
	except Exception:
		pass
	app = QApplication([])
	app.setApplicationName("VERITAS")

	window = MainWindow()
	window.show()

	app.exec()


if __name__ == "__main__":
	main()
