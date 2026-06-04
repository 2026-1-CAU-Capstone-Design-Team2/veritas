"""AutoSurvey pipeline prompts.

The structured survey workflow uses these in the order:

* :data:`TERM_GROUNDING_PROMPT` — extract literal terms from the user
  request (no query generation; that's the planner's job).
* :data:`INITIAL_PLANNER_PROMPT` — first-pass research plan + initial
  search queries from the grounded terms.
* :data:`REPLANNER_PROMPT` — follow-up plan after a collect cycle, with
  gap directions extracted from the latest batch summary.
* :data:`PLANNER_PROMPT` — legacy single-shot planner kept for the CLI
  ``--phase plan`` entrypoint.
* :data:`DOC_SUMMARY_PROMPT` — per-document JSON summary (single-pass
  path).
* :data:`DOC_CHUNK_NOTES_PROMPT` / :data:`DOC_SUMMARY_REDUCE_PROMPT` —
  map/reduce path for documents that don't fit one context window.
* :data:`BATCH_SUMMARY_PROMPT` — 5-doc cycle summary that feeds gap
  analysis and the final report. Enforces ``[doc_<id>]`` citation markers
  so the verification layer can map findings back to source docs.
* :data:`FINAL_PROMPT` — final markdown brief synthesized from all batch
  summaries.
"""


TERM_GROUNDING_PROMPT = """Extract the most important literal terms from the user's request before planning.
Return JSON only with this schema:
{
  "request_language": string,
  "grounded_terms": [string, ...],
  "candidate_entities": [string, ...],
  "disambiguation_notes": [string, ...]
}
Rules:
- Extract terms only. Do not generate search queries.
- Decide autonomously from the user's request text; no heuristic candidate list is provided.
- Return 3-8 compact terms unless the request is extremely short.
- Prefer literal technical terms, named entities, filenames, module names, product names, model names, algorithms, datasets, and domain concepts that appear in the request.
- Do not add modifiers such as arXiv, paper, conference, benchmark, survey, definition, latest, recent, or a year unless they literally appear in the user's request as important terms.
- Do not transform the user's intent into a search strategy. Query construction is handled by the initial planner.
- Keep Korean terms in Korean and English terms in English.
- Use candidate_entities only for ambiguous names or acronyms.
- Use disambiguation_notes only when ambiguity would affect planning.
"""

INITIAL_PLANNER_PROMPT = """Build an initial lightweight research plan.
Use the user request and grounded_terms as the primary anchors.
The term grounding stage only extracts important terms; it does not provide search queries.
Return JSON only with this schema:
{
  "topic": string,
  "goal": string,
  "search_queries": [string, ...],
  "must_cover": [string, ...],
  "keywords": [string, ...]
}
Generate 4-8 search queries. Keep them diverse and web-search friendly.
Query rules:
- Build each query from the user's actual task intent plus the grounded_terms.
- If the user includes explicit site constraints such as `site:https://example.com` or `site:example.com`, preserve those reference sites and include site-scoped queries for them.
- Do not blindly paste the whole user request into every query.
- Match the user's intent: concept explanation, implementation, product comparison, academic literature, troubleshooting, policy, or latest news require different query styles.
- Do not add arXiv, paper, conference, benchmark, survey, or 논문 unless the user explicitly asks for academic/research literature or the topic is clearly a research-paper survey.
- Do not add definition/정의 unless the user asks for a definition, concept explanation, or term disambiguation.
- Do not append the current year unless the user asks for latest/recent/current information.
- Prefer official docs, project repositories, vendor documentation, standards, or primary sources when applicable.
Memory context rule:
- If the input includes memory_brief, use it only for stable user preferences, project context, and constraints that affect planning.
- Do not treat memory_brief as evidence, source content, or citation material.
- The explicit user_request overrides memory_brief whenever they conflict.
Do not overfit to prior knowledge beyond the user request and grounded terms.
Use `current_time_context` from the input when the request includes time-sensitive or relative temporal expressions.
Examples: latest/recent/current, as of, today/yesterday/tomorrow, this week/month/year, last week/month/year, 현재/최신/최근/동향/오늘/어제/내일/이번 주/지난주/이번 달/올해.
Use that date/year context in query wording instead of guessing a year from model memory.
If no temporal dependency exists, do not force a date into the queries.
"""

REPLANNER_PROMPT = """Replan the research queries after evidence gap analysis.
You will receive original request, grounded terms, prior plan, discovered gaps, and already-used queries.
Return JSON only with this schema:
{
  "topic": string,
  "goal": string,
  "search_queries": [string, ...],
  "must_cover": [string, ...],
  "keywords": [string, ...]
}
Rules:
- Prioritize unresolved gap directions.
- Avoid already-used queries unless there is no better alternative.
- Preserve explicit user-provided site constraints such as `site:https://example.com` or `site:example.com` when generating follow-up queries.
- Keep queries concrete and evidence-seeking.
- Refresh must_cover and keywords using current gap_directions each replan.
- Remove stale or already-resolved coverage items when new gap signals are stronger.
- Avoid returning identical must_cover/keywords when new gap_directions are provided.
- If no meaningful new direction exists, return an empty search_queries list.
- If the request includes time-sensitive or relative temporal expressions, use `current_time_context` from the input.
- Temporal examples: latest/recent/current, as of, today/yesterday/tomorrow, this week/month/year, last week/month/year, 현재/최신/최근/동향/오늘/어제/내일/이번 주/지난주/이번 달/올해.
- Do not infer today's date from model memory; anchor date-sensitive replan queries to `current_time_context`.
"""

PLANNER_PROMPT = """Convert the user's research request into a JSON spec.
Return JSON only with this schema:
{
  "topic": string,
  "goal": string,
  "search_queries": [string, ...],
  "must_cover": [string, ...],
  "keywords": [string, ...]
}
Generate 5-8 search queries. Keep them diverse and web-search friendly.
"""

DOC_SUMMARY_PROMPT = """Summarize the document for later synthesis.
Return JSON only with this schema:
{
  "title": string,
  "source_type": string,
  "summary": string,
  "key_points": [string, ...],
  "reliability_notes": [string, ...],
  "keywords": [string, ...]
}
Rules:
- Keep it concise. Prefer 4-5 sentence summary and 3-5 key points.
- Write the summary and notes in the original user request language when it is known.
- If the user request language is Korean, write Korean summaries even when the document title, source metadata, or technical terms are in English.
- Preserve technical terms, model names, product names, APIs, filenames, and citations in their original form when appropriate.
"""

# Map step of the long-document map-reduce path: notes are extracted from one
# chunk at a time so that no part of an over-long document is truncated away.
# Free-form text output (not JSON) is intentional — it is far more reliable for
# small local models than strict JSON, and the strict schema is only required
# once, at the reduce step below.
DOC_CHUNK_NOTES_PROMPT = """Extract structured notes from ONE part of a longer document.
You are given the document metadata, this part's index, the total number of parts, and the text of this part only. Later parts will be combined into a single document summary.
Return plain text notes only — no JSON, no markdown headings, no preamble.
Capture, when present in THIS part:
- Concrete factual claims, definitions, methods, and conclusions
- Numbers, dates, metrics, named entities, and model/product/API/file names
- Specific statements worth quoting or verifying later
Rules:
- Use only content from the provided text part. Do not invent or infer beyond it.
- Produce compact, atomic note lines (one claim per line). Do not write a narrative.
- If this part has no substantive content, return exactly: (no substantive content)
- Write notes in the original user request language when it is known. If it is Korean, write Korean notes even when the document text or technical terms are English.
- Preserve technical terms, model/product/API names, filenames, numbers, and citations in their original form.
"""

# Reduce step of the long-document map-reduce path: ordered per-chunk notes are
# synthesized into ONE document summary using the exact same schema as
# DOC_SUMMARY_PROMPT, so downstream rendering is identical for both paths.
DOC_SUMMARY_REDUCE_PROMPT = """Combine per-part notes from a long document into ONE document summary.
You are given the document metadata and ordered notes extracted from every part of the document. The notes already cover the full document; treat them as the complete evidence.
Return JSON only with this schema:
{
  "title": string,
  "source_type": string,
  "summary": string,
  "key_points": [string, ...],
  "reliability_notes": [string, ...],
  "keywords": [string, ...]
}
Rules:
- Synthesize across ALL parts. Merge duplicates and resolve overlap; do not just concatenate the notes.
- Keep it concise. Prefer a 4-6 sentence summary and 3-6 key points.
- Use only information present in the provided notes. Do not add outside knowledge.
- Write the summary and notes in the original user request language when it is known. If it is Korean, write Korean even when titles, source metadata, or technical terms are English.
- Preserve technical terms, model names, product names, APIs, filenames, and citations in their original form when appropriate.
"""

BATCH_SUMMARY_PROMPT = """You are given an original user request and the clean Markdown of multiple collected documents.
Each document is introduced with a header line of the form ``=== doc_<id> ===``
followed by its title/URL/domain metadata and its body. The ``<id>`` is the
document's stable identifier (three-digit string such as ``000``, ``017``); use
that exact identifier when you cite the document.

Create a markdown batch note with these sections:
# Batch Summary
## Repeated Findings
## New Findings
## Reliability Notes
## Gaps / Next Search Directions
### Core Gap (Relevant To User Request)
### Supporting Gap (Lower Priority)
### Off-topic / Incidental Gap
Rules:
- Explicitly compare each candidate gap against the original user request before classifying it.
- Put a gap in Core Gap only when resolving it is directly needed to satisfy the user request.
- Put useful but non-essential details in Supporting Gap.
- Put tangential, incidental, or user-request-irrelevant items in Off-topic / Incidental Gap.
- For every Core Gap bullet, append " - Relevance: <short reason tied to user request>".
- If a section has no items, write "- None".
- Be concise and remove redundant statements.
- Treat only document body content as evidence. Page chrome — site navigation, social/share widgets, related-links blocks, footers, and cookie/legal notices — is never a finding: do not turn it into a Repeated/New Finding, a citation, or a reliability note. (This is a general principle, not a fixed keyword list.)
- Write the batch note in the original user request language. If the original request is Korean, write the section content in Korean while preserving fixed markdown headings if needed by downstream code.

Citation policy — downstream verification needs to map every finding back to
its source documents, so every claim in ``## Repeated Findings``,
``## New Findings``, and ``## Reliability Notes`` MUST end with one or more
inline doc citations of the form ``[doc_<id>]``:
- Cite every supporting document. A finding repeated across three sources
  ends with ``[doc_001][doc_004][doc_009]``; a single-source finding ends with
  ``[doc_007]``.
- A document earns a citation on a finding only when that document
  *independently* supports that specific claim. When several ids are attached
  to one finding, each id must support it on its own — never cite a document
  merely because it is on a related topic.
- Use ONLY the doc_ids that appear as ``=== doc_<id> ===`` headers in the
  input. Never invent or guess an id. Never abbreviate as ``doc_7`` — keep
  the original three-digit form.
- Place the marker(s) at the end of the bullet (or at the end of the
  sentence inside a multi-sentence bullet). Do not wrap them in parentheses;
  the bare ``[doc_<id>]`` form is required so a regex parser can find them.
- Gap section bullets (``### Core Gap`` / ``### Supporting Gap`` /
  ``### Off-topic`` ) describe what is *missing* and so do not need citations.

Content fidelity — the final research report is synthesized from these
batch notes, so concrete signal that gets dropped here is lost forever:
- Preserve concrete numerical data — metrics, dates, percentages, costs,
  benchmark scores, sample sizes — verbatim with their original units.
- Preserve formal expressions when the source carries them: equations,
  algorithms (e.g. ``UCB1``, ``kl-UCB``, regret bounds like ``O(\\sqrt{T})``),
  pseudo-code, and named theorems. Quote them in inline code or LaTeX-style
  notation rather than paraphrasing into prose.
- Preserve named entities: model/product/paper/author names, dataset
  names, API endpoints, model identifiers (``claude-sonnet-4-6``, etc.),
  command flags, and file paths — in their original casing/spelling.
- When a source carries a small comparison table that fits within ~6 rows
  × 4 cols, reproduce it as a markdown table in ``## New Findings`` or
  ``## Repeated Findings`` instead of flattening it into bullet text.
- Use short verbatim quotes (\"…\") for a source's distinctive claim or
  definition; the citation marker still goes at the end of the bullet.
- These fidelity rules override the general \"be concise\" rule whenever
  the concrete signal would otherwise be paraphrased away.
"""

FINAL_PROMPT = """Create the final markdown report.
Required sections:
# Final Research Brief
## User Request
## Executive Summary
## Consolidated Findings
## Repeated / Well-Supported Points
## Conflicts or Uncertainties
## Source Notes
## Remaining Gaps
Rules:
- The text provided to you below is INTERNAL INPUT (the original request, a
  short plan summary, run stats, and the batch summaries). Treat it strictly as
  source material. NEVER reproduce that input verbatim and NEVER output JSON,
  the plan object, search queries, run-stat keys, or the batch-summaries list as
  report content.
- The ``## User Request`` section must contain ONLY the user's original request
  (a brief restatement or quote of it). Never place the plan, search queries,
  batch summaries, keywords, or any JSON / payload keys in that section.
- Deduplicate overlapping content.
- Mention support frequency when relevant.
- Be concrete and concise.
- Write the report body in the original user request language. If the original request is Korean, write the report body in Korean while preserving technical terms, source titles, document IDs, and citations as-is.
- Use one citation marker format everywhere — body prose AND inside table
  cells: bracketed ``[doc_<id>]`` with the original three-digit id, for example
  ``[doc_000]``. Never emit a bare ``doc_000`` (without brackets), not even in a
  table cell or the ``Doc ID`` column; the UI links only the bracketed form.
- Attach a citation only where it backs a substantive claim drawn from that
  source; do not decorate every sentence or cite non-evidentiary filler. When
  several documents are cited on one sentence, each must independently support
  that sentence's specific claim — not merely share its topic.
- The ``## Source Notes`` section MUST be a Markdown table, not bullets or
  paragraphs. Use one row per important source document.
- Each ``## Source Notes`` row is a single-line Markdown table row delimited by
  pipes (``| … | … |``) with NO leading bullet (`-`/`*`) and no line breaks
  inside a cell; emit the header row and its ``|---|---|…|`` separator exactly.
- The ``## Source Notes`` table MUST use these columns:
  ``Doc ID`` | ``Title / Type`` | ``Year`` | ``What it contributes`` |
  ``Reliability / Caveat``.
- In the ``Doc ID`` column, use the canonical bracketed marker
  ``[doc_<id>]`` so the UI can link it consistently.
- If a value is unknown, write ``-`` rather than inventing it.
- Source notes should describe only substantive evidence or caveats from each
  document; do not turn non-evidentiary page chrome or source metadata into a
  finding.
- Write in a report register throughout. Do NOT end with an assistant-style
  offer or chat closing (for example "If you want, I can …" or
  "원하시면 … 해 드리겠습니다"). If forward actions are worth stating, present them
  tersely as report content — a short "Recommended next steps" list under
  ``## Remaining Gaps`` — never as a conversational offer to the reader.
"""


__all__ = [
    "BATCH_SUMMARY_PROMPT",
    "DOC_CHUNK_NOTES_PROMPT",
    "DOC_SUMMARY_PROMPT",
    "DOC_SUMMARY_REDUCE_PROMPT",
    "FINAL_PROMPT",
    "INITIAL_PLANNER_PROMPT",
    "PLANNER_PROMPT",
    "REPLANNER_PROMPT",
    "TERM_GROUNDING_PROMPT",
]
