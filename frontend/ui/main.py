from __future__ import annotations

from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


def main() -> None:
	app = QApplication([])
	app.setApplicationName("VERITAS")

	window = MainWindow()
	window.show()

	app.exec()


if __name__ == "__main__":
	main()
