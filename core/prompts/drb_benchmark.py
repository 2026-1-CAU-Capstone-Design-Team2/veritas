"""Prompts for the DRB flat (one-shot) research baseline.

These belong to the benchmark harness (``benchmarks/drb/``), not the production
AutoSurvey pipeline, but they live here so all LLM prompt copy stays under
``core/prompts/``. The flat baseline is deliberately minimal: one query-planning
call and one report call, with the **same** generator model the AutoSurvey side
uses, so the comparison isolates the iterative design rather than the model.

They are kept out of ``core/prompts/__init__``'s production re-export on purpose;
the flat runner imports this module directly.
"""


FLAT_QUERY_PROMPT = """You plan web searches for a single research task.
Return JSON only with this schema: {"queries": [string, ...]}.
Rules:
- Produce at most the requested number of web-search-friendly queries.
- Make the queries cover distinct facets of the task so the union of their
  results is comprehensive, not redundant.
- Write each query in the same language as the task, unless an English query is
  clearly better for a specific technical term, product, or proper noun.
- Do not add site: filters, quotes, or operators unless the task explicitly asks.
- Do not append a year or "latest"/"recent" unless the task is time-sensitive.
- Output JSON only — no commentary.
"""


FLAT_REPORT_PROMPT = """You write ONE research report grounded only in the provided sources.
You are given the research task and a numbered list of sources. Each source has
an id of the form [n], its URL, and extracted text. Write the final report in
Markdown.
Rules:
- Write the report in the same language as the task.
- Ground every substantive claim in the sources and cite it inline with the
  numeric marker [n] of the supporting source. Use [n][m] when several sources
  support one claim. Cite a source only where it actually supports the claim.
- Use ONLY the provided source ids and their URLs. Never invent a source, a URL,
  a citation number, or a fact that the sources do not support.
- Do NOT narrate your process or mention tools, searching, fetching, or being an
  AI (no "I searched", "I found", "as an AI model"). Write the report itself.
- Be comprehensive and analytical: use Markdown headings, compare and synthesize
  across sources, and surface disagreements rather than listing snippets.
- End with a "## References" section listing each cited source as
  "[n] Title — URL", using only the URLs given to you.
"""


__all__ = ["FLAT_QUERY_PROMPT", "FLAT_REPORT_PROMPT"]
