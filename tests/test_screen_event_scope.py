"""Regression test: screen-assist events are scoped to their workspace.

Guards against the bug where the document-assist window's proactive suggestions
from a previous workspace reappeared after switching workspaces. The screen
event ring buffer is shared across the whole runtime and every poller restart
(including a workspace switch) reads from cursor 0, so ``get_events_since`` must
return only the requested workspace's events.
"""
from __future__ import annotations

import threading
import unittest

from api.services.screen_monitor import ScreenMonitor


def _intervention(event_id: str) -> dict:
    return {"event_id": event_id, "writing_context": {}, "app_context": {}}


class ScreenEventWorkspaceScopeTests(unittest.TestCase):
    def _monitor_with_two_workspaces(self) -> ScreenMonitor:
        mon = ScreenMonitor(workspace_lock=threading.RLock())
        mon.record_assist_answer(
            "World model answer", _intervention("wm1"), workspace_id="World_Model"
        )
        mon.record_assist_answer(
            "Gaussian splatting answer", _intervention("gs1"),
            workspace_id="3D_Gaussian_Splatting",
        )
        return mon

    def test_events_filtered_by_workspace(self) -> None:
        mon = self._monitor_with_two_workspaces()

        wm = mon.get_events_since(since=0, limit=20, workspace_id="World_Model")
        gs = mon.get_events_since(since=0, limit=20, workspace_id="3D_Gaussian_Splatting")

        self.assertEqual([e["workspaceId"] for e in wm["items"]], ["World_Model"])
        self.assertEqual(
            [e["workspaceId"] for e in gs["items"]], ["3D_Gaussian_Splatting"]
        )

    def test_switch_then_repoll_from_cursor_zero_excludes_old_workspace(self) -> None:
        # Simulates the bug's trigger: after switching to 3DGS, the assist window's
        # poller restarts at cursor 0 and must NOT receive the World_Model answers.
        mon = self._monitor_with_two_workspaces()

        repoll = mon.get_events_since(since=0, limit=20, workspace_id="3D_Gaussian_Splatting")
        answers = [e["answer"] for e in repoll["items"]]

        self.assertIn("Gaussian splatting answer", answers)
        self.assertNotIn("World model answer", answers)


if __name__ == "__main__":
    unittest.main()
