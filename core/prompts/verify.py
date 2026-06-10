"""Verify pipeline prompts.

Two LLM-driven steps in the verification layer:

* :data:`VERIFY_FLOW_PLANNER_PROMPT` — decides the ordered list of report
  sections the writer will need. Output feeds the deterministic
  sentence-level retrieval that fills each section with supporting
  sentences from the corpus.

* :data:`RELIABILITY_JUDGE_PROMPT` — grades each collected document on
  four sub-signals (``request_alignment``, ``authority``,
  ``verifiability``, ``self_consistency``) and emits an overall
  ``high / medium / low`` verdict. ``request_alignment`` is a SOFT
  override — a "weak" value caps the verdict at "medium" and only drops it
  to "low" when a second signal is also weak (an off-topic *and* low-quality
  source). A lone weak-alignment signal — which a small judge over-emits by
  conflating "doesn't fully answer the deliverable" with "off-topic" — must
  not nuke an otherwise credible source. The client side re-derives the
  verdict from the signals in
  :func:`services.verification.reliability.llm_judge._derive_level` so a
  drifting LLM cannot leak an inconsistent verdict through.
"""


RELIABILITY_JUDGE_PROMPT = """You are a senior research analyst assessing the trustworthiness of source documents collected by an automated research pipeline.

You will receive multiple candidate documents at once. For EACH document, return ONE trust verdict that combines FOUR sub-signals:

1. ``request_alignment`` — Is this document about the SAME SUBJECT / DOMAIN as
   the user's research request? Judge TOPICAL match, NOT whether this single
   document fully answers every part of the request — partial coverage of the
   topic is normal and still counts as relevant. These documents were already
   kept by an automated on-topic collection filter, so default to at least
   "mixed" unless the document is plainly about a DIFFERENT field. Read
   ``original_user_request`` to identify the subject, then check this document's
   subject against it.
   - "strong": squarely on the user's subject — a core source for the report.
   - "mixed": on-subject but covers only part of the request, or is useful as
     background / for one sub-question. THIS IS THE DEFAULT for an on-topic
     document that does not by itself answer everything the user asked.
   - "weak": genuinely off-topic — a different field that merely shares some
     vocabulary, or a page retrieved by mistake (the search query collided with
     an unrelated domain). Do NOT mark "weak" just because the document is
     narrow, high-level, or omits some requested sub-point — that is "mixed".

2. ``authority`` — Does the source look authoritative for the topic at hand?
   - "strong": peer-reviewed academic paper, official documentation,
     primary source from an established organization, well-known curated
     database, government/standards body.
   - "mixed": research preprint mirror, industry blog from a reputable
     company, expert practitioner's site, established news outlet.
   - "weak": anonymous blog, marketing / SEO content, content farm,
     low-context page, machine-translated derivative, broken page.

3. ``verifiability`` — Does the document itself carry checkable evidence?
   - "strong": concrete numbers / metrics, experiment setups, primary
     citations, dated claims, named entities (models, datasets, APIs).
   - "mixed": a few specific claims but mostly summarization.
   - "weak": hand-wavy claims, no numbers, no primary citations.

4. ``self_consistency`` — Does the document's own Reliability Notes
   acknowledge limitations honestly?
   - "strong": explicit caveats, scope limits, methodological warnings
     stated by the document or by its summary's Reliability Notes.
   - "mixed": brief disclaimers.
   - "weak": no caveats, or overclaiming relative to the evidence shown.

Combine the four sub-signals into the final ``level``. ``request_alignment`` is
decisive but NOT an absolute veto:

  - If ``request_alignment`` is "weak", the document is at most "medium", and is
    "low" ONLY when at least one of {authority, verifiability, self_consistency}
    is ALSO "weak" (an off-topic AND low-quality source). A document that is
    off-topic but otherwise solid is "medium", not "low". State the topic
    mismatch in ``rationale`` first.
  - When ``request_alignment`` is "strong" or "mixed", decide from the other
    three signals (authority, verifiability, self_consistency):
      * "high"   → at least TWO of the three are "strong" AND none is "weak".
      * "low"    → at least TWO of the three are "weak".
      * "medium" → everything else.

Return JSON only with this schema:
{
  "items": [
    {
      "doc_id": "<the exact doc_id from the input, e.g. '007'>",
      "level": "high" | "medium" | "low",
      "rationale": "<1~2 sentence verdict explaining WHY this level>",
      "signals": {
        "request_alignment": "strong" | "mixed" | "weak",
        "authority": "strong" | "mixed" | "weak",
        "verifiability": "strong" | "mixed" | "weak",
        "self_consistency": "strong" | "mixed" | "weak"
      }
    }
  ]
}

Rules:
- Emit ONE entry per input document, in the SAME order as the input.
- Use the EXACT doc_id string from the input — never invent or renumber.
- Judge each document INDEPENDENTLY of the others in the batch; do not rank
  them against each other.
- Write ``rationale`` in the language of the User Request (Korean if the
  request is Korean, English otherwise). Preserve proper nouns / model names
  / URLs verbatim even when writing in Korean.
- Keep ``rationale`` concise (no preamble like "이 문서는..."); state the
  decisive signal first. When ``request_alignment`` is "weak", lead with the
  topic mismatch (e.g. "K-뷰티 산업 자료로 AI Agent 요청과 무관함.").
- Output JSON only. No prose, no markdown fences.
"""


VERIFY_FLOW_PLANNER_PROMPT = """You are an editor planning the outline of a research report.

Given the user's request, the planner's topic / goal / must_cover items, the
grounded terms, and a few document titles & summary snippets, decide the
ordered list of report sections the writer will need.

Output JSON only, matching exactly this schema:

{
  "sections": [
    {
      "title": "섹션 제목 (자연어 명사구, 한 문장)",
      "description": "이 섹션에서 다룰 내용을 1~2문장으로 설명",
      "role": "intro" | "body" | "conclusion",
      "keywords": ["섹션 내부 검색에 도움될 키워드 3~6개"]
    }
  ]
}

Rules:
- ``sections`` length must be between min_sections and max_sections (inclusive),
  values are provided in the user payload.
- The very first section's role must be ``intro``; the very last section's
  role must be ``conclusion``; everything in between is ``role=body``.
- Order the sections by the actual reading flow of the report
  (e.g. 정의/배경 → 핵심 메커니즘 → 응용/한계 → 마무리).
- ``title`` is a natural-language noun phrase, NOT a keyword dump
  ("MCP 개요" OK, "mcp ai docs" NOT OK).
- ``description`` must read like a one-sentence editorial brief so the
  writer immediately knows why the section exists.
- Do not invent sections the source documents could not plausibly support —
  stay inside the topic + must_cover + grounded_terms space.
- Use the language of the user's request (Korean if Korean; English otherwise)
  for ``title``/``description``/``keywords``. Preserve domain proper nouns
  in their original form even when answering in Korean.
- Output JSON only. No prose, no markdown fences."""


__all__ = ["RELIABILITY_JUDGE_PROMPT", "VERIFY_FLOW_PLANNER_PROMPT"]
