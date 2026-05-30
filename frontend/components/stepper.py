from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from ..theme import theme


class StepNode(QFrame):
	def __init__(self, label: str, index: int, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.index = index
		self._status = "pending"

		self.setObjectName("StepNode")
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
		self.setFixedHeight(38)

		wrapper = QHBoxLayout(self)
		wrapper.setContentsMargins(12, 6, 12, 6)
		wrapper.setSpacing(0)

		self.text = QLabel(label)
		self.text.setObjectName("StepText")
		self.text.setAlignment(Qt.AlignCenter)

		wrapper.addWidget(self.text, 1)

		self._apply_style()

	def set_status(self, status: str) -> None:
		self._status = status
		self._apply_style()

	def _apply_style(self, *args) -> None:
		if self._status == "active":
			bg = theme.color("stepper.active.bg")
			border = theme.color("stepper.active.border")
			text_color = theme.color("stepper.active.text")
			weight = 800
		elif self._status == "done":
			bg = theme.color("stepper.done.bg")
			border = theme.color("stepper.done.border")
			text_color = theme.color("stepper.done.text")
			weight = 700
		else:
			bg = theme.color("stepper.pending.bg")
			border = theme.color("stepper.pending.border")
			text_color = theme.color("stepper.pending.text")
			weight = 600

		self.setStyleSheet(
			f"""
			QFrame#StepNode {{
				background-color: {bg};
				border: 1px solid {border};
				border-radius: 19px;
			}}
			"""
		)

		self.text.setStyleSheet(
			f"""
			QLabel#StepText {{
				color: {text_color};
				font-size: 12px;
				font-weight: {weight};
				background-color: transparent;
			}}
			"""
		)


class WorkflowStepper(QFrame):
	def __init__(self, steps: list[str], parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("WorkflowStepper")
		self._nodes: list[StepNode] = []
		self._connectors: list[QFrame] = []

		layout = QHBoxLayout(self)
		layout.setContentsMargins(16, 14, 16, 14)
		layout.setSpacing(8)

		for index, step in enumerate(steps):
			node = StepNode(step, index)
			self._nodes.append(node)
			layout.addWidget(node, 1)

			if index < len(steps) - 1:
				connector = QFrame()
				connector.setObjectName("StepperConnector")
				connector.setFixedHeight(3)
				connector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
				self._connectors.append(connector)
				layout.addWidget(connector, 1)

		self._current = 0
		self.set_current_step(0)
		# Re-tint nodes + connectors when the theme toggles.
		theme.themeChanged.connect(self._on_theme_changed)

	def _on_theme_changed(self, *args) -> None:
		self.set_current_step(self._current)

	def set_current_step(self, index: int) -> None:
		self._current = index
		for i, node in enumerate(self._nodes):
			if i < index:
				node.set_status("done")
			elif i == index:
				node.set_status("active")
			else:
				node.set_status("pending")

		# Keep connector color synchronized with highlighted chips.
		# Show immediate feedback on the first step by coloring the first connector.
		colored_count = max(1, index)

		for i, connector in enumerate(self._connectors):
			if i < colored_count:
				color = theme.color("stepper.connector.on")
			else:
				color = theme.color("stepper.connector.off")
			connector.setStyleSheet(
				f"""
				QFrame#StepperConnector {{
					background-color: {color};
					border-radius: 2px;
				}}
				"""
			)
