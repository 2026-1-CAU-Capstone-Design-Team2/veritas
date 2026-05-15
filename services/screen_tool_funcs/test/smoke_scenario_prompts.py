"""스크린 개입 5개 시나리오 LLM 응답 smoke test.

프로젝트 루트(veritas/)에서 실행:
  python services/screen_tool_funcs/test/smoke_scenario_prompts.py

llama-server 가 LLM_HOST:LLM_PORT 에서 떠 있어야 함.
결과는 services/screen_tool_funcs/test/outputs/smoke_<timestamp>.md 에 저장됨.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# 프로젝트 루트를 sys.path에 추가하여 core/llm 모듈 import 가능하게 함
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.prompts import (  # noqa: E402
    SCREEN_INTERVENTION_SYSTEM_PROMPT,
    SCREEN_INTERVENTION_USER_PROMPT_TEMPLATE,
    SCREEN_SCENARIO_GUIDANCE,
    SCREEN_SCENARIO_GUIDANCE_DEFAULT,
)
from llm.llama_server_llm import LLMClient  # noqa: E402

# 런타임 설정 — 필요 시 수정
LLM_HOST = "127.0.0.1"
LLM_PORT = 8080
RUNS_PER_SCENARIO = 4
TIMEOUT_SEC = 180

# 공통 본문 — 사용자 제공 (기아 분석 보고서, 마지막 문장 미완)
DOCUMENT_TEXT = """전 세계의 기아 발생원인과 그 해결책에 대한 분석 보고서 작성
기아는 단순히 음식 섭취가 일시적으로 부족한 상태를 넘어, 생존과 건강 유지를 위해 필요한 영양분을 장기간 공급받지 못하는 '만성적 영양부족' 상태를 의미한다. 유엔식량농업기구는 기아를 개인의 면역 체계가 붕괴되고 성장 및 인지 기능이 저하되어, 한 세대 전체의 역량이 손실되는 심각한 사회적 위기로 규정하고 있다.

2025년 발표된 '세계 식량안보 및 영양 현황(SOFI 2025)'에 따르면, 전 세계 약 6억 7,300만 명이 여전히 기아 상태에 놓여 있다고 추정한다. 특히 아프리카의 기아 비율은 20%를 상회하며 서아시아 역시 12.7%로 높은 수치를 기록하고 있다. 이는 기아가 지구 전체에 균등하게 발생하는 문제가 아니라, 특정 지역에 압도적으로 집중되어 나타나는 구조적 문제임을 시사한다. 따라서 본 보고서는 기아를 단순한 자연재해나 생산량 부족의 결과가 아닌, 역사적 경험과 정치적 불평등에 기인한 인재(人災)로 파악하고 그 원인과 해법을 고찰하고자 한다.

현대 사회는 질소 비료의 발명과 농업 기술의 비약적 발전으로 인류 전체를 먹여 살리고도 남을 식량을 생산하고 있다. 그럼에도 기아가 특정 지역에 집중되는 이유는 식량의 절대량이 부족해서가 아니라, 식량을 적절히 분배하지 못하는 '구조적 탄력성'의 결여에 있다고 생각한다.

현재의 기아 문제는 수백 년간 지속된 유럽의 역사적 착취가 누적된 결과이다. 19세기 말 베를린 회의 이후 아프리카를 분할 점령한 열강은 현지의 자립적 식량 체계를 파괴하고, 코코아나 면화 같은 '현금작물' 중심의 단작 수출 경제로 재편했다. 이러한 구조는 독립 이후에도 고착화되어, 해당 국가들이 국제 농산물 가격 변동에 취약해지고 자국민을 위한 식량 자급 능력을 상실하게 만드는 결정적 원인이 되었다.

또한 유럽 열강이 민족과 문화적 경계를 무시하고 그은 인위적인 국경선은 오늘날 내전과 정치적 불안정의 씨앗이 되었다. '인위적 국가(Artificial States)' 연구들이 지적하듯, 역사적 맥락이 결여된 국경은 집단 간의 폭력적 충돌과 정부 차원의 차별을 야기했다. 르완다의 비극적인 인종 학살이나 수단의 장기 내전은 식민 지배가 심어놓은 갈등 구조가 폭발한 대표적 사례이다. 결국 기아 지도와 과거 식민지 지도, 그리고 현재의 내전 지도가 일치하는 현상은 기아가 '먹을 수 없는 처지'로 내몰린 역사의 산물임을 증명한다.

이에 기아 문제를 근본적으로 해결하기 위해서는 일시적인 식량 원조를 넘어선 세 가지 방향의 구조적 개혁이 필요하다고 생각한다.

첫째, 불공정한 국제 무역 구조를 개혁해야 할 것이다. 선진국의 막대한 농업 보조금은 개발도상국 농산물의 가격 경쟁력을 무력화하여 현지 농민들의 자립 기반을 무너뜨린다. 따라서 WTO 규범 개선을 통해 개도국 농민들이 공정한 환경에서 경쟁하고 제값을 받을 수 있는 무역 환경을 조성해야 한다.

둘째, 식량 주권을 보장해야 한다. 이는 단순히 식량을 공급받는 단계를 넘어, 각국이 자국의 식량 체계를 스스로 설계하고 운영할 역량을 갖추는 것을 의미한다. 이를 위해 단작 구조에서 벗어나 현지 생태계에 적합한 다양한 식량작물을 재배하는 '생태 농업'으로의 전환을 지원하고, 소농들의 토지 점유권을 법적으로 보호하는 제도가 마련되어야 한다.

셋째, 역사적 책임의 인정과 구조적 보상이 병행되어야 한다. 현재의 빈곤이 식민 지배의 유산임을 인정하고, 부채 탕감이나 기술 전수 등을 단순한 '시혜'가 아닌 '역사적 채무 이행'의 차원에서 접근해야 한다. 선진국이 보유한 농업 R&D 기술과 기후변화 대응 기술을 개도국에 실질적으로 이전하는 기술 보상이 적극적으로 논의되어야 한다.

다만 앞서 제시한 세 가지 해법은 한 가지 전제 조건이 충족될 때 실질적인 효과를 가져올 수 있다. 바로 사회적 화해와 정치적 안정이다. 전쟁과 내전과 같은 정치적 불안정 속에서는 어떠한 인프라도 유지될 수 없기 때문이다. 따라서 분쟁 지역에서는 중앙정부뿐만 아니라 지역 공동체가 참여하는 상향식 평화 구축 모델을 강화해야 한다.

그에 대한 예시로 소말리 랜드의 사례를 들고 싶다. 소말리 랜드는, 소말리아 서쪽에 위치한 자치 치구로서, 아직 국가로 인정받은 곳은 아니지만, 오랜 소말리아 내전 속에서 독립을 선언하고 평화롭게 질서를 구축한 사례로 잘 알려져 있다. 이런 사례에서 알 수 있듯, 아무리"""

# 시나리오별 routing_hint — scenarios.py 정의와 일치시킴
SCENARIO_HINTS: dict[str, dict[str, str]] = {
    "idle_after_writing": {
        "tone": "gentle_continuation",
        "preferred_action": "continue_writing",
        "focus_scope": "recent_writing",
    },
    "whole_document_review": {
        "tone": "comprehensive_review",
        "preferred_action": "review_whole_document",
        "focus_scope": "full_document",
    },
    "long_static_review": {
        "tone": "proofreading_review",
        "preferred_action": "review_whole_document",
        "focus_scope": "full_document",
    },
    "paragraph_churn": {
        "tone": "unstick",
        "preferred_action": "revise_current_paragraph",
        "focus_scope": "recent_writing",
    },
    "blank_document_start": {
        "tone": "kickoff",
        "preferred_action": "continue_writing",
        "focus_scope": "full_document",
    },
}

# 응답 채점 시 참고용 — 설계 doc 3-4표 요지
SCENARIO_SPEC_HINT: dict[str, str] = {
    "idle_after_writing": "expected: 1-sentence continuation or short fact, gentle tone",
    "whole_document_review": "expected: 2-3 bullets on flow/structure/missing points",
    "long_static_review": "expected: specific quotes + concrete fixes (typos/awkward phrasing)",
    "paragraph_churn": "expected: 1-2 rewrites of the stuck paragraph, no new scope",
    "blank_document_start": "expected: opening sentence/outline, low-pressure tone",
}

SCENARIOS: list[str] = list(SCENARIO_HINTS.keys())


def split_paragraphs(text: str) -> list[str]:
    # 빈 줄(\n\n) 기준 문단 분리
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def last_paragraph(text: str) -> str:
    parts = split_paragraphs(text)
    return parts[-1] if parts else ""


def last_sentence(text: str) -> str:
    # 종결부호 + 공백 기준 분리; 미완 fragment도 마지막 원소로 잡힘
    if not text:
        return ""
    parts = re.split(r"[.!?]\s+", text.strip())
    parts = [p.strip() for p in parts if p.strip()]
    return parts[-1] if parts else text.strip()


def scenario_full_text(intervention_type: str) -> str:
    # blank_document_start만 첫 줄(제목)로 축소
    if intervention_type == "blank_document_start":
        return DOCUMENT_TEXT.split("\n", 1)[0]
    return DOCUMENT_TEXT


def make_writing_context(intervention_type: str) -> dict[str, Any]:
    # intervention_dispatcher._writing_context_for_type 출력 형태 모방
    full_text = scenario_full_text(intervention_type)
    current_paragraph = last_paragraph(full_text)
    focused_sentence = last_sentence(current_paragraph or full_text)
    hint = SCENARIO_HINTS[intervention_type]
    return {
        "full_text": full_text,
        "full_text_chars": len(full_text),
        "current_paragraph": current_paragraph,
        "recent_sentences": focused_sentence,
        "focused_sentence": focused_sentence,
        "paragraph_source": "smoke_test",
        "paragraph_rect": None,
        "changed_text": "",
        "confidence": 0.95,
        "focus_scope": hint["focus_scope"],
    }


def make_routing_hint(intervention_type: str) -> dict[str, Any]:
    hint = SCENARIO_HINTS[intervention_type]
    return {
        "tone": hint["tone"],
        "preferred_action": hint["preferred_action"],
        "intervention_type": intervention_type,
        "focus_scope": hint["focus_scope"],
    }


def pretty(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_prompt(intervention_type: str) -> str:
    # chat_agent.answer_screen_intervention 의 .format() 인자 구성을 그대로 모방
    writing_context = make_writing_context(intervention_type)
    routing_hint = make_routing_hint(intervention_type)
    guidance = SCREEN_SCENARIO_GUIDANCE.get(
        intervention_type, SCREEN_SCENARIO_GUIDANCE_DEFAULT
    )
    app_context = {
        "process": "notepad.exe",
        "title": f"smoke_{intervention_type}.txt",
        "pid": 0,
        "hwnd": 0,
        "app_type": "text_editor",
        "document_key": f"smoke::{intervention_type}",
    }
    return SCREEN_INTERVENTION_USER_PROMPT_TEMPLATE.format(
        history="(no chat history — smoke test)",
        app_context=pretty(app_context),
        writing_context=pretty(writing_context),
        routing_hint=pretty(routing_hint),
        scenario_guidance=guidance,
        knowledge_context="(no knowledge base context — smoke test)",
    )


def run_once(llm: LLMClient, intervention_type: str, run_idx: int) -> dict[str, Any]:
    # 1회 호출 + 시간/응답/에러 수집
    prompt = build_prompt(intervention_type)
    start = time.monotonic()
    error: str | None = None
    answer = ""
    try:
        answer = llm.ask(
            SCREEN_INTERVENTION_SYSTEM_PROMPT,
            prompt,
            reasoning=False,
            stream=False,
            stream_label=f"smoke_{intervention_type}",
            timeout_sec=TIMEOUT_SEC,
        )
    except Exception as e:
        error = repr(e)
    elapsed = time.monotonic() - start
    return {
        "intervention_type": intervention_type,
        "run_idx": run_idx,
        "elapsed_sec": round(elapsed, 2),
        "answer": (answer or "").strip(),
        "error": error,
    }


def dump_markdown(
    results: list[dict[str, Any]],
    out_path: Path,
    *,
    started_at: datetime,
    total_elapsed: float,
    interrupted: bool = False,
) -> None:
    # 응답 결과를 마크다운 한 파일로 정리
    lines = [
        "# Scenario prompt smoke results",
        "",
        f"- started: {started_at.isoformat()}",
        f"- total elapsed: {round(total_elapsed, 2)}s",
        f"- runs per scenario: {RUNS_PER_SCENARIO}",
        f"- LLM: {LLM_HOST}:{LLM_PORT}",
        f"- runs collected: {len(results)}",
    ]
    if interrupted:
        lines.append("- interrupted: true (partial results)")
    lines.append("")
    for res in results:
        itype = res["intervention_type"]
        lines.append(f"## {itype} #{res['run_idx']}")
        lines.append(f"- {SCENARIO_SPEC_HINT.get(itype, '')}")
        lines.append(f"- elapsed: {res['elapsed_sec']}s  |  chars: {len(res['answer'])}")
        if res["error"]:
            lines.append(f"- error: `{res['error']}`")
        lines.append("")
        lines.append("```")
        lines.append(res["answer"] or "(empty)")
        lines.append("```")
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    started_at = datetime.now()
    out_dir = Path(__file__).resolve().parent / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = started_at.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"smoke_{stamp}.md"

    print(f"[smoke] start {started_at.isoformat()}", flush=True)
    print(f"[smoke] llama-server {LLM_HOST}:{LLM_PORT}", flush=True)
    llm = LLMClient(host=LLM_HOST, port=LLM_PORT)

    results: list[dict[str, Any]] = []
    total_start = time.monotonic()
    interrupted = False
    try:
        for itype in SCENARIOS:
            for run_idx in range(1, RUNS_PER_SCENARIO + 1):
                print(f"[smoke] {itype} #{run_idx} ...", flush=True)
                res = run_once(llm, itype, run_idx)
                tag = f"err={res['error']}" if res["error"] else f"{len(res['answer'])}chars"
                print(f"[smoke]   done {res['elapsed_sec']}s {tag}", flush=True)
                results.append(res)
    except KeyboardInterrupt:
        interrupted = True
        print("[smoke] interrupted, dumping partial results", flush=True)

    total_elapsed = time.monotonic() - total_start
    dump_markdown(
        results,
        out_path,
        started_at=started_at,
        total_elapsed=total_elapsed,
        interrupted=interrupted,
    )
    print(f"[smoke] wrote {out_path}", flush=True)
    print(f"[smoke] done {round(total_elapsed, 2)}s ({len(results)} runs)", flush=True)


if __name__ == "__main__":
    main()
