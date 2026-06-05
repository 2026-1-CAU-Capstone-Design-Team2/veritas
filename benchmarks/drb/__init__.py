"""DeepResearch Bench (DRB) harness for comparing Veritas AutoSurvey against a
flat LLM + web-search/fetch baseline.

This package is **evaluation-only**. It drives the production AutoSurvey
pipeline and a separate flat baseline, exports their reports to DRB's raw-data
JSONL format, and helps analyze the official RACE/FACT evaluator outputs. It
never imports the vendored DRB evaluator internals (`deep_research_bench/utils`,
`.../prompt`); it only produces the files those scripts consume and documents
the commands to run them.
"""
