"""Regression test: a workspace switch drives the live runtime, not just state.

The 문서 보조 list kept showing the previous workspace's proactive assist cards
after switching because ``workspaces_service.switch_workspace`` only updated the
persisted "current workspace" — it never told the AgentRuntime to switch. The
screen-event buffer is filtered by ``runtime.workspace_id``, so a stale runtime
kept serving the old workspace's events. This guards the wiring that fixes it.
"""
from __future__ import annotations

import contextlib
import unittest
from unittest import mock

from fastapi import HTTPException

import api.services.workspaces_service as ws


class _FakeRuntime:
    def __init__(self) -> None:
        self.switched_to: str | None = None

    def set_workspace(self, workspace_id: str) -> None:
        self.switched_to = workspace_id


class SwitchWorkspaceRuntimeTests(unittest.TestCase):
    def _common_patches(self, runtime_factory):
        return [
            mock.patch.object(ws, "_sync_run_workspaces", lambda: None),
            mock.patch.object(ws, "_ensure_current_workspace", lambda *_a, **_k: None),
            mock.patch.object(ws, "_save_current_workspace_id", lambda *_a, **_k: None),
            mock.patch.object(ws.repo, "list_workspaces", lambda: []),
            mock.patch.object(
                ws.repo, "find_workspace", lambda wid: {"workspaceId": wid, "name": wid}
            ),
            mock.patch.object(ws.repo, "set_current_workspace", lambda *_a, **_k: None),
            mock.patch("api.services.agent_runtime.get_runtime", runtime_factory),
        ]

    def test_switch_drives_runtime_set_workspace(self) -> None:
        fake = _FakeRuntime()
        with contextlib.ExitStack() as stack:
            for patch in self._common_patches(lambda: fake):
                stack.enter_context(patch)
            result = ws.switch_workspace("World_Model")
        self.assertEqual(fake.switched_to, "World_Model")
        self.assertEqual(result["workspaceId"], "World_Model")

    def test_switch_survives_runtime_unavailable(self) -> None:
        def _boom():
            raise HTTPException(status_code=503, detail="runtime down")

        with contextlib.ExitStack() as stack:
            for patch in self._common_patches(_boom):
                stack.enter_context(patch)
            # Must not raise: the persisted switch still stands when the runtime
            # can't be acquired; it adopts the workspace on next use.
            result = ws.switch_workspace("3D_Gaussian_Splatting")
        self.assertEqual(result["workspaceId"], "3D_Gaussian_Splatting")


if __name__ == "__main__":
    unittest.main()
