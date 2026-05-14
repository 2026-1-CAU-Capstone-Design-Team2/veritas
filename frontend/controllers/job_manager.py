"""Central async job manager for the PySide frontend.

Design
------
Every long-running call to the backend (LLM inference, AutoSurvey, workspace
rebuild, feedback analysis, ...) goes through this manager so we never run
blocking work on the UI thread. The manager also owns the "busy" model that
the UI binds to so incompatible operations are *prevented* at input time —
e.g. while AutoSurvey is running, the chat input is automatically disabled
and the run-research button itself stays disabled.

The pattern:

1. Each operation is tagged with a :class:`JobCategory` constant.
2. ``submit(category, fn, ...)`` runs ``fn`` on a worker QThread, marks the
   category as active for the lifetime of the call, and emits ``busy_changed``
   on transition. Returns ``False`` immediately if any *blocker* of the
   requested category is already active.
3. For operations that already have a bespoke worker (the chat SSE stream),
   callers use ``register(category)`` / ``unregister(category)`` to plug
   their own threads into the same exclusion model.
4. ``is_blocked(category)`` is the *single source of truth* the views consult
   when deciding whether to enable their buttons / inputs.

The block matrix (which existing categories prevent which new ones) lives in
one place — :data:`_BLOCKS_THIS` — so adding a new category is a one-line
change. This avoids ad-hoc disable logic scattered across pages.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal


class JobCategory:
	"""Plain string constants — easy to log, serialize, and pass over signals."""

	RESEARCH = "research"
	CHAT = "chat"
	DRAFT = "draft"
	FEEDBACK = "feedback"
	DOC_ANALYZE = "doc_analyze"
	WORKSPACE_SWITCH = "workspace_switch"


# "If any of these are active, this category cannot start."
# Reading: _BLOCKS_THIS[CHAT] == {RESEARCH, CHAT} → chat is blocked by an
# in-flight research run AND by another in-flight chat (one at a time).
_BLOCKS_THIS: dict[str, set[str]] = {
	JobCategory.RESEARCH: {
		JobCategory.RESEARCH,
		JobCategory.CHAT,
		JobCategory.DRAFT,
		JobCategory.FEEDBACK,
		JobCategory.DOC_ANALYZE,
		JobCategory.WORKSPACE_SWITCH,
	},
	JobCategory.CHAT: {JobCategory.RESEARCH, JobCategory.CHAT},
	JobCategory.DRAFT: {JobCategory.RESEARCH, JobCategory.DRAFT},
	JobCategory.FEEDBACK: {JobCategory.RESEARCH, JobCategory.FEEDBACK},
	JobCategory.DOC_ANALYZE: {JobCategory.RESEARCH, JobCategory.DOC_ANALYZE},
	JobCategory.WORKSPACE_SWITCH: {
		JobCategory.RESEARCH,
		JobCategory.WORKSPACE_SWITCH,
	},
}


class _JobThread(QThread):
	"""Runs one callable on a background thread and reports back via signals."""

	succeeded = Signal(object)
	failed = Signal(str)

	def __init__(
		self,
		fn: Callable[..., Any],
		args: tuple,
		kwargs: dict,
		parent: QObject | None = None,
	) -> None:
		super().__init__(parent)
		self._fn = fn
		self._args = args
		self._kwargs = kwargs

	def run(self) -> None:  # type: ignore[override]
		try:
			result = self._fn(*self._args, **self._kwargs)
		except Exception as exc:  # pragma: no cover - surfaced via signal
			self.failed.emit(f"{type(exc).__name__}: {exc}")
			return
		self.succeeded.emit(result)


class JobManager(QObject):
	"""Application-wide busy-state and async-dispatch coordinator.

	All public methods are intended to be called from the main (UI) thread.
	The worker threads created by :meth:`submit` post their results back to
	the main thread through Qt's queued signal connections.
	"""

	busy_changed = Signal()

	_instance: "JobManager | None" = None

	def __init__(self, parent: QObject | None = None) -> None:
		super().__init__(parent)
		self._active: set[str] = set()
		# Detached background loads (chat history, document summary, dashboard
		# data) are parked here so their QThread wrappers are not garbage
		# collected mid-run. Each removes itself on `finished`.
		self._detached: set["_JobThread"] = set()

	@classmethod
	def instance(cls) -> "JobManager":
		if cls._instance is None:
			cls._instance = JobManager()
		return cls._instance

	# ----- queries -----------------------------------------------------------

	def active_categories(self) -> set[str]:
		return set(self._active)

	def is_busy(self, category: str) -> bool:
		return category in self._active

	def is_blocked(self, category: str) -> bool:
		"""Return True if `category` cannot start right now.

		Views call this whenever ``busy_changed`` fires to decide the enabled
		state of their inputs.
		"""
		blockers = _BLOCKS_THIS.get(category, {category})
		return bool(self._active & blockers)

	# ----- lifecycle ---------------------------------------------------------

	def register(self, category: str) -> bool:
		"""Mark a category as active. Returns False if blocked.

		Intended for callers that own a custom worker thread (e.g.
		:class:`ChatStreamWorker` which streams SSE). They pair this with
		:meth:`unregister` when the worker finishes.
		"""
		if self.is_blocked(category):
			return False
		self._active.add(category)
		self.busy_changed.emit()
		return True

	def unregister(self, category: str) -> None:
		if category in self._active:
			self._active.discard(category)
			self.busy_changed.emit()

	def submit(
		self,
		category: str,
		fn: Callable[..., Any],
		*args: Any,
		on_success: Callable[[Any], None] | None = None,
		on_error: Callable[[str], None] | None = None,
		on_done: Callable[[], None] | None = None,
		**kwargs: Any,
	) -> bool:
		"""Run `fn(*args, **kwargs)` on a worker thread.

		Returns ``False`` immediately when the category is blocked by an
		already-active job — callers should treat that as "ignore the click"
		because the matching UI affordance is already disabled.

		Callbacks fire on the main thread:
		    on_success(result)
		    on_error(error_message)
		    on_done()           # always, after success or error
		"""
		if not self.register(category):
			return False

		thread = _JobThread(fn, args, kwargs)

		def _emit_success(result: Any) -> None:
			if on_success is not None:
				on_success(result)

		def _emit_error(message: str) -> None:
			if on_error is not None:
				on_error(message)

		def _emit_finished() -> None:
			# unregister BEFORE on_done so any code in on_done sees the
			# manager already in the post-completion state.
			self.unregister(category)
			if on_done is not None:
				on_done()
			thread.deleteLater()

		thread.succeeded.connect(_emit_success)
		thread.failed.connect(_emit_error)
		thread.finished.connect(_emit_finished)
		thread.start()
		return True

	def run_detached(
		self,
		fn: Callable[..., Any],
		*args: Any,
		on_success: Callable[[Any], None] | None = None,
		on_error: Callable[[str], None] | None = None,
		on_done: Callable[[], None] | None = None,
		**kwargs: Any,
	) -> None:
		"""Run `fn(*args, **kwargs)` on a worker thread with no busy semantics.

		Unlike :meth:`submit`, this does not touch the job-exclusion matrix — it
		is for non-exclusive background *reads* that must keep the UI thread free
		(chat history, document summary, dashboard refresh) but must neither
		block a chat / research run nor be blocked by one. Use it for anything
		that today calls the HTTP API or the local DB straight from the UI
		thread on page navigation.

		Callbacks fire on the main thread:
		    on_success(result)
		    on_error(error_message)
		    on_done()           # always, after success or error
		"""
		thread = _JobThread(fn, args, kwargs)
		self._detached.add(thread)

		def _emit_success(result: Any) -> None:
			if on_success is not None:
				on_success(result)

		def _emit_error(message: str) -> None:
			if on_error is not None:
				on_error(message)

		def _emit_finished() -> None:
			if on_done is not None:
				on_done()
			self._detached.discard(thread)
			thread.deleteLater()

		thread.succeeded.connect(_emit_success)
		thread.failed.connect(_emit_error)
		thread.finished.connect(_emit_finished)
		thread.start()


def get_job_manager() -> JobManager:
	return JobManager.instance()
