"""Cross-check 결과 → 초안 프롬프트 주입 경로 테스트.

verification/crosscheck.json (claims + flags)
  → KnowledgePackBuilder._load_conflict_notes()   — claim 원문을 인용한 한국어 노트
  → KnowledgePackBuilder.render_markdown()        — 섹션 증거 앞에 1회 렌더링
  → draft_service._compose_user_prompt()          — 교차 검증 섹션이 있을 때만 작성 지침 주입

설계 원칙: 노트 생성은 수치·키워드 파싱 없이 충돌 claim 원문을 그대로 전달하고,
해석(어떤 수치가 충돌하는지, 어떻게 병기할지)은 작성 LLM 의 몫으로 남긴다.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.prompts import (
    DRAFT_CROSSCHECK_GUIDELINE,
    DRAFT_CROSSCHECK_NOTES_TITLE,
)
from services.knowledge import KnowledgePackBuilder, RetrievalService


class FakeLLM:
    n_ctx = 50_000
    model = "fake-local-model"

    def __init__(self) -> None:
        self.ask_calls: list[dict] = []

    def embed(self, _text):
        return [0.0, 1.0]

    def embed_batch(self, texts):
        return [[0.0, float(index + 1)] for index, _ in enumerate(texts)]

    def ask(self, system_prompt, user_prompt, **kwargs):
        self.ask_calls.append(
            {"system_prompt": system_prompt, "user_prompt": user_prompt, **kwargs}
        )
        return "# 생성된 초안\n\n본문입니다."


class FakeVectorStore:
    def __init__(self) -> None:
        self.query_results: list[dict] = []

    def query(self, **_kwargs):
        return list(self.query_results)

    def get_all(self, where=None):
        return []


EXTERNAL_CLAIM = {
    "claim_id": "external:000:claim_001",
    "source_id": "000",
    "source_scope": "external",
    "text": "DS 부문 매출 44조원, 영업이익 16.4조원으로 전체 이익의 82% 차지.",
    "claim_type": "numeric",
    "evidence_span": "DS 부문 매출 44조원, 영업이익 16.4조원으로 전체 이익의 82% 차지.",
    "metadata": {
        "title": "삼성전자, 2025년 4분기 실적 발표 – Samsung Newsroom Korea",
        "domain": "news.samsung.com",
        "url": "https://news.samsung.com/kr/실적-발표",
    },
}

LOCAL_CLAIM = {
    "claim_id": "local:local_abc:claim_004",
    "source_id": "local_abc",
    "source_scope": "local",
    "text": "다만 내부 관리회계 기준 4분기 영업이익은 15.8조원으로 집계되었다.",
    "claim_type": "numeric",
    "evidence_span": "다만 내부 관리회계 기준 4분기 영업이익은 15.8조원으로 집계되었다.",
    "metadata": {
        "title": "내부결산보고.docx",
        "display_path": "내부결산보고.docx",
        "privacy_label": "local_private",
    },
}

FLAG = {
    "relation": "numeric_mismatch",
    "severity": "high",
    "claimA": EXTERNAL_CLAIM["claim_id"],
    "claimB": LOCAL_CLAIM["claim_id"],
    "message": (
        "External and local claims cite different values for the same metric "
        "(영업, 이익): external=16.4, local=15.8."
    ),
}


def write_crosscheck(workspace_dir: Path, *, claims=None, flags=None) -> None:
    verification = workspace_dir / "verification"
    verification.mkdir(parents=True, exist_ok=True)
    payload = {
        "claims": [EXTERNAL_CLAIM, LOCAL_CLAIM] if claims is None else claims,
        "relations": [],
        "flags": [FLAG] if flags is None else flags,
    }
    (verification / "crosscheck.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def make_builder(workspace_root: Path) -> KnowledgePackBuilder:
    retrieval = RetrievalService(llm=FakeLLM(), vector_store=FakeVectorStore())
    return KnowledgePackBuilder(retrieval_service=retrieval, workspace_root=workspace_root)


class ConflictNoteRenderingTests(unittest.TestCase):
    """KnowledgePackBuilder 가 flags 를 한국어 claim 병기 노트로 변환하는지."""

    def test_notes_quote_both_claims_with_source_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_crosscheck(root)
            pack = make_builder(root).build_for_outline("ws1", ["현황"])

        context = pack.global_context
        self.assertIn(DRAFT_CROSSCHECK_NOTES_TITLE, context)
        # 양쪽 claim 원문이 그대로 인용된다 — 수치 파싱·요약 없이.
        self.assertIn(EXTERNAL_CLAIM["text"], context)
        self.assertIn(LOCAL_CLAIM["text"], context)
        # 출처 라벨: 내부 파일명 / 외부 문서 제목.
        self.assertIn("내부결산보고.docx", context)
        self.assertIn("Samsung Newsroom", context)
        # 영어 파이프라인 메시지는 더 이상 노출되지 않는다.
        self.assertNotIn("External and local claims", context)

    def test_crosscheck_block_renders_once_for_multi_section_outline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_crosscheck(root)
            pack = make_builder(root).build_for_outline(
                "ws1", ["분석 개요", "현황", "문제점", "원인 분석", "개선 방향"]
            )

        # 충돌 노트는 워크스페이스 전역 정보 — 섹션마다 반복되지 않는다.
        self.assertEqual(pack.global_context.count(DRAFT_CROSSCHECK_NOTES_TITLE), 1)
        self.assertEqual(pack.global_context.count(EXTERNAL_CLAIM["text"]), 1)

    def test_unresolvable_claims_fall_back_to_pipeline_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_crosscheck(root, claims=[], flags=[FLAG])
            pack = make_builder(root).build_for_outline("ws1", ["현황"])

        # 구버전 산출물(claims 미보존)은 원래 메시지로 폴백한다.
        self.assertIn(FLAG["message"], pack.global_context)

    def test_missing_crosscheck_file_keeps_pack_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pack = make_builder(Path(tmp)).build_for_outline("ws1", ["현황"])

        self.assertEqual(pack.global_context, "")


class FakeRagService:
    def __init__(self, vector_store) -> None:
        self.vector_store = vector_store


class FakeRuntime:
    def __init__(self, llm, rag_service=None) -> None:
        self.llm = llm
        self.workspace_id = "ws1"
        self.rag_service = rag_service


class DraftPromptCrosscheckTests(unittest.TestCase):
    """교차 검증 노트·작성 지침이 초안 프롬프트까지 전달되는지."""

    def _generate(self, tmp: str, llm: FakeLLM, *, with_crosscheck: bool):
        from api.services import draft_service

        workspace_dir = Path(tmp) / "ws1"
        (workspace_dir / "summary").mkdir(parents=True)
        (workspace_dir / "summary" / "batch_001.md").write_text(
            "=== batch ===\n근거 내용입니다.", encoding="utf-8"
        )
        # 로컬 코퍼스가 등록되어 있음을 표시 (지식 팩 경로 활성화).
        knowledge_dir = workspace_dir / "knowledge"
        knowledge_dir.mkdir(parents=True)
        (knowledge_dir / "sources.json").write_text("[]", encoding="utf-8")
        if with_crosscheck:
            write_crosscheck(workspace_dir)

        payload = {
            "source": "custom",
            "outline": ["분석 개요", "현황"],
            "tone": "중립",
            "length": "보통",
        }
        runtime = FakeRuntime(llm, rag_service=FakeRagService(FakeVectorStore()))
        with mock.patch.object(
            draft_service, "get_runtime", return_value=runtime
        ), mock.patch.dict("os.environ", {"VERITAS_OUTPUT_DIR": tmp}), mock.patch.object(
            draft_service.activity, "log_activity", lambda *args, **kwargs: None
        ):
            return draft_service.generate_builtin_draft("ws1", payload)

    def test_crosscheck_notes_and_guideline_reach_the_draft_prompt(self) -> None:
        llm = FakeLLM()
        with tempfile.TemporaryDirectory() as tmp:
            self._generate(tmp, llm, with_crosscheck=True)

        user_prompt = llm.ask_calls[0]["user_prompt"]
        # 충돌 claim 원문(외부·내부 수치)이 프롬프트에 함께 들어간다.
        self.assertIn(DRAFT_CROSSCHECK_NOTES_TITLE, user_prompt)
        self.assertIn("16.4조원", user_prompt)
        self.assertIn("15.8조원", user_prompt)
        # 작성 지침에 병기 지시가 추가된다.
        self.assertIn(DRAFT_CROSSCHECK_GUIDELINE, user_prompt)

    def test_without_crosscheck_prompt_carries_no_guideline(self) -> None:
        llm = FakeLLM()
        with tempfile.TemporaryDirectory() as tmp:
            self._generate(tmp, llm, with_crosscheck=False)

        user_prompt = llm.ask_calls[0]["user_prompt"]
        # 교차 검증 자료가 없으면 관련 지침도 주입되지 않는다 (환각 방지).
        self.assertNotIn(DRAFT_CROSSCHECK_NOTES_TITLE, user_prompt)
        self.assertNotIn(DRAFT_CROSSCHECK_GUIDELINE, user_prompt)


if __name__ == "__main__":
    unittest.main()
