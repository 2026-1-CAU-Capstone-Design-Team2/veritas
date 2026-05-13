from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from ..api_common import api_client


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

	def get_chat_history(self, workspace_id: str) -> list[dict[str, Any]]:
		response = api_client.get(f"/api/v1/chat/sessions/session_{workspace_id}/messages")
		items = response.get("items", [])
		return items if isinstance(items, list) else []

	def generate_draft(self, workspace_id: str, prompt: str) -> dict[str, Any]:
		return api_client.post(
			"/api/v1/draft/generate",
			{"workspaceId": workspace_id, "prompt": prompt},
		)

	def run_research(
		self,
		workspace_id: str,
		instruction: str,
		reference_urls: list[str],
	) -> dict[str, Any]:
		return api_client.post(
			"/api/v1/research/jobs",
			{
				"workspaceId": workspace_id,
				"instruction": instruction,
				"referenceUrls": reference_urls,
			},
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

	def get_screen_monitoring_events(self, since: int = 0, limit: int = 20) -> dict[str, Any]:
		return api_client.get(
			"/api/v1/screen-monitoring/events",
			{"since": since, "limit": limit},
		)
