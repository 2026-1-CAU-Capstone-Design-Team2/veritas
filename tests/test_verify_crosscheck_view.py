from __future__ import annotations

import unittest

from api.services.verify_view import crosscheck_overview
from core.knowledge_models import (
    KnowledgeSourceRecord,
    PrivacyLabel,
    SourceKind,
    SourceScope,
)
from core.models import ParsedDocRecord
from core.verification_crosscheck_models import (
    CrossCheckArtifact,
    CrossCheckClaim,
    CrossCheckRelation,
)
from services.verification.crosscheck import run_crosscheck_pipeline
from services.verification.models import VerificationArtifacts


def make_crosscheck_artifact() -> CrossCheckArtifact:
    """Hand-built artifact with one numeric mismatch between web and local."""
    external_claim = CrossCheckClaim(
        claim_id="external:010:claim_000",
        source_id="010",
        source_scope=SourceScope.EXTERNAL,
        text="DS 부문 매출 44조원, 영업이익 16.4조원으로 견고한 수익성 달성.",
        claim_type="numeric",
        evidence_span="",
        metadata={
            "title": "삼성전자, 2025년 4분기 실적 발표",
            "domain": "news.samsungsemiconductor.com",
            "url": "https://news.samsungsemiconductor.com/kr/...",
        },
    )
    local_claim = CrossCheckClaim(
        claim_id="local:local_abc:claim_000",
        source_id="local_abc",
        source_scope=SourceScope.LOCAL,
        text="내부 관리회계 기준 4분기 영업이익은 15.8조원으로 집계되었다.",
        claim_type="numeric",
        evidence_span="",
        metadata={
            "title": "DS부문_2025년_4분기_내부결산보고_대외비.docx",
            "display_path": "scenario_a/DS부문_2025년_4분기_내부결산보고_대외비.docx",
            "privacy_label": "local_private",
        },
    )
    relation = CrossCheckRelation(
        claim_a=external_claim.claim_id,
        claim_b=local_claim.claim_id,
        relation="numeric_mismatch",
        severity="high",
        reason="External and local claims cite different numbers.",
    )
    flag = {
        "relation": "numeric_mismatch",
        "severity": "high",
        "claimA": external_claim.claim_id,
        "claimB": local_claim.claim_id,
        "message": relation.reason,
    }
    return CrossCheckArtifact(
        claims=[external_claim, local_claim],
        relations=[relation],
        flags=[flag],
    )


class CrosscheckOverviewTests(unittest.TestCase):
    def test_missing_artifact_reports_unavailable(self) -> None:
        artifacts = VerificationArtifacts(crosscheck=None)

        payload = crosscheck_overview(artifacts)

        self.assertFalse(payload["available"])
        self.assertEqual(payload["flags"], [])
        self.assertEqual(payload["flagCount"], 0)

    def test_ran_without_local_docs_reports_zero_local_sources(self) -> None:
        # The crosscheck task ran but the workspace had no registered local
        # documents — the panel must be able to distinguish this from "never ran".
        artifacts = VerificationArtifacts(
            crosscheck=CrossCheckArtifact(claims=[], relations=[], flags=[])
        )

        payload = crosscheck_overview(artifacts)

        self.assertTrue(payload["available"])
        self.assertEqual(payload["localSourceCount"], 0)
        self.assertEqual(payload["flagCount"], 0)

    def test_flags_resolve_internal_and_external_sides(self) -> None:
        artifacts = VerificationArtifacts(crosscheck=make_crosscheck_artifact())

        payload = crosscheck_overview(artifacts)

        self.assertTrue(payload["available"])
        self.assertEqual(payload["localSourceCount"], 1)
        self.assertEqual(payload["externalDocCount"], 1)
        self.assertEqual(payload["flagCount"], 1)

        flag = payload["flags"][0]
        self.assertEqual(flag["relation"], "numeric_mismatch")
        # 내부 주장: 텍스트와 출처(파일 경로)가 그대로 풀어져 있어야 한다.
        self.assertIn("15.8조원", flag["local"]["text"])
        self.assertIn("DS부문_2025년_4분기", flag["local"]["label"])
        # 외부 주장: 텍스트와 출처(도메인)가 풀어져 있어야 한다.
        self.assertIn("16.4조원", flag["external"]["text"])
        self.assertEqual(flag["external"]["label"], "news.samsungsemiconductor.com")

    def test_dangling_claim_reference_degrades_gracefully(self) -> None:
        # A flag pointing at a missing claim id must not crash the view.
        artifact = CrossCheckArtifact(
            claims=[],
            relations=[],
            flags=[
                {
                    "relation": "numeric_mismatch",
                    "severity": "high",
                    "claimA": "external:missing:claim_000",
                    "claimB": "local:missing:claim_000",
                    "message": "dangling",
                }
            ],
        )
        artifacts = VerificationArtifacts(crosscheck=artifact)

        payload = crosscheck_overview(artifacts)

        self.assertEqual(payload["flagCount"], 1)
        self.assertEqual(payload["flags"][0]["local"]["text"], "")
        self.assertEqual(payload["flags"][0]["external"]["text"], "")


class CrosscheckEndToEndTests(unittest.TestCase):
    def test_pipeline_output_renders_through_view(self) -> None:
        """run_crosscheck_pipeline → crosscheck_overview 전체 경로 검증.

        시나리오 A 설계와 동일한 구조: 외부 문서는 영업이익 16.4조원을,
        내부 문서는 15.8조원을 주장 → numeric_mismatch 1건이 view 까지
        도달해야 한다.
        """
        external = ParsedDocRecord(
            doc_id="010",
            title="삼성전자 2025년 4분기 실적 발표",
            domain="news.samsungsemiconductor.com",
            key_points=["삼성전자 DS부문 4분기 영업이익 16.4조원 기록"],
        )
        local_source = KnowledgeSourceRecord(
            source_id="local_settlement",
            workspace_id="ws1",
            source_scope=SourceScope.LOCAL,
            source_kind=SourceKind.DOCX,
            title="DS부문_내부결산보고.docx",
            canonical_uri="local",
            display_path="scenario_a/DS부문_내부결산보고.docx",
            privacy_label=PrivacyLabel.LOCAL_PRIVATE,
            content_hash="hash",
        )
        local_documents = {
            "local_settlement": (
                "내부 관리회계 기준 삼성전자 DS부문 4분기 영업이익 15.8조원으로 집계되었다."
            )
        }

        crosscheck = run_crosscheck_pipeline(
            external_docs=[external],
            local_sources=[local_source],
            local_documents=local_documents,
        )
        artifacts = VerificationArtifacts(crosscheck=crosscheck)

        payload = crosscheck_overview(artifacts)

        self.assertTrue(payload["available"])
        self.assertGreaterEqual(payload["flagCount"], 1)
        mismatch = next(
            (f for f in payload["flags"] if f["relation"] == "numeric_mismatch"),
            None,
        )
        self.assertIsNotNone(mismatch)
        self.assertIn("15.8", mismatch["local"]["text"])
        self.assertIn("16.4", mismatch["external"]["text"])


if __name__ == "__main__":
    unittest.main()
