SYSTEM_PROMPT = """You are a careful research assistant running on a local model.
Return concise, factual, structured answers.
Do not invent sources or URLs.
When asked for JSON, return valid JSON only.
"""

TERM_GROUNDING_PROMPT = """Ground key terms in the user's request before planning.
Return JSON only with this schema:
{
  "request_language": string,
  "grounded_terms": [string, ...],
  "candidate_entities": [string, ...],
  "disambiguation_notes": [string, ...],
  "seed_queries": [string, ...]
}
Rules:
- Keep grounded terms compact and literal when possible.
- Include candidate entities only when ambiguity exists.
- Seed queries should be concrete, high-signal, and web-search friendly.
- If the request includes time-sensitive or relative temporal expressions, call `current_time` first when available.
- Temporal expressions include examples like: latest/recent/current, as of, today/yesterday/tomorrow, this week/month/year, last week/month/year, 현재/최신/최근/동향/오늘/어제/내일/이번 주/지난주/이번 달/올해.
- Do not guess current date/year from model memory. Resolve it from tool output first, then generate grounded terms and seed queries.
- If the request has no temporal dependency, avoid unnecessary `current_time` calls.
"""

INITIAL_PLANNER_PROMPT = """Build an initial lightweight research plan.
Use the user request and grounded terms as the primary anchor.
Return JSON only with this schema:
{
  "topic": string,
  "goal": string,
  "search_queries": [string, ...],
  "must_cover": [string, ...],
  "keywords": [string, ...]
}
Generate 6-10 search queries. Keep them diverse and web-search friendly.
Do not overfit to prior knowledge beyond grounded terms.
If the request includes time-sensitive or relative temporal expressions, call `current_time` when available before generating queries.
Examples: latest/recent/current, as of, today/yesterday/tomorrow, this week/month/year, last week/month/year, 현재/최신/최근/동향/오늘/어제/내일/이번 주/지난주/이번 달/올해.
Use the returned date/year context in query wording instead of guessing a year from model memory.
If no temporal dependency exists, skip the tool.
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
- Keep queries concrete and evidence-seeking.
- Refresh must_cover and keywords using current gap_directions each replan.
- Remove stale or already-resolved coverage items when new gap signals are stronger.
- Avoid returning identical must_cover/keywords when new gap_directions are provided.
- If no meaningful new direction exists, return an empty search_queries list.
- If the request includes time-sensitive or relative temporal expressions, call `current_time` when available and use its date/year context.
- Temporal examples: latest/recent/current, as of, today/yesterday/tomorrow, this week/month/year, last week/month/year, 현재/최신/최근/동향/오늘/어제/내일/이번 주/지난주/이번 달/올해.
- Do not infer today's date from model memory; anchor date-sensitive replan queries to tool output.
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
Keep it concise. Prefer 4-5 sentence summary and 3-5 key points.
"""

BATCH_SUMMARY_PROMPT = """You are given an original user request and multiple document summaries.
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
- Deduplicate overlapping content.
- Mention support frequency when relevant.
- Be concrete and concise.
"""