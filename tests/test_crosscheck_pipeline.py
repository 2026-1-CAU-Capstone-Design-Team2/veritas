from __future__ import annotations

import unittest

from core.knowledge_models import (
    KnowledgeSourceRecord,
    PrivacyLabel,
    SourceKind,
    SourceScope,
)
from core.models import ParsedDocRecord
from services.verification.crosscheck import run_crosscheck_pipeline
from services.verification.crosscheck.pipeline import (
    _extract_measurements,
    _select_claims,
    _sentences,
)


def make_external(doc_id: str, key_points: list[str], title: str = "외부 문서") -> ParsedDocRecord:
    return ParsedDocRecord(
        doc_id=doc_id,
        title=title,
        domain="example.com",
        key_points=key_points,
    )


def make_local_source(source_id: str, title: str = "내부 문서.docx") -> KnowledgeSourceRecord:
    return KnowledgeSourceRecord(
        source_id=source_id,
        workspace_id="ws1",
        source_scope=SourceScope.LOCAL,
        source_kind=SourceKind.DOCX,
        title=title,
        canonical_uri="local",
        display_path=title,
        privacy_label=PrivacyLabel.LOCAL_PRIVATE,
        content_hash="hash",
    )


try:
    from services.verification.tokenization import HybridTokenizer

    # 모듈 로드 시 1회만 생성 — Kiwi 초기화 비용(수백 ms)을 테스트마다 내지 않는다.
    # 실제 서비스(VerificationService)가 주입하는 것과 동일한 토크나이저로
    # 한국어 형태소 경로를 검증한다.
    _KIWI_TOKENIZER = HybridTokenizer()
    HAS_KIWI = True
except Exception:
    _KIWI_TOKENIZER = None
    HAS_KIWI = False


def run_pipeline(external_points, local_text, *, tokenizer=None):
    """One external doc + one local doc helper."""
    artifact = run_crosscheck_pipeline(
        external_docs=[make_external("1", external_points)],
        local_sources=[make_local_source("local_a")],
        local_documents={"local_a": local_text},
        tokenizer=tokenizer,
    )
    return artifact


class MeasurementExtractionTests(unittest.TestCase):
    """수치 분류는 구조적 신호(자릿수·범위·표기)만 사용한다 — 단어 키워드 금지."""

    def test_bare_calendar_range_integers_are_year_identifiers(self) -> None:
        # "년" 같은 단어가 아니라 "분리자 없는 4자리 달력 범위 정수" 라는
        # 구조만으로 연도를 식별한다.
        measurements, years = _extract_measurements(
            "2025년 4분기 연결 기준 매출은 93.8조원, 영업이익은 20.1조원을 기록했다."
        )
        self.assertEqual(years, {"2025"})
        self.assertIn("93.8", measurements)
        self.assertIn("20.1", measurements)
        self.assertNotIn("2025", measurements)

    def test_separator_or_decimal_keeps_calendar_range_value_as_measurement(self) -> None:
        # "2,050억" / "2050.5" 는 달력 범위에 있어도 측정값이다 (구조가 다름).
        measurements, years = _extract_measurements("매출 2,050억원과 단가 2050.5달러")
        self.assertEqual(years, set())
        self.assertEqual(measurements, {"2050", "2050.5"})

    def test_dates_are_context(self) -> None:
        measurements, years = _extract_measurements("2025-01 매출 10,976억원")
        self.assertEqual(measurements, {"10976"})
        self.assertEqual(years, {"2025"})

    def test_percentages_and_amounts_are_measurements(self) -> None:
        measurements, _ = _extract_measurements("점유율 62%, 시장 규모 546억 달러")
        self.assertEqual(measurements, {"62%", "546"})


class CompareClaimsTests(unittest.TestCase):
    """The four false-positive/false-negative classes found in the 삼성전자 run."""

    def test_context_numbers_do_not_cause_mismatch(self) -> None:
        # 실측 FLAG 1/6/9: 핵심 수치(93.8/20.1)가 완전히 일치 — 한쪽에만 있는
        # 연도/분기 수치 때문에 mismatch가 되면 안 된다.
        artifact = run_pipeline(
            ["연결 기준 매출은 93.8조원, 영업이익은 20.1조원으로 역대 최대를 기록했다."],
            "2025년 4분기 연결 기준 매출은 93.8조원, 영업이익은 20.1조원으로 분기 기준 역대 최대 실적을 기록하였다.",
        )
        relations = [r.relation for r in artifact.relations]
        self.assertIn("supports", relations)
        self.assertEqual(artifact.flags, [])

    def test_different_period_figures_are_not_conflicts(self) -> None:
        # 실측 FLAG 8/10/11: 2분기 실적 vs 4분기 실적 — 다른 기간의 수치 차이는
        # 모순이 아니다. 비율 조건(1.2x)과 저정보 수치 규칙이 이를 걸러낸다
        # (분기라는 단어를 인식해서가 아니라, 구조적 신호만으로).
        artifact = run_pipeline(
            ["2025년 2분기 삼성전자 매출은 74.57조원, 영업이익은 4.68조원으로 집계됐다."],
            "2025년 4분기 삼성전자 매출은 93.8조원, 영업이익은 20.1조원으로 집계되었다.",
        )
        self.assertEqual(artifact.flags, [])

    def test_different_years_are_not_compared(self) -> None:
        # 연도가 명시적으로 다르면 (둘 다 연도형 수치 보유 + 교집합 없음)
        # 비교 자체가 일어나지 않는다.
        artifact = run_pipeline(
            ["2025년 HBM4 시장 규모는 600억 달러로 전망된다."],
            "2026년 HBM4 시장 규모는 546억 달러로 전망되어 성장세가 이어진다.",
        )
        self.assertEqual(artifact.relations, [])
        self.assertEqual(artifact.flags, [])

    def test_single_digit_agreement_is_not_supports_evidence(self) -> None:
        # 한 자리 정수(서수/개수)의 일치만으로는 supports 가 되지 않는다 —
        # 우연 일치 확률이 높은 저정보량 수치이기 때문.
        artifact = run_pipeline(
            ["4분기 신제품 3종을 출시할 계획이라고 발표했다."],
            "4분기 내부 로드맵 기준 신제품 출시 계획은 총 7종으로 확정되어 보고되었다.",
        )
        relations = [r.relation for r in artifact.relations]
        self.assertNotIn("supports", relations)

    def test_different_kinds_of_figures_are_not_conflicts(self) -> None:
        # 실측 FLAG 3: 비중(82%) vs 절대 금액(93.8조) — 서로 다른 종류의 수치는
        # 충돌 후보가 아니므로 partially_supports.
        artifact = run_pipeline(
            ["삼성전자 4분기 실적에서 DS부문이 82%의 이익을 차지하는 구조를 강조한다."],
            "삼성전자 4분기 실적은 매출 93.8조원, 영업이익은 20.1조원으로 집계되었다.",
        )
        relations = [r.relation for r in artifact.relations]
        self.assertNotIn("numeric_mismatch", relations)
        self.assertEqual(artifact.flags, [])

    def test_genuine_same_scale_conflict_is_flagged(self) -> None:
        # 실측 FLAG 4 (유효한 불일치): 시장 규모 600억 vs 546억 — 같은 종류,
        # 같은 스케일의 다른 값 → 진짜 mismatch.
        artifact = run_pipeline(
            ["2026년 HBM4 시장의 규모는 600억 달러 규모로 성장할 가능성이 있다."],
            "2026년 HBM4 시장 규모는 546억 달러로 전년 대비 58% 성장이 전망된다.",
        )
        relations = [r.relation for r in artifact.relations]
        self.assertIn("numeric_mismatch", relations)
        self.assertEqual(len(artifact.flags), 1)
        self.assertIn("600", artifact.flags[0]["message"])
        self.assertIn("546", artifact.flags[0]["message"])

    def test_korean_particles_break_matching_without_tokenizer(self) -> None:
        # 시나리오 A의 의도된 불일치(15.8조 vs 16.4조)가 누락된 원인 재현:
        # 표현이 다른 두 문장은 조사 차이로 regex 토큰이 겹치지 않아 비교가
        # 일어나지 않는다 (tokenizer 미주입 시).
        external = ["DS 부문 매출 44조원, 영업이익 16.4조원으로 전체 이익의 82% 차지."]
        local = "다만 내부 관리회계 기준 4분기 영업이익은 15.8조원으로 집계되었다."

        without_tokenizer = run_pipeline(external, local)
        self.assertEqual(without_tokenizer.flags, [])  # 누락 (기존 문제)

    @unittest.skipUnless(HAS_KIWI, "kiwipiepy is required for the morpheme path")
    def test_korean_particles_handled_with_tokenizer(self) -> None:
        # 동일한 케이스에 실제 Kiwi 형태소 토크나이저(서비스가 주입하는 것과
        # 동일)를 주입하면 "영업이익은"/"영업이익"이 같은 형태소(영업+이익)로
        # 분해되어 비교가 성립하고, 16.4 vs 15.8 의 진짜 불일치가 탐지된다.
        external = ["DS 부문 매출 44조원, 영업이익 16.4조원으로 전체 이익의 82% 차지."]
        local = "다만 내부 관리회계 기준 4분기 영업이익은 15.8조원으로 집계되었다."

        with_tokenizer = run_pipeline(external, local, tokenizer=_KIWI_TOKENIZER)

        relations = [r.relation for r in with_tokenizer.relations]
        self.assertIn("numeric_mismatch", relations)
        self.assertEqual(len(with_tokenizer.flags), 1)
        message = with_tokenizer.flags[0]["message"]
        self.assertIn("16.4", message)
        self.assertIn("15.8", message)

    def test_duplicate_conflicts_are_deduped(self) -> None:
        # 같은 내부 claim 에 대해 같은 충돌 값을 반복하는 외부 문서 여러 건 →
        # flag 는 1건만 생성된다.
        artifact = run_crosscheck_pipeline(
            external_docs=[
                make_external("1", ["HBM4 시장 규모는 600억 달러로 전망된다."]),
                make_external("2", ["HBM4 시장 규모는 600억 달러 수준으로 성장한다."]),
            ],
            local_sources=[make_local_source("local_a")],
            local_documents={
                "local_a": "내부 분석 기준 2026년 HBM4 시장 규모는 546억 달러 수준으로 전망된다."
            },
        )

        mismatch_flags = [f for f in artifact.flags if f["relation"] == "numeric_mismatch"]
        self.assertEqual(len(mismatch_flags), 1)


class ClaimExtractionTests(unittest.TestCase):
    def test_markdown_table_rows_are_not_claims(self) -> None:
        sentences = _sentences(
            "본문 문장은 충분히 길어서 claim 후보가 되어야 한다고 설명한다.\n\n"
            "| 2025-01 | 메모리 | HBM | 북미 | 10,976 | 3,864 | 56.0 | 43 |\n"
            "| 2025-01 | 메모리 | HBM | 중국 | 6,036 | 2,125 | 56.0 | 23 |\n"
        )
        self.assertEqual(len(sentences), 1)
        self.assertNotIn("|", sentences[0])

    def test_single_newlines_do_not_split_sentences(self) -> None:
        # PDF 추출 텍스트는 시각적 줄바꿈으로 문장이 끊긴다 — 단일 줄바꿈은
        # 공백으로 합쳐져 하나의 claim 이 되어야 한다.
        sentences = _sentences(
            "내부 분석 기준으로는 SK 하이닉스 62%,\n자사 28%, Micron 15% 미만으로 추정된다."
        )
        self.assertEqual(len(sentences), 1)
        self.assertIn("62%", sentences[0])
        self.assertIn("15%", sentences[0])

    def test_numeric_sentences_win_claim_slots(self) -> None:
        # 수치 없는 배너/머리말보다 수치를 담은 문장이 claim 슬롯을 우선 차지한다.
        candidates = [
            "이 문서는 대외비이며 사외 반출이 금지되어 있다는 안내문이다.",
            "회사의 전반적인 시장 전략과 방향성에 대해 길게 설명하는 문장이다.",
            "4분기 영업이익은 15.8조원으로 집계되었다고 보고한다.",
            "연간 연구개발비는 36.2조원으로 내부 집계되었다고 명시한다.",
        ]
        selected = _select_claims(candidates, max_claims=2)
        self.assertEqual(len(selected), 2)
        self.assertTrue(all("조원" in text for text in selected))


class BackwardCompatibilityTests(unittest.TestCase):
    def test_english_numeric_mismatch_still_detected(self) -> None:
        # 기존 test_local_corpus_mvp 의 영어 케이스 — regex fallback 경로 회귀 확인.
        artifact = run_crosscheck_pipeline(
            external_docs=[
                make_external("1", ["Revenue target for Project Atlas is 39 units in 2026."])
            ],
            local_sources=[make_local_source("local_a", title="internal.md")],
            local_documents={
                "local_a": "Internal plan: Revenue target for Project Atlas is 42 units in 2026."
            },
        )
        self.assertTrue(any(r.relation == "numeric_mismatch" for r in artifact.relations))
        self.assertTrue(artifact.flags)


class NoHardcodedKeywordGuardTests(unittest.TestCase):
    """일반화 원칙 회귀 가드 — crosscheck 알고리즘에 언어/도메인 키워드 금지.

    claim 추출과 수치 비교는 구조적 신호(수치 형식, 달력 범위, 문장부호,
    마크다운 표기, POS 기반 형태소)만 사용해야 한다. 코드(주석·docstring 제외)에
    한국어 단어 리터럴이나 자연어 불용어 리스트가 들어오면 이 테스트가 실패한다.

    proactive 서브시스템의 NoKeywordModulesTests 와 같은 역할을 crosscheck 에
    적용한 것이다.
    """

    @staticmethod
    def _code_string_literals() -> list[str]:
        """pipeline.py 의 string 리터럴 중 docstring 이 아닌 것 전부."""
        import ast
        import inspect

        from services.verification.crosscheck import pipeline

        source = inspect.getsource(pipeline)
        tree = ast.parse(source)

        # docstring 노드(모듈/클래스/함수 본문의 첫 Expr 문자열) 식별
        docstring_ids: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstring_ids.add(id(body[0].value))

        literals: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstring_ids
            ):
                literals.append(node.value)
        return literals

    def test_no_korean_word_literals_in_code(self) -> None:
        import re as _re

        korean = _re.compile(r"[가-힣]")
        violations = [
            literal for literal in self._code_string_literals() if korean.search(literal)
        ]
        self.assertEqual(
            violations,
            [],
            "crosscheck 알고리즘 코드에 한국어 리터럴이 있으면 안 됩니다 (구조적 신호만 허용): "
            f"{violations}",
        )

    def test_no_natural_language_stopword_lists_in_code(self) -> None:
        import ast
        import inspect

        from services.verification.crosscheck import pipeline

        # 자연어 불용어들이 집합/리스트/튜플 리터럴로 묶여 있으면 어휘 사전으로 간주.
        stopword_markers = {"the", "and", "for", "with", "this", "that", "from"}
        source = inspect.getsource(pipeline)
        tree = ast.parse(source)
        violations: list[set] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
                values = {
                    element.value
                    for element in node.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                }
                overlap = values & stopword_markers
                if overlap:
                    violations.append(overlap)
        self.assertEqual(
            violations,
            [],
            f"crosscheck 알고리즘 코드에 불용어 사전이 있으면 안 됩니다: {violations}",
        )


if __name__ == "__main__":
    unittest.main()
