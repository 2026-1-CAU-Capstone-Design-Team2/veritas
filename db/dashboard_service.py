from __future__ import annotations

from .dashboard_repository import (
	get_dashboard_summary as fetch_dashboard_summary,
	get_recent_activities,
	get_recent_workspaces,
)


def get_dashboard_summary() -> dict[str, object]:
	summary = fetch_dashboard_summary()
	total_docs = summary["total_docs"]
	feedback_completed_docs = summary["feedback_completed_docs"]
	feedback_rate = 0 if total_docs == 0 else round((feedback_completed_docs / total_docs) * 100)

	return {
		"processed_docs": summary["processed_docs"],
		"validated_workspaces": summary["validated_workspaces"],
		"feedback_rate": feedback_rate,
		"recent_workspaces": get_recent_workspaces(limit=5),
		"recent_activities": get_recent_activities(limit=5),
	}

