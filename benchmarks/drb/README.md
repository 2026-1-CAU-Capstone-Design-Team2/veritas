# DRB AutoSurvey Benchmark Harness

Compares **Veritas AutoSurvey** (iterative design) against a **flat one-shot
baseline** (search + fetch + one report call) on
[DeepResearch Bench](https://github.com/Ayanami0730/deep_research_bench) (DRB),
to quantify how much the iterative pipeline improves research quality.

> With the same generator LLM, same web-search/fetch primitive, same fetched
> document budget, and same citation format, does AutoSurvey beat a flat
> baseline on DRB RACE/FACT?

This is an **evaluation harness only**. It does not change the production
AutoSurvey algorithm, and it never imports the vendored DRB evaluator internals
(`deep_research_bench/utils`, `.../prompt`) — it only produces the raw-data
JSONL those scripts consume and helps analyze their outputs.

## Layout

| Module | Responsibility |
|---|---|
| `drb_vendor.py` | Resolve/validate the `deep_research_bench/` checkout; build the official raw-output path; reject traversal. |
| `drb_io.py` | Robust JSON-object iterator; load/filter `query.jsonl`; write the **official** raw rows (`id`/`prompt`/`article` only) + a `.meta.jsonl` sidecar; resume via completed ids. |
| `citation_adapter.py` | Export a workspace `final.md` → DRB article: renumber `[doc_NNN]`→`[n]` (first-appearance), build `## References` from `summary/index.json` (prefer `final_url`). Never mutates `final.md`. |
| `flat_agent.py` | The flat baseline orchestration, against injected `query/search/fetch/report` callables. No AutoSurvey imports. |
| `veritas_runner.py` | CLI: drive AutoSurvey like `main.py`, one workspace per task, export article. |
| `flat_runner.py` | CLI: drive the flat baseline with the **same** generator + `WebSearchTool` + `fetch_with_crawl4ai`. |
| `validate_raw_data.py` | Pre-flight: official keys only, non-empty article, inline `[n]` citations, URL-bearing `## References`. |
| `analyze_results.py` | Parse RACE/FACT outputs → `bench_results/drb/<comparison>/` (`summary.csv`, `paired_deltas.csv`, `comparison_report.md`); mean/median delta, win rate, deterministic bootstrap CI; aggregate-only fallback. |
| `crawl4ai_scrape.py` | Drop-in for DRB's FACT **scrape** stage using `fetch_with_crawl4ai` — runs FACT with **no `JINA_API_KEY`**. Same `deduplicated.jsonl` in / `scraped.jsonl` out; vendored evaluator untouched. Non-official variant — label `fact_crawl4ai_budget`. |

Flat-baseline prompts live in `core/prompts/drb_benchmark.py` (all prompt copy
stays under `core/prompts/`).

## Fairness

Both arms use: the **same** generator (`build_autosurvey_llm(LLMClient(...))` —
local llama-server, or OpenAI when the env selects it), the **same**
`WebSearchTool`, the **same** `fetch_with_crawl4ai` with the same
`--fetch-max-chars` cap, and produce numeric `[n]` citations + URL references.
The flat baseline calls **no** AutoSurvey component. Neither arm receives chat
memory, RAG state, screen context, local-private docs, or proactive state.

## Manual run (needs a running llama-server; uses the local `agent` env)

```powershell
# 1) Generate raw DRB articles for both systems (2-task smoke shown).
C:\Users\pc21\miniconda3\envs\agent\python.exe -m benchmarks.drb.veritas_runner `
  --model-name veritas_autosurvey_local_m15 --max-docs 15 --scout-docs 3 --batch-size 5 --limit 2 --resume

C:\Users\pc21\miniconda3\envs\agent\python.exe -m benchmarks.drb.flat_runner `
  --model-name flat_local_web_m15 --max-docs 15 --search-query-count 5 --fetch-max-chars 100000 --limit 2 --resume

# 2) Validate the raw files before judging.
C:\Users\pc21\miniconda3\envs\agent\python.exe -m benchmarks.drb.validate_raw_data `
  deep_research_bench\data\test_data\raw_data\veritas_autosurvey_local_m15.jsonl `
  deep_research_bench\data\test_data\raw_data\flat_local_web_m15.jsonl

# 3) Run the DRB evaluator (from inside deep_research_bench/, sets TARGET_MODELS).
#    Budget-judge pilot env: RACE_MODEL=gpt-5.4-mini FACT_MODEL=gpt-5.4-mini
#    Official confirmation env: RACE_MODEL=gpt-5.5  FACT_MODEL=gpt-5.4-mini
#    (also LLM_BACKEND, OPENAI_API_KEY/OPENROUTER_API_KEY, JINA_API_KEY)

# 4) Analyze the two systems' RACE/FACT outputs.
C:\Users\pc21\miniconda3\envs\agent\python.exe -m benchmarks.drb.analyze_results `
  --system-a veritas_autosurvey_local_m15 --system-b flat_local_web_m15 --label budget_judge
```

## Cost / judge policy (≈ USD 90 planning budget)

1. Unit tests — no network, no LLM, no judge cost.
2. 2-task smoke — generation only, no judge (near USD 0 with the local generator).
3. 10-task budget-judge pilot — `RACE_MODEL=gpt-5.4-mini`, `FACT_MODEL=gpt-5.4-mini`;
   label results **`budget_judge`** (internal, *not* a leaderboard score). Stop if
   judge spend approaches USD 60 before official confirmation.
4. 3–5 overlapping tasks official confirmation — `RACE_MODEL=gpt-5.5`,
   `FACT_MODEL=gpt-5.4-mini`; label **`official_judge_confirmation`**.
5. Full 100-task official judging for two systems is **out of scope** for this
   budget — mark it "not run". Verify current model pricing before any paid run.

## What is NOT run automatically

Unit tests use fakes only. They never hit the network, a real LLM, or the DRB
judge. Generating articles and running RACE/FACT are manual steps requiring a
llama-server (generation) and provider/Jina API keys (judging). Generated raw
articles, evaluator outputs, `runs/drb/`, and `bench_results/drb/` are git-ignored.
