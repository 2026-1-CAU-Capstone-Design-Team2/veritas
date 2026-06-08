from __future__ import annotations

import unittest
from unittest.mock import patch

from llm.context_settings import CONTEXT_TIERS
from llm.hardware_policy import (
    RISK_RISKY,
    estimate_runtime,
    max_parallel_slots,
    model_fit_context_tokens,
    recommended_context_tokens,
)
from llm.model_catalog import get_model, llm_models
from llm.model_manager import _expand_split_gguf
from llm.llama_supervisor import _common_args, _context_flag, _ngl_retry_values, effective_context_per_slot


class ModelCatalogVariantTests(unittest.TestCase):
    def test_catalog_contains_requested_size_and_quantization_variants(self) -> None:
        ids = {model.id for model in llm_models()}
        for model_id in (
            "qwen35-0.8b-bf16",
            "qwen35-0.8b-q8_0",
            "qwen35-2b-q4",
            "qwen35-4b-q6",
            "qwen35-9b-q5",
            "qwen35-27b-bf16",
            "qwen35-27b-q3",
            "qwen35-35b-a3b-bf16",
            "qwen35-35b-a3b-q2",
        ):
            self.assertIn(model_id, ids)

    def test_legacy_aliases_resolve_to_catalog_models(self) -> None:
        self.assertEqual(get_model("qwen35-4b-q4_k_m", kind="llm").id, "qwen35-4b-q4")
        self.assertEqual(get_model("qwen35-27b-q4_k_m", kind="llm").id, "qwen35-27b-q4")
        self.assertEqual(get_model("qwen35-27b-q16", kind="llm").id, "qwen35-27b-bf16")
        self.assertEqual(
            get_model("qwen35-35b-q4", kind="llm").id,
            "qwen35-35b-a3b-q4",
        )
        self.assertEqual(
            get_model("qwen35-35b-f16", kind="llm").id,
            "qwen35-35b-a3b-bf16",
        )

    def test_split_gguf_expansion_collects_all_shards(self) -> None:
        matches = [
            "BF16/Qwen3.5-27B-BF16-00002-of-00002.gguf",
            "BF16/Qwen3.5-27B-BF16-00001-of-00002.gguf",
        ]
        self.assertEqual(
            _expand_split_gguf(matches[1], matches),
            [
                "BF16/Qwen3.5-27B-BF16-00001-of-00002.gguf",
                "BF16/Qwen3.5-27B-BF16-00002-of-00002.gguf",
            ],
        )


class HardwarePolicyTests(unittest.TestCase):
    def test_parallel_cap_respects_model_context_limit(self) -> None:
        model = get_model("qwen35-0.8b-q8_0", kind="llm")
        limit = max_parallel_slots(
            model,
            context_per_slot_tokens=90_000,
            available_bytes=512 * 1024**3,
            prefer_installed_file=False,
        )
        self.assertEqual(limit, 2)

    def test_estimate_marks_large_model_risky_on_low_ram(self) -> None:
        model = get_model("qwen35-35b-a3b-q8_0", kind="llm")
        estimate = estimate_runtime(
            model,
            context_per_slot_tokens=32_768,
            parallel_slots=1,
            available_bytes=16 * 1024**3,
            prefer_installed_file=False,
        )
        self.assertEqual(estimate.risk, RISK_RISKY)

    def test_recommendation_returns_known_context_tier(self) -> None:
        model = get_model("qwen35-9b-q4", kind="llm")
        tokens = recommended_context_tokens(
            model,
            context_tiers=CONTEXT_TIERS,
            available_bytes=32 * 1024**3,
            parallel_slots=1,
            app_limit=90_000,
            prefer_installed_file=False,
        )
        self.assertIn(tokens, CONTEXT_TIERS)
        self.assertLessEqual(tokens, 90_000)

    def test_model_fit_context_caps_small_and_mid_models(self) -> None:
        self.assertEqual(
            model_fit_context_tokens(get_model("qwen35-0.8b-q8_0", kind="llm")),
            16384,
        )
        self.assertEqual(
            model_fit_context_tokens(get_model("qwen35-4b-q4", kind="llm")),
            16384,
        )
        self.assertEqual(
            model_fit_context_tokens(get_model("qwen35-9b-q4", kind="llm")),
            16384,
        )
        self.assertEqual(
            model_fit_context_tokens(get_model("qwen35-27b-q4", kind="llm")),
            32768,
        )
        self.assertEqual(
            model_fit_context_tokens(get_model("qwen35-35b-a3b-q4", kind="llm")),
            50000,
        )

    def test_recommendation_prioritizes_five_parallel_slots_over_max_context(self) -> None:
        model = get_model("qwen35-9b-q4", kind="llm")
        tokens = recommended_context_tokens(
            model,
            context_tiers=CONTEXT_TIERS,
            available_bytes=96 * 1024**3,
            parallel_slots=1,
            app_limit=90_000,
            prefer_installed_file=False,
        )
        self.assertEqual(tokens, 16_384)
        self.assertEqual(
            max_parallel_slots(
                model,
                context_per_slot_tokens=tokens,
                available_bytes=96 * 1024**3,
                prefer_installed_file=False,
            ),
            5,
        )

    def test_large_model_auto_context_still_preserves_five_parallel_slots(self) -> None:
        model = get_model("qwen35-35b-a3b-q4", kind="llm")
        tokens = recommended_context_tokens(
            model,
            context_tiers=CONTEXT_TIERS,
            available_bytes=128 * 1024**3,
            parallel_slots=1,
            app_limit=90_000,
            prefer_installed_file=False,
        )
        self.assertEqual(tokens, 50_000)
        self.assertEqual(
            max_parallel_slots(
                model,
                context_per_slot_tokens=tokens,
                available_bytes=128 * 1024**3,
                prefer_installed_file=False,
            ),
            5,
        )

    def test_llama_common_args_try_full_gpu_offload_by_default(self) -> None:
        with patch.dict("os.environ", {"VERITAS_LLAMA_NP": "1"}, clear=True):
            args = _common_args("llm")
        self.assertEqual(args[args.index("-ngl") + 1], "99")
        self.assertEqual(args[args.index("-np") + 1], "1")

    def test_llama_memory_context_uses_per_slot_context(self) -> None:
        with (
            patch.dict("os.environ", {"VERITAS_LLAMA_CTX": "40960"}, clear=True),
            patch("llm.llama_supervisor._np_flag", return_value="5"),
        ):
            self.assertEqual(_context_flag("llm"), "40960")
            self.assertEqual(effective_context_per_slot("llm"), 8192)

    def test_llama_common_args_respect_explicit_gpu_layer_override(self) -> None:
        with patch.dict(
            "os.environ",
            {"VERITAS_LLAMA_NGL": "0", "VERITAS_LLAMA_NP": "1"},
            clear=True,
        ):
            args = _common_args("llm")
        self.assertEqual(args[args.index("-ngl") + 1], "0")

    def test_llama_gpu_cpu_mode_disables_offload_retries(self) -> None:
        with patch.dict(
            "os.environ",
            {"VERITAS_LLAMA_GPU_MODE": "cpu", "VERITAS_LLAMA_NP": "1"},
            clear=True,
        ):
            self.assertEqual(_ngl_retry_values(), ("0",))
            args = _common_args("llm")
        self.assertEqual(args[args.index("-ngl") + 1], "0")


if __name__ == "__main__":
    unittest.main()
