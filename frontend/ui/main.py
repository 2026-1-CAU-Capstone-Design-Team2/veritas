from __future__ import annotations

from PySide6.QtWidgets import QApplication

from ..api_common import API_BASE_URL, load_bootstrap_state
from ..backend_server import ensure_api_server
from .main_window import MainWindow


def main() -> None:
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
