"""Deterministic synthetic-data generator for docs/deprecated/scenarios.md Scenario B.

Scenario B = "대체육(식물성 단백질) 시장 + 가상 식품기업 (주)그린테이블". It exercises
the local-corpus → cross-check path: internal documents reuse the SAME market
terms (IMARC / Polaris / FMI / Arizton / GFI / KREI / aT, plant-based meat) as
the web survey so the token-overlap comparison fires, and deliberately restate
some web figures with a small offset so the numeric-mismatch detector flags them.

The web figures here are the real ones reported by the Scenario B AutoSurvey
(`runs/국내/final.md`); see ``WEB_CLAIMS``.

Crucial design constraint (read from ``services/verification/crosscheck/pipeline.py``):
a numeric mismatch is only flagged when the two values share a metric label, are
the same kind (both % or both absolute), AND are within ``_CONFLICT_MAX_RATIO``
(1.2 → ≤20%). So the **detectable** mismatches are 10-18% off; one **severe**
(>20%) case is included to document that the pipeline deliberately does NOT flag
it (it reads as a different metric, not a mis-stated one).

Outputs (everything regenerated identically — no randomness):

    test_data/scenario_b/
      ├─ 시장분석_내부보고서.docx     # crosscheck claims (supports + mismatches)
      ├─ 소비자패널_조사결과.pdf      # crosscheck claims (supports + mismatches)
      ├─ 월별_제품매출.csv            # 300 rows — table_query
      └─ 신제품_매출실적.xlsx         # 분기실적 + 월별상세(300행) — table_query
    test_data/scenario_b_expected/
      └─ expected.json               # web claims, intended mismatches, table check sums

Run directly to (re)populate ``test_data/`` for manual app testing; the
automated test (`tests/test_scenario_b_crosscheck.py`) imports :func:`generate`
and regenerates into a temp dir.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


# --------------------------------------------------------------------------
# Web (external) claims — frozen from the real Scenario B survey (runs/국내).
# Each becomes an external ParsedDocRecord's key_points in the test.
# --------------------------------------------------------------------------
WEB_CLAIMS: list[dict] = [
    {
        "doc_id": "006",
        "title": "IMARC 시장조사 보고서(요약)",
        "domain": "imarcgroup.com",
        "key_points": [
            "IMARC 보고서 기준 글로벌 plant-based meat 시장 규모는 2025년 약 USD 20.4B 수준으로 추정된다.",
        ],
    },
    {
        "doc_id": "005",
        "title": "Polaris Market Research(요약)",
        "domain": "polarismarketresearch.com",
        "key_points": [
            "Polaris 분석 기준 글로벌 plant-based meat 시장 규모는 2025년 약 USD 10.77B이다.",
            "Polaris는 글로벌 plant-based meat 시장의 연평균 성장률(CAGR)을 약 19.8%로 제시한다.",
        ],
    },
    {
        "doc_id": "004",
        "title": "Future Market Insights(FMI) 요약",
        "domain": "futuremarketinsights.com",
        "key_points": [
            "Future Market Insights(FMI) 기준 글로벌 plant-based meat 시장 규모는 2025년 약 USD 5.36B로 보수적으로 전망된다.",
            "FMI는 글로벌 plant-based meat 시장의 연평균 성장률(CAGR)을 약 4.1%로 전망한다.",
        ],
    },
    {
        "doc_id": "007",
        "title": "Arizton 리포트(요약)",
        "domain": "arizton.com",
        "key_points": [
            "Arizton 리포트 기준 글로벌 plant-based meat 시장의 연평균 성장률(CAGR)은 약 14.7%로 제시된다.",
        ],
    },
    {
        "doc_id": "003",
        "title": "GFI / SPINS 기반 미국 리테일 분석(요약)",
        "domain": "gfi.org",
        "key_points": [
            "GFI/SPINS 기준 2025년 미국 리테일 plant-based meat 카테고리 매출은 약 USD 1.0B로 관찰된다.",
        ],
    },
    {
        "doc_id": "010",
        "title": "농촌경제연구원(KREI) 추정(요약)",
        "domain": "krei.re.kr",
        "key_points": [
            "농촌경제연구원(KREI) 추정 기준 국내 대체육 시장 규모는 2026년 약 2,800억원으로 제시된다.",
        ],
    },
    {
        "doc_id": "011",
        "title": "aT(한국농수산식품유통공사) 인용 보도",
        "domain": "at.or.kr",
        "key_points": [
            "aT(한국농수산식품유통공사) 인용 보도 기준 국내 대체육 시장은 2025년 약 USD 22.6M 수준으로 전망된다.",
        ],
    },
]


# --------------------------------------------------------------------------
# Local (private) documents — same terms as the web, some figures offset.
# --------------------------------------------------------------------------
DOCX_NAME = "시장분석_내부보고서.docx"
PDF_NAME = "소비자패널_조사결과.pdf"
CSV_NAME = "월별_제품매출.csv"
XLSX_NAME = "신제품_매출실적.xlsx"

_DOCX_PARAGRAPHS: list[str] = [
    "(주)그린테이블 대체육 시장 분석 내부보고서 (대외비)",
    "1. 글로벌 시장 개요",
    # supports — exact web value 20.4
    "IMARC 보고서 기준 글로벌 plant-based meat 시장 규모는 2025년 약 USD 20.4B 수준으로, 당사 분석도 이와 동일하게 평가한다.",
    # mismatch — web 10.77 vs 12.5 (≈16%, flagged)
    "Polaris 분석 기준 글로벌 plant-based meat 시장 규모는 2025년 약 USD 12.5B로 당사는 추정한다.",
    # severe — web 5.36 vs 2.6 (>20%, NOT flagged by the ratio gate, by design)
    "Future Market Insights(FMI) 기준 글로벌 plant-based meat 시장 규모는 2025년 약 USD 2.6B로 당사 보수 시나리오는 본다.",
    "2. 국내 시장 및 당사 현황",
    # mismatch — web 2,800 vs 2,400 (≈14%, flagged)
    "농촌경제연구원(KREI) 추정 기준 국내 대체육 시장 규모는 2026년 약 2,400억원으로 당사는 본다.",
    # internal only (no web counterpart) — RAG-internal evidence
    "(주)그린테이블의 2025년 대체육 제품군 매출은 약 142억원으로 전년 대비 38% 성장하였다.",
    "프로젝트 베지프라임 신제품 라인은 2026년 2분기 정식 출시를 목표로 한다.",
]

_PDF_PARAGRAPHS: list[str] = [
    "(주)그린테이블 소비자 패널 조사 결과 요약 (대외비)",
    # supports — exact web CAGR 4.1%
    "Future Market Insights(FMI) 전망과 동일하게 글로벌 plant-based meat 시장의 연평균 성장률(CAGR)은 약 4.1%로 당사도 본다.",
    # mismatch — web 14.7% vs 16.0% (≈9%, flagged)
    "Arizton 리포트 기준 글로벌 plant-based meat 시장의 연평균 성장률(CAGR)을 당사는 약 16.0%로 추정한다.",
    # mismatch — web 1.0 vs 1.18 (≈18%, flagged)
    "GFI/SPINS 기준 2025년 미국 리테일 plant-based meat 카테고리 매출은 약 USD 1.18B로 당사는 추정한다.",
    # domestic support figure in KRW — deliberately NOT in USD so its metric
    # label ({시장}) cannot collide with the global "USD …B" figures. (An earlier
    # USD framing false-conflicted with IMARC's global USD 20.4B because the
    # pipeline is unit-blind: 국내 22.6M vs 글로벌 20.4B share the {usd} label and
    # sit within the 1.2× ratio gate. 2025 here also year-separates it from the
    # 2026 KREI figures.)
    "aT(한국농수산식품유통공사) 인용 보도 기준 국내 대체육 시장은 2025년 약 300억원 수준으로 전망된다.",
    # internal only
    "당사 소비자 패널 1,200명 조사에서 대체육 제품 재구매 의향은 약 47%로 나타났다.",
    "대체육 제품 맛 만족도는 5점 척도 기준 평균 3.8점으로 집계되었다.",
]

EXPECTED_MISMATCHES: list[dict] = [
    {"metric": "Polaris 글로벌 시장 규모(USD B)", "file": DOCX_NAME, "external": "10.77", "local": "12.5"},
    {"metric": "KREI 국내 시장 규모(억원)", "file": DOCX_NAME, "external": "2800", "local": "2400"},
    {"metric": "Arizton CAGR(%)", "file": PDF_NAME, "external": "14.7%", "local": "16.0%"},
    {"metric": "GFI 미국 리테일 meat(USD B)", "file": PDF_NAME, "external": "1.0", "local": "1.18"},
]
EXPECTED_SEVERE_NOT_FLAGGED: list[dict] = [
    {
        "metric": "FMI 글로벌 시장 규모(USD B)",
        "file": DOCX_NAME,
        "external": "5.36",
        "local": "2.6",
        "reason": "ratio>1.2 (>20%): the pipeline treats a far-apart value as a different metric, not a mis-stated one.",
    },
]
EXPECTED_SUPPORTS: list[str] = ["20.4", "4.1%"]


# --------------------------------------------------------------------------
# Table data (CSV / XLSX) — deterministic; the test re-checks these sums.
# --------------------------------------------------------------------------
_CSV_MONTHS = [f"{y}-{m:02d}" for y in (2024, 2025) for m in range(1, 13)][:20]  # 20 months
_CSV_PRODUCTS = ["식물성버거패티", "식물성너겟", "콩불고기", "비건만두", "대체참치"]
_CSV_CHANNELS = ["대형마트", "온라인", "편의점"]


def _csv_rows() -> list[dict]:
    """300 rows = 20 months × 5 products × 3 channels, deterministic 매출."""
    rows: list[dict] = []
    for m_idx, month in enumerate(_CSV_MONTHS):
        for p_idx, product in enumerate(_CSV_PRODUCTS):
            for c_idx, channel in enumerate(_CSV_CHANNELS):
                revenue = 3_000_000 + (p_idx * 700_000) + (c_idx * 400_000) + (m_idx * 50_000)
                qty = 120 + p_idx * 20 + c_idx * 10 + m_idx
                rows.append(
                    {
                        "월": month,
                        "제품": product,
                        "채널": channel,
                        "매출(원)": f"{revenue:,}",  # comma-formatted, e.g. "3,400,000"
                        "판매수량": qty,
                        "_revenue": revenue,  # internal, dropped before writing
                    }
                )
    return rows


_XLSX_QUARTERS = [f"{y} {q}" for y in (2024, 2025) for q in ("Q1", "Q2", "Q3", "Q4")]
_XLSX_REGIONS = ["수도권", "영남권", "호남권", "충청권", "강원권"]
_XLSX_LINES = ["HMR", "냉동", "소스"]
_XLSX_MONTHS = [f"{y}-{m:02d}" for y in (2024, 2025) for m in range(1, 13)][:20]


def _xlsx_quarter_rows() -> list[list]:
    rows = [["분기", "매출(백만원)", "영업이익(백만원)", "영업이익률(%)"]]
    for i, q in enumerate(_XLSX_QUARTERS):
        revenue = 8_200 + i * 640
        profit = round(revenue * (0.07 + i * 0.004))
        margin = round(profit / revenue * 100, 1)
        rows.append([q, revenue, profit, margin])
    return rows


def _xlsx_detail_rows() -> list[dict]:
    rows: list[dict] = []
    for m_idx, month in enumerate(_XLSX_MONTHS):
        for r_idx, region in enumerate(_XLSX_REGIONS):
            for l_idx, line in enumerate(_XLSX_LINES):
                revenue = 40 + r_idx * 9 + l_idx * 5 + m_idx  # 억원
                export = 8 + r_idx * 3 + l_idx * 2  # 수출비중 %
                rows.append(
                    {
                        "월": month,
                        "지역": region,
                        "제품군": line,
                        "매출(억원)": f"{revenue:,}",
                        "수출비중(%)": export,
                        "_revenue": revenue,
                    }
                )
    return rows


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
def _write_docx(path: Path, paragraphs: list[str]) -> None:
    import docx

    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    document.save(str(path))


def _write_pdf(path: Path, paragraphs: list[str]) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    font_name = "Helvetica"
    for candidate in ("C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/gulim.ttc"):
        if Path(candidate).exists():
            try:
                pdfmetrics.registerFont(TTFont("SceneKR", candidate))
                font_name = "SceneKR"
                break
            except Exception:
                continue
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont(font_name, 11)
    y = 800
    for text in paragraphs:
        c.drawString(40, y, text)
        y -= 26
        if y < 60:
            c.showPage()
            c.setFont(font_name, 11)
            y = 800
    c.save()


def _write_csv(path: Path, rows: list[dict]) -> None:
    fields = ["월", "제품", "채널", "매출(원)", "판매수량"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in fields})


def _write_xlsx(path: Path, quarter_rows: list[list], detail_rows: list[dict]) -> None:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet1 = workbook.active
    sheet1.title = "분기실적"
    for row in quarter_rows:
        sheet1.append(row)
    sheet2 = workbook.create_sheet("월별상세")
    sheet2.append(["월", "지역", "제품군", "매출(억원)", "수출비중(%)"])
    for row in detail_rows:
        sheet2.append([row["월"], row["지역"], row["제품군"], row["매출(억원)"], row["수출비중(%)"]])
    workbook.save(str(path))


def _table_checks(csv_rows: list[dict], detail_rows: list[dict]) -> dict:
    csv_total = sum(r["_revenue"] for r in csv_rows)
    csv_by_channel = {}
    csv_by_product = {}
    for r in csv_rows:
        csv_by_channel[r["채널"]] = csv_by_channel.get(r["채널"], 0) + r["_revenue"]
        csv_by_product[r["제품"]] = csv_by_product.get(r["제품"], 0) + r["_revenue"]
    detail_total = sum(r["_revenue"] for r in detail_rows)
    return {
        CSV_NAME: {
            "rows": len(csv_rows),
            "total_매출(원)": csv_total,
            "by_채널": csv_by_channel,
            "by_제품": csv_by_product,
        },
        XLSX_NAME: {
            "월별상세_rows": len(detail_rows),
            "월별상세_total_매출(억원)": detail_total,
        },
    }


def generate(data_dir: Path, expected_dir: Path) -> dict:
    """Create all Scenario B files under *data_dir* + ``expected.json`` under
    *expected_dir*. Returns the expected dict."""
    data_dir.mkdir(parents=True, exist_ok=True)
    expected_dir.mkdir(parents=True, exist_ok=True)

    _write_docx(data_dir / DOCX_NAME, _DOCX_PARAGRAPHS)
    _write_pdf(data_dir / PDF_NAME, _PDF_PARAGRAPHS)
    csv_rows = _csv_rows()
    _write_csv(data_dir / CSV_NAME, csv_rows)
    detail_rows = _xlsx_detail_rows()
    _write_xlsx(data_dir / XLSX_NAME, _xlsx_quarter_rows(), detail_rows)

    expected = {
        "scenario": "B — 대체육 시장 + (주)그린테이블 (synthetic)",
        "web_claims": WEB_CLAIMS,
        "local_text_docs": {
            DOCX_NAME: "\n\n".join(_DOCX_PARAGRAPHS),
            PDF_NAME: "\n\n".join(_PDF_PARAGRAPHS),
        },
        "expected_mismatches": EXPECTED_MISMATCHES,
        "expected_severe_not_flagged": EXPECTED_SEVERE_NOT_FLAGGED,
        "expected_supports": EXPECTED_SUPPORTS,
        "table_checks": _table_checks(csv_rows, detail_rows),
        "notes": (
            "Web figures are the real ones from runs/국내 (Scenario B AutoSurvey). "
            "Detectable numeric mismatches are ≤20% off (the crosscheck ratio gate); "
            "the severe case (>20%) is intentionally NOT flagged by design."
        ),
    }
    (expected_dir / "expected.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return expected


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> None:
    root = _repo_root()
    expected = generate(
        root / "test_data" / "scenario_b",
        root / "test_data" / "scenario_b_expected",
    )
    print(f"[scenario_b] wrote files to {root / 'test_data' / 'scenario_b'}")
    print(f"[scenario_b] detectable mismatches: {len(expected['expected_mismatches'])}")
    print(f"[scenario_b] table checks: {list(expected['table_checks'])}")


if __name__ == "__main__":
    main()
