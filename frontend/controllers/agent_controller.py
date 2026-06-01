from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

from ..api_common import api_client


# An AutoSurvey run blocks the POST /research/jobs request for the whole
# workflow (the route is synchronous by design). Allow a long ceiling so a
# normal multi-document run on a local model is not cut off mid-run by the
# default request timeout — the run continues server-side regardless, but a
# client-side timeout would surface as a spurious "오류" in the UI.
RESEARCH_JOB_TIMEOUT_SEC = float(os.getenv("VERITAS_RESEARCH_JOB_TIMEOUT_SEC", "5400"))

# Verification is much shorter than AutoSurvey (15-60s for a typical workspace
# per VERIFY_DESIGN.md §9) but still blocks the request; the ceiling here is
# the same 5400s headroom used for research so even a giant workspace cannot
# trip a spurious client timeout.
VERIFY_JOB_TIMEOUT_SEC = float(os.getenv("VERITAS_VERIFY_JOB_TIMEOUT_SEC", "5400"))


class AgentController:
	"""Controller boundary between PySide views and the HTTP API."""

	def send_chat_message(self, workspace_id: str, message: str, mode: str) -> str:
		response = api_client.post(
			"/api/v1/chat/messages",
			{"workspaceId": workspace_id, "message": message, "mode": mode},
		)
		return str(response.get("assistant") or "")

	def stream_chat_message(
		self,
		workspace_id: str,
		message: str,
		mode: str,
	) -> Iterator[tuple[str, dict[str, Any]]]:
		return api_client.stream_post_sse(
			"/api/v1/chat/messages/stream",
			{"workspaceId": workspace_id, "message": message, "mode": mode},
		)

	def get_research_progress(self, since: int = 0, limit: int = 50) -> dict[str, Any]:
		return api_client.get(
			"/api/v1/research/progress",
			{"since": since, "limit": limit},
		)

	def delete_workspace(self, workspace_id: str) -> dict[str, Any]:
		return api_client.delete(f"/api/v1/workspaces/{workspace_id}")

	def get_chat_history(self, workspace_id: str) -> list[dict[str, Any]]:
		response = api_client.get(f"/api/v1/chat/sessions/session_{workspace_id}/messages")
		items = response.get("items", [])
		return items if isinstance(items, list) else []

	def generate_draft(self, workspace_id: str, prompt: str) -> dict[str, Any]:
		return api_client.post(
			"/api/v1/draft/generate",
			{"workspaceId": workspace_id, "prompt": prompt},
		)

	def get_draft_forms(self) -> dict[str, Any]:
		return api_client.get("/api/v1/draft/forms")

	def import_draft_form(self, file_path: Path) -> dict[str, Any]:
		"""Upload a form file and get back its extracted md template + outline.

		Supports .docx/.doc/.hwp/.hwpx/.pdf — the backend strips body prose and
		keeps only structure (headings/bullets/tables).
		"""
		return api_client.upload_files("/api/v1/draft/forms/import", [file_path])

	def generate_builtin_draft(self, workspace_id: str, settings: dict[str, Any]) -> dict[str, Any]:
		"""Generate a built-in form draft from structured wizard settings.

		The backend maps ``tone`` to a sampling strategy, grounds on the
		workspace knowledge base, and persists the settings as
		``drafts/draft_<n>_settings.json`` for later regeneration.
		"""
		return api_client.post(
			"/api/v1/draft/builtin/generate",
			{**settings, "workspaceId": workspace_id},
		)

	def regenerate_builtin_draft(self, workspace_id: str, draft_number: int) -> dict[str, Any]:
		return api_client.post(
			"/api/v1/draft/builtin/regenerate",
			{"workspaceId": workspace_id, "draftNumber": int(draft_number)},
		)

	def list_builtin_drafts(self, workspace_id: str) -> dict[str, Any]:
		return api_client.get("/api/v1/draft/builtin/list", {"workspaceId": workspace_id})

	def run_research(
		self,
		workspace_id: str,
		instruction: str,
		reference_urls: list[str],
		max_docs: int | None = None,
		scout_docs: int | None = None,
		collect_batch_size: int | None = None,
	) -> dict[str, Any]:
		payload: dict[str, Any] = {
			"workspaceId": workspace_id,
			"instruction": instruction,
			"referenceUrls": reference_urls,
		}
		if max_docs is not None:
			payload["maxDocs"] = int(max_docs)
		if scout_docs is not None:
			payload["scoutDocs"] = int(scout_docs)
		if collect_batch_size is not None:
			payload["collectBatchSize"] = int(collect_batch_size)
		return api_client.post(
			"/api/v1/research/jobs",
			payload,
			timeout=RESEARCH_JOB_TIMEOUT_SEC,
		)

	def list_research_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
		response = api_client.get("/api/v1/research/jobs", {"limit": limit})
		items = response.get("items", [])
		return items if isinstance(items, list) else []

	def upload_feedback_files(self, files: list[Path]) -> list[dict[str, str]]:
		response = api_client.upload_files("/api/v1/feedback/files", files)
		items = response.get("items", [])
		return items if isinstance(items, list) else []

	def analyze_feedback(self, file_ids: list[str]) -> dict[str, Any]:
		return api_client.post("/api/v1/feedback/analyze", {"fileIds": file_ids})

	def get_feedback_result(self, file_id: str) -> dict[str, Any]:
		return api_client.get(f"/api/v1/feedback/results/{file_id}")

	def update_model(self, model_id: str) -> dict[str, Any]:
		return api_client.put(
			"/api/v1/settings/model",
			{"modelId": model_id},
		)

	def get_model_switch_progress(self, since: int = 0, limit: int = 50) -> dict[str, Any]:
		return api_client.get(
			"/api/v1/settings/model/progress",
			{"since": since, "limit": limit},
		)

	def update_research_method(self, sample_count: int, plan_count: int) -> dict[str, Any]:
		return api_client.put(
			"/api/v1/settings/research-method",
			{"sampleCount": int(sample_count), "planCount": int(plan_count)},
		)

	def update_autosurvey_openai_api_key(
		self,
		api_key: str = "",
		*,
		clear: bool = False,
	) -> dict[str, Any]:
		return api_client.put(
			"/api/v1/settings/autosurvey-openai",
			{"apiKey": str(api_key or ""), "clear": bool(clear)},
		)

	def update_llm_parallel(self, value: int) -> dict[str, Any]:
		return api_client.put(
			"/api/v1/settings/llm-parallel",
			{"value": int(value)},
		)

	def get_document_summary(self, workspace_id: str) -> str:
		response = api_client.get(f"/api/v1/documents/{workspace_id}/summary")
		return str(response.get("summary") or "")

	def get_document_merged(self, workspace_id: str) -> str:
		response = api_client.get(f"/api/v1/documents/{workspace_id}/merged")
		return str(response.get("mergedText") or "")

	def analyze_document(self, workspace_id: str, text: str, cursor: int | None = None) -> dict[str, Any]:
		return api_client.post(
			"/api/v1/document-assist/analyze",
			{"workspaceId": workspace_id, "text": text, "cursor": cursor},
		)

	def send_document_assist_message(self, workspace_id: str, message: str, mode: str) -> str:
		response = api_client.post(
			"/api/v1/document-assist/chat/messages",
			{"workspaceId": workspace_id, "message": message, "mode": mode},
		)
		return str(response.get("reply") or "")

	def start_screen_monitoring(self, workspace_id: str | None = None) -> dict[str, Any]:
		payload: dict[str, Any] = {}
		if workspace_id:
			payload["workspaceId"] = workspace_id
		return api_client.post("/api/v1/screen-monitoring/start", payload)

	def stop_screen_monitoring(self) -> dict[str, Any]:
		return api_client.post("/api/v1/screen-monitoring/stop", {})

	def get_screen_monitoring_status(self) -> dict[str, Any]:
		return api_client.get("/api/v1/screen-monitoring/status")

	def get_screen_monitoring_events(
		self, since: int = 0, limit: int = 20, workspace_id: str | None = None
	) -> dict[str, Any]:
		# Carry the active workspace so the backend keeps the screen runtime bound
		# to it (continuous sync, mirroring how chat sets the workspace per message).
		params: dict[str, Any] = {"since": since, "limit": limit}
		if workspace_id:
			params["workspaceId"] = workspace_id
		return api_client.get("/api/v1/screen-monitoring/events", params)

	def submit_proactive_feedback(
		self,
		decision_id: str,
		action: str,
		metadata: dict[str, Any] | None = None,
	) -> dict[str, Any]:
		"""Send one canonical-feedback action to the proactive bandit.

		``action`` is the raw surface-specific string (``tab`` / ``esc`` /
		``retry`` / ``timeout`` for the native editor, ``copy`` /
		``red_reject`` / ``retry`` for external cards). The backend's
		``services.proactive.reward`` collapses it onto the canonical
		feedback before reward shaping.
		"""
		return api_client.post(
			"/api/v1/proactive/feedback",
			{
				"decisionId": decision_id,
				"action": action,
				"metadata": dict(metadata or {}),
			},
		)

	def submit_screen_feedback(
		self, event_id: str, intervention_type: str, action: str
	) -> dict[str, Any]:
		return api_client.post(
			"/api/v1/screen-monitoring/feedback",
			{
				"eventId": event_id,
				"interventionType": intervention_type,
				"action": action,
			},
		)

	# -- verification --------------------------------------------------------
	# Mirrors the research controller surface: a long-running POST plus a fast
	# cursor-based progress poll, with read-only list/detail/summary endpoints
	# the page can call without blocking on a run.

	def run_verification(
		self,
		workspace_id: str | None = None,
		tasks: list[str] | None = None,
	) -> dict[str, Any]:
		payload: dict[str, Any] = {}
		if workspace_id:
			payload["workspaceId"] = workspace_id
		if tasks:
			payload["tasks"] = list(tasks)
		return api_client.post(
			"/api/v1/verify/jobs",
			payload,
			timeout=VERIFY_JOB_TIMEOUT_SEC,
		)

	def get_verify_progress(self, since: int = 0, limit: int = 50) -> dict[str, Any]:
		return api_client.get(
			"/api/v1/verify/progress",
			{"since": since, "limit": limit},
		)

	def get_verify_summary(self, workspace_id: str | None = None) -> dict[str, Any]:
		params: dict[str, Any] = {}
		if workspace_id:
			params["workspaceId"] = workspace_id
		return api_client.get("/api/v1/verify/summary", params)

	def list_verify_results(
		self,
		workspace_id: str | None = None,
		level: str | None = None,
		page: int = 1,
		page_size: int = 100,
	) -> dict[str, Any]:
		params: dict[str, Any] = {"page": int(page), "pageSize": int(page_size)}
		if workspace_id:
			params["workspaceId"] = workspace_id
		if level and level != "전체":
			params["level"] = level
		return api_client.get("/api/v1/verify/results", params)

	def get_verify_detail(
		self,
		doc_id: str,
		workspace_id: str | None = None,
	) -> dict[str, Any]:
		params: dict[str, Any] = {}
		if workspace_id:
			params["workspaceId"] = workspace_id
		return api_client.get(f"/api/v1/verify/results/{doc_id}", params)
