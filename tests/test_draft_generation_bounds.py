from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from api.services import draft_forms


class LengthMaxTokensTests(unittest.TestCase):
    def test_every_length_has_an_output_budget(self) -> None:
        for length in draft_forms.LENGTHS:
            budget = draft_forms.resolve_length_max_tokens(length)
            self.assertGreater(budget, 0)

    def test_unknown_length_falls_back_to_default_budget(self) -> None:
        self.assertEqual(
            draft_forms.resolve_length_max_tokens("이상한값"),
            draft_forms.LENGTH_MAX_TOKENS[draft_forms.DEFAULT_LENGTH],
        )
        self.assertEqual(
            draft_forms.resolve_length_max_tokens(None),
            draft_forms.LENGTH_MAX_TOKENS[draft_forms.DEFAULT_LENGTH],
        )

    def test_budgets_increase_with_length(self) -> None:
        self.assertLess(
            draft_forms.LENGTH_MAX_TOKENS["짧게"],
            draft_forms.LENGTH_MAX_TOKENS["보통"],
        )
        self.assertLess(
            draft_forms.LENGTH_MAX_TOKENS["보통"],
            draft_forms.LENGTH_MAX_TOKENS["길게"],
        )


class RecordingLLM:
    """Captures the kwargs of every ask() call; returns a canned draft."""

    n_ctx = 50_000
    model = "fake-local-model"

    def __init__(self) -> None:
        self.ask_calls: list[dict] = []

    def ask(self, system_prompt, user_prompt, **kwargs):
        self.ask_calls.append(kwargs)
        return "# 생성된 초안\n\n본문입니다."


class FakeRuntime:
    def __init__(self, llm) -> None:
        self.llm = llm
        self.workspace_id = "ws1"
        self.rag_service = None


class DraftGenerationBoundsTests(unittest.TestCase):
    """초안 생성 LLM 호출이 무한정 실행될 수 없도록 하는 경계 조건들.

    회귀 배경: max_tokens / timeout 없이 reasoning=True 로 호출되어, 작은 로컬
    모델이 thinking + 무제한 생성으로 10분(프론트/백엔드 동시 timeout)을
    넘기던 문제.
    """

    def _generate(self, tmp: str, llm: RecordingLLM, payload_overrides: dict | None = None):
        from api.services import draft_service

        workspace_dir = Path(tmp) / "ws1"
        (workspace_dir / "summary").mkdir(parents=True)
        (workspace_dir / "summary" / "batch_001.md").write_text(
            "=== batch ===\n근거 내용입니다.", encoding="utf-8"
        )

        payload = {
            "source": "custom",
            "outline": ["개요", "본문", "결론"],
            "tone": "중립",
            "length": "보통",
        }
        payload.update(payload_overrides or {})

        runtime = FakeRuntime(llm)
        with mock.patch.object(draft_service, "get_runtime", return_value=runtime), mock.patch.dict(
            "os.environ", {"VERITAS_OUTPUT_DIR": tmp}
        ), mock.patch.object(
            # 활동 로그는 DB 의존 — 이 테스트의 관심사가 아니므로 차단.
            draft_service.activity, "log_activity", lambda *args, **kwargs: None
        ):
            return draft_service.generate_builtin_draft("ws1", payload)

    def test_draft_call_is_bounded(self) -> None:
        llm = RecordingLLM()
        with tempfile.TemporaryDirectory() as tmp:
            result = self._generate(tmp, llm)

        self.assertEqual(len(llm.ask_calls), 1)
        kwargs = llm.ask_calls[0]

        # ① 출력 토큰 상한 — 길이 설정(보통)의 예산이 sampling_params 에 들어간다.
        sampling_params = kwargs.get("sampling_params") or {}
        self.assertEqual(
            sampling_params.get("max_tokens"),
            draft_forms.LENGTH_MAX_TOKENS["보통"],
        )

        # ② thinking 모드 OFF — 작은 로컬 모델이 <think> 블록으로 시간/예산을
        #    태우지 않는다.
        self.assertFalse(kwargs.get("reasoning"))

        # ③ 요청 timeout — 프론트엔드 600초보다 작은 명시적 한도.
        timeout_sec = kwargs.get("timeout_sec")
        self.assertIsNotNone(timeout_sec)
        self.assertLess(float(timeout_sec), 600.0)

        # 생성 결과는 정상 저장된다.
        self.assertTrue(result["content"].startswith("# 생성된 초안"))

    def test_short_length_uses_smaller_budget(self) -> None:
        llm = RecordingLLM()
        with tempfile.TemporaryDirectory() as tmp:
            self._generate(tmp, llm, {"length": "짧게"})

        sampling_params = llm.ask_calls[0].get("sampling_params") or {}
        self.assertEqual(
            sampling_params.get("max_tokens"),
            draft_forms.LENGTH_MAX_TOKENS["짧게"],
        )

    def test_tone_sampling_params_are_preserved_alongside_budget(self) -> None:
        # max_tokens 추가가 기존 톤별 샘플링 값을 덮어쓰지 않아야 한다.
        llm = RecordingLLM()
        with tempfile.TemporaryDirectory() as tmp:
            self._generate(tmp, llm, {"tone": "격식체"})

        sampling_params = llm.ask_calls[0].get("sampling_params") or {}
        profile = draft_forms.TONE_PROFILES["격식체"]["samplingParams"]
        for key, value in profile.items():
            self.assertEqual(sampling_params.get(key), value)
        self.assertIn("max_tokens", sampling_params)


if __name__ == "__main__":
    unittest.main()
