from __future__ import annotations

import unittest
from unittest.mock import patch

from llm.context_settings import effective_context_tokens, normalize_context_settings
from llm.hardware_policy import MemorySnapshot
from llm.model_catalog import get_model
from api.services import settings_service


class ContextSettingsTests(unittest.TestCase):
    def test_manual_payload_is_normalized_to_auto_recommendation(self) -> None:
        snapshot = MemorySnapshot(
            total_bytes=64 * 1024**3,
            available_bytes=12 * 1024**3,
        )
        with patch("llm.context_settings.detect_memory", return_value=snapshot):
            context = normalize_context_settings(
                {"mode": "manual", "tokens": 90_000},
            )

        self.assertEqual(context["mode"], "auto")
        self.assertTrue(context["autoOnly"])
        self.assertEqual(context["tokens"], 16_384)
        self.assertEqual(context["lastAutoTokens"], 16_384)

    def test_effective_context_tokens_ignores_persisted_manual_value(self) -> None:
        snapshot = MemorySnapshot(
            total_bytes=64 * 1024**3,
            available_bytes=70 * 1024**3,
        )
        settings = {
            "llamaContext": {
                "mode": "manual",
                "tokens": 8_192,
            }
        }

        with patch.dict("os.environ", {}, clear=True):
            with patch("llm.context_settings.detect_memory", return_value=snapshot):
                tokens = effective_context_tokens(settings)

        self.assertEqual(tokens, 90_000)

    def test_model_aware_auto_context_prefers_fit_over_largest_safe_window(self) -> None:
        snapshot = MemorySnapshot(
            total_bytes=96 * 1024**3,
            available_bytes=80 * 1024**3,
        )
        model = get_model("qwen35-9b-q4", kind="llm")

        with patch("llm.context_settings.detect_memory", return_value=snapshot):
            context = normalize_context_settings(
                {"mode": "manual", "tokens": 90_000},
                model_limit=model.context_tokens,
                model=model,
                parallel_slots=1,
            )

        self.assertEqual(context["mode"], "auto")
        self.assertEqual(context["tokens"], 16_384)
        self.assertEqual(context["hardware"]["maxParallelSlots"], 5)

    def test_settings_service_skips_restart_when_auto_context_is_unchanged(self) -> None:
        with patch.object(
            settings_service.repo,
            "get_settings",
            return_value={"llamaContext": {"mode": "auto", "tokens": 90_000}},
        ):
            with patch.object(
                settings_service.repo,
                "set_llama_context_settings",
                return_value={"mode": "auto", "tokens": 90_000, "autoOnly": True},
            ):
                payload = settings_service.update_llama_context("manual", 8_192)

        self.assertTrue(payload["restartApplied"])
        self.assertTrue(payload["restartSkipped"])
        self.assertEqual(payload["llamaContext"]["tokens"], 90_000)


if __name__ == "__main__":
    unittest.main()
