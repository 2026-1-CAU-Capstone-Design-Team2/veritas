# Implementation Plan: Vendored DeepResearch Bench AutoSurvey Benchmark Harness

## Goal
Build a reproducible benchmark harness inside the Veritas repository that compares **Veritas AutoSurvey** against a **flat LLM + web-search/fetch baseline** on DeepResearch Bench (DRB), while keeping the official DRB repository available to Codex/Claude and teammates from the same workspace.

This version assumes the DRB repository is placed **inside** the Veritas repository and committed for teammate smoke testing. The harness must still keep benchmark-generated artifacts, API keys, logs, full fetched bodies, and evaluator outputs out of git.

The core comparison claim remains:

> Under the same generation LLM, same search provider, same fetch budget, and same document budget, does Veritas AutoSurvey produce better DRB RACE/FACT scores than a flat LLM that only receives a prompt plus web search/fetch primitives?

This is a benchmark/evaluation harness task. Do not tune the production AutoSurvey algorithm in this increment.

---

## Codex Role
Codex should use this document as the implementation plan and review checklist.

Codex should either:

1. paste this whole plan into the repository `INSTRUCTION.md` under a new current-task heading, or
2. add it as `docs/drb_autosurvey_benchmark_vendor_instruction.md` and reference that file from `INSTRUCTION.md`.

Codex reviews Claude's implementation by reading `git diff`, checking that the vendored DRB tree is safe to commit, and verifying that benchmark outputs remain ignored.

### Codex review commands

```powershell
git status --short
git diff --stat
git diff -- .gitignore benchmarks core tests ARCHITECTURE.md AGENTS.md CLAUDE.md

git status --short vendor/deep_research_bench

git check-ignore -v runs/drb/veritas_autosurvey_gpt5mini_m15/task_001/final.md
git check-ignore -v vendor/deep_research_bench/data/test_data/raw_data/veritas_autosurvey_gpt5mini_m15.jsonl
git check-ignore -v vendor/deep_research_bench/results/race/veritas_autosurvey_gpt5mini_m15/race_result.txt

C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_drb_benchmark_io -v
C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_drb_vendor_layout -v
C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_drb_citation_adapter -v
C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_drb_flat_baseline -v
C:\Users\asdf\.conda\envs\agent\python.exe -m unittest tests.test_drb_analysis -v
```

Full `unittest discover` is useful but not required to pass if unrelated existing failures remain. Claude must report whether it was run and classify any failures.

---

## Required Repository Layout

Use this canonical internal layout:

```text
veritas/
  vendor/
    deep_research_bench/              # vendored official DRB checkout, committed source/data only
      README.md
      LICENSE
      requirements.txt
      run_benchmark.sh
      deepresearch_bench_race.py
      data/
        prompt_data/query.jsonl       # committed benchmark prompts
        criteria_data/                # committed if present in upstream
        test_data/
          raw_data/                   # generated model outputs are ignored by pattern
          cleaned_data/               # generated cleaned outputs are ignored by pattern
      prompt/
      utils/
      VENDOR_INFO.md                  # added by this task
  benchmarks/
    __init__.py
    drb/
      __init__.py
      README.md
      commands.md
      common.py
      drb_io.py
      drb_vendor.py
      citation_adapter.py
      veritas_runner.py
      flat_agent.py
      flat_runner.py
      validate_raw_data.py
      analyze_results.py
  core/
    prompts/
      drb_benchmark.py
  tests/
    test_drb_benchmark_io.py
    test_drb_vendor_layout.py
    test_drb_citation_adapter.py
    test_drb_flat_baseline.py
    test_drb_analysis.py
  runs/                              # ignored
  bench_results/                     # ignored
```

### Why `vendor/deep_research_bench/`

The previous sibling-repo plan is no longer available because Codex/Claude are already running inside a single Veritas workspace. Therefore the benchmark code must treat DRB as a vendored dependency inside the repository.

The vendored path must be configurable, but examples and defaults should use:

```text
vendor/deep_research_bench
```

Every CLI should accept `--drb-root`, defaulting to `vendor/deep_research_bench` relative to the Veritas repo root.

---

## Vendoring Policy

### Default: full vendor copy, not sibling repo

For this task, assume the team wants a normal clone of Veritas to contain the DRB benchmark prompts and evaluator code. Use a full vendored copy unless the repository already has a submodule policy.

If `vendor/deep_research_bench/.git` exists after cloning DRB, Claude must not leave that nested `.git` directory in the final commit unless Codex explicitly chooses submodule mode.

Allowed states:

```text
# Full-vendor mode, preferred for easiest teammate testing:
veritas/vendor/deep_research_bench/README.md
veritas/vendor/deep_research_bench/LICENSE
veritas/vendor/deep_research_bench/data/prompt_data/query.jsonl
# no vendor/deep_research_bench/.git directory
# no .gitmodules entry required

# Submodule mode, only if Codex explicitly chooses it:
veritas/.gitmodules
veritas/vendor/deep_research_bench    # gitlink
```

Do not mix the two. A nested `.git` directory that is not registered as a submodule is a review failure.

### Required `VENDOR_INFO.md`

Add `vendor/deep_research_bench/VENDOR_INFO.md`:

```markdown
# DeepResearch Bench Vendor Info

- Upstream: https://github.com/Ayanami0730/deep_research_bench
- Vendor mode: full-copy | submodule
- Upstream commit: <git sha from upstream checkout>
- Vendored on: 2026-06-05
- Local modifications:
  - VENDOR_INFO.md added.
  - Generated Veritas/flat benchmark outputs are ignored and should not be committed.
- License: preserve upstream LICENSE file.

## Refresh procedure

1. Clone/fetch upstream in a temporary directory.
2. Record the upstream commit hash.
3. Replace this vendor directory, preserving this VENDOR_INFO.md updates and local ignore policy.
4. Run DRB vendor layout tests and benchmark IO tests.
```

If the upstream commit hash is unavailable because the user copied a ZIP rather than a git checkout, write `unknown-copied-snapshot` and say so in Claude's report.

### Required `.gitignore` additions

Add precise ignore patterns. Do not ignore the entire vendored DRB repo.

```gitignore
# DeepResearch Bench vendored evaluator: keep source/data prompts, ignore generated benchmark artifacts
/vendor/deep_research_bench/**/__pycache__/
/vendor/deep_research_bench/**/*.pyc
/vendor/deep_research_bench/.pytest_cache/
/vendor/deep_research_bench/.mypy_cache/
/vendor/deep_research_bench/.ruff_cache/
/vendor/deep_research_bench/.env
/vendor/deep_research_bench/.env.*

# DRB generated model outputs from Veritas benchmark harness
/vendor/deep_research_bench/data/test_data/raw_data/veritas_*.jsonl
/vendor/deep_research_bench/data/test_data/raw_data/flat_*.jsonl
/vendor/deep_research_bench/data/test_data/raw_data/*_pilot*.jsonl
/vendor/deep_research_bench/data/test_data/raw_data/*_smoke*.jsonl
/vendor/deep_research_bench/data/test_data/raw_data/*.meta.jsonl

# DRB generated cleaned/evaluation outputs for Veritas benchmark runs
/vendor/deep_research_bench/data/test_data/cleaned_data/veritas_*/
/vendor/deep_research_bench/data/test_data/cleaned_data/flat_*/
/vendor/deep_research_bench/results/race/veritas_*/
/vendor/deep_research_bench/results/race/flat_*/
/vendor/deep_research_bench/results/fact/veritas_*/
/vendor/deep_research_bench/results/fact/flat_*/

# Veritas benchmark workspaces and local analysis outputs
/runs/drb/
/bench_results/drb/
/benchmarks/drb/out/
/benchmarks/drb/cache/
```

If DRB's checked-out structure differs, adapt the patterns conservatively and add a test that proves generated Veritas/flat artifacts are ignored.

---

## User Value / Completion Criteria

The implementation is complete when:

- [ ] `vendor/deep_research_bench/` exists and contains the official DRB prompt data and evaluator code needed for local evaluation.
- [ ] `vendor/deep_research_bench/VENDOR_INFO.md` records upstream URL, vendor mode, upstream commit or snapshot status, and refresh procedure.
- [ ] `.gitignore` prevents generated Veritas/flat raw outputs, evaluator cleaned outputs, evaluator result folders, `runs/drb/`, and `bench_results/drb/` from being committed.
- [ ] A vendor-layout test verifies the DRB root, `query.jsonl`, `run_benchmark.sh`, and ignore-sensitive generated paths.
- [ ] A Veritas runner can load tasks from `vendor/deep_research_bench/data/prompt_data/query.jsonl` and export `vendor/deep_research_bench/data/test_data/raw_data/veritas_autosurvey_<model>_m<max_docs>.jsonl`.
- [ ] A flat baseline runner can export `vendor/deep_research_bench/data/test_data/raw_data/flat_<model>_web_m<max_docs>.jsonl`.
- [ ] Veritas `[doc_NNN]` citations are converted to DRB-friendly numeric citations such as `[1]`, with a `References` section mapping each number to a URL.
- [ ] Flat baseline reports also use numeric inline citations and a URL-bearing `References` section.
- [ ] Official raw article JSONL contains only `id`, `prompt`, and `article`; task metadata goes to sidecar `.meta.jsonl`.
- [ ] A smoke command can run `--limit 2` for both systems without requiring sibling repo paths.
- [ ] A pilot command can run a stratified `--limit 10` or `--task-ids ...` subset.
- [ ] An analysis script can combine official DRB RACE/FACT outputs into a paired comparison report with mean delta, median delta, win rate, and bootstrap 95% CI when per-task outputs are available.
- [ ] Unit tests use fake LLM/search/fetch where possible. Real network, real OpenAI, and official DRB evaluation are manual benchmark commands, not unit tests.

---

## Benchmark Contract

DRB raw outputs must be written as JSONL with one object per task:

```json
{
  "id": "task_id",
  "prompt": "original_query_text",
  "article": "generated_research_article_with_citations"
}
```

The internal harness should write those files under the vendored evaluator path:

```text
vendor/deep_research_bench/data/test_data/raw_data/<model_name>.jsonl
```

DRB official evaluation is executed from inside the vendored DRB directory:

```bash
cd vendor/deep_research_bench
bash run_benchmark.sh
```

Do not hard-code evaluator model names. Document the values used in the current vendored DRB README and allow `RACE_MODEL` / `FACT_MODEL` to override them.

Important loader note: the DRB `query.jsonl` file may contain embedded newlines or can appear as concatenated JSON objects in some raw views. Implement a robust JSON object iterator using `json.JSONDecoder().raw_decode()` over the entire file text, not only naive line-by-line parsing.

---

## Benchmark Design

### Systems to compare first

Start with one fair pair:

| System name | Generator LLM | Search/fetch budget | Structure |
|---|---|---:|---|
| `flat_gpt5mini_web_m15` | `gpt-5-mini` or configured equivalent | same as Veritas | Flat LLM-generated search queries + direct web search/fetch + one report synthesis |
| `veritas_autosurvey_gpt5mini_m15` | same model | `max_docs=15` | Existing AutoSurvey workflow: grounding → plan → collect → cleanup → summarize → gap/replan → final |

After this pair works, add local-model variants:

| System name | Generator LLM | Purpose |
|---|---|---|
| `flat_local_web_m15` | same local model as AutoSurvey | local baseline |
| `veritas_autosurvey_local_m15` | same local model | local pipeline effect |

### Fairness constraints

- Same generator LLM for Veritas and flat baseline.
- Same web search primitive/provider for both runners.
- Same fetch primitive and `fetch_max_chars` for both runners.
- Same `max_docs`, default `15`.
- Same maximum fetched webpages, not just same number of final citations.
- Same report language behavior: Chinese tasks answer Chinese, English tasks answer English.
- Temperature/reasoning effort should be fixed when the LLM client supports it. If not exposed, record that limitation in metadata.
- Flat baseline must have both search and fetch. Do not make it a weak search-snippet-only baseline unless explicitly configured as a separate `search_only` ablation.
- Flat baseline must not use AutoSurvey internals: no term grounding, no query planner, no source-quality gates, no cleanup, no batch summary memory, no gap/replan loop.

---

## Architecture Constraints

- Do not call the chat-facing `AutoSurveyTool` for DRB generation. It is designed for chat-triggered short surveys and can have chat-facing document caps.
- Prefer direct workflow wiring borrowed from `main.py`: `build_autosurvey_llm()` / registry setup / `AutoSurveyWorkflow.run_all()`.
- `AgentRuntime.run_autosurvey()` is acceptable only if Claude documents why the API/runtime lifecycle is needed and confirms no chat memory brief, RAG state, screen context, or local private documents are injected.
- Do not modify stored `final.md` or source documents for benchmark export. Citation conversion is export-only.
- Do not inject chat memory, local private documents, screen context, RAG chunks, proactive state, or prior user-specific context into DRB generation.
- Do not add deterministic language/site/boilerplate keyword blocklists as part of the benchmark.
- Do not add production dependencies unless unavoidable. Use standard library for JSONL, CSV, bootstrap CI, sidecar metadata, and validation where possible.
- Keep benchmark code isolated under `benchmarks/drb/` and prompts under `core/prompts/drb_benchmark.py`.
- Do not add a Veritas GUI page or API endpoint for benchmark results in this increment.
- Do not modify official DRB evaluator source files unless the change is a vendoring metadata file such as `VENDOR_INFO.md`. If an evaluator patch is necessary, put it in `benchmarks/drb/patches/` and document manual application.
- Do not commit benchmark-generated JSONL, `runs/drb`, official evaluator results, secrets, logs, or full fetched bodies.

---

## Implementation Checklist for Claude Code

### 0. Vendor layout and ignore safety

- [ ] Ensure `vendor/deep_research_bench/` exists. If the DRB repo is currently placed elsewhere inside Veritas, either move it to `vendor/deep_research_bench/` or set the default path in docs/tests to the actual chosen path. Prefer moving to the canonical path.
- [ ] Detect whether `vendor/deep_research_bench/.git` exists.
  - If full-vendor mode: remove the nested `.git` directory before commit and record upstream commit in `VENDOR_INFO.md` before removal.
  - If submodule mode: ensure `.gitmodules` exists and points to the official upstream.
- [ ] Preserve upstream `LICENSE`, `README.md`, `requirements.txt`, `run_benchmark.sh`, `data/prompt_data/query.jsonl`, evaluator scripts, `prompt/`, and `utils/`.
- [ ] Add `vendor/deep_research_bench/VENDOR_INFO.md`.
- [ ] Add `.gitignore` patterns listed above.
- [ ] Add `benchmarks/drb/drb_vendor.py` with helpers:
  - `default_drb_root(repo_root: Path) -> Path`
  - `resolve_drb_root(path: str | Path | None, repo_root: Path) -> Path`
  - `validate_drb_root(drb_root: Path) -> list[str]` returning warnings/errors
  - `drb_query_file(drb_root: Path) -> Path`
  - `drb_raw_output_path(drb_root: Path, model_name: str) -> Path`
- [ ] Add `tests/test_drb_vendor_layout.py`:
  - default DRB root is `vendor/deep_research_bench`
  - required files are checked
  - path resolution rejects traversal outside repo when used by local CLIs
  - generated output path follows `vendor/deep_research_bench/data/test_data/raw_data/<model>.jsonl`
  - expected generated files are ignored by `.gitignore` if the test can safely call `git check-ignore`; otherwise provide a pure pattern test and document manual `git check-ignore` command.

### 1. DRB IO helpers

- [ ] Add `benchmarks/drb/drb_io.py`.
- [ ] Implement `iter_json_objects(text: str) -> Iterator[dict]` using `json.JSONDecoder().raw_decode()`.
- [ ] Implement `load_tasks(query_file: Path, *, limit=None, task_ids=None, languages=None, topics=None) -> list[dict]`.
- [ ] Validate every task has `id` and `prompt`.
- [ ] Preserve `topic`, `language`, or other DRB metadata in in-memory objects and sidecar metadata when present.
- [ ] Implement `write_raw_article_jsonl(out_path, rows)` that writes only official keys by default: `id`, `prompt`, `article`.
- [ ] Implement resume/checkpoint helpers:
  - `load_completed_ids(out_path) -> set[str]`
  - `sidecar_meta_path(out_path) -> Path`
  - atomic per-task metadata append
- [ ] Add `validate_raw_data.py` CLI to check:
  - all rows are parseable
  - required fields exist
  - `article` is non-empty
  - inline numeric citations are present
  - `References` section contains URLs

### 2. Veritas citation adapter

- [ ] Add `benchmarks/drb/citation_adapter.py`.
- [ ] Implement `export_veritas_article(workspace_dir: Path) -> str`:
  - read `final.md`
  - read `summary/index.json`
  - map Veritas doc ids to numeric references in first-appearance order or deterministic index order
  - replace inline `[doc_000]`, `doc_000`, `doc-000`, and `doc000` with numeric `[n]`
  - prefer `final_url`, then `url`, as reference URL
  - append or normalize final `## References` section using `[n] <url> — <title>` lines
  - leave report text otherwise intact
- [ ] Ensure no `[doc_000]` markers remain in the exported DRB article when a URL mapping exists.
- [ ] Do not mutate workspace `final.md`.
- [ ] Handle duplicate documents and duplicate URLs deterministically.
- [ ] If no URL mapping exists for a citation, keep the text readable and record a metadata warning.
- [ ] Do not corrupt code fences, inline code spans, existing external markdown links, or URLs containing `doc_000`-like text.

### 3. Veritas DRB runner

- [ ] Add `benchmarks/drb/veritas_runner.py` with module CLI.
- [ ] Default `--drb-root` to `vendor/deep_research_bench`.
- [ ] Default `--query-file` to `<drb-root>/data/prompt_data/query.jsonl`.
- [ ] Default `--out` to `<drb-root>/data/test_data/raw_data/<model_name>.jsonl`.
- [ ] Default `--work-root` to `runs/drb/<model_name>`.
- [ ] Create one workspace per DRB task:

```text
runs/drb/<model_name>/task_<id>/
```

- [ ] Wire AutoSurvey directly, similar to `main.py`, and skip post-survey chat/RAG unless an explicit flag is provided.
- [ ] Accept these environment variables without hard-coding secrets:

```powershell
$env:VERITAS_AUTOSURVEY_LLM_PROVIDER="openai"
$env:VERITAS_AUTOSURVEY_OPENAI_MODEL="gpt-5-mini"
$env:VERITAS_MAX_DOCS="15"
$env:VERITAS_BATCH_SIZE="5"
$env:VERITAS_SCOUT_DOCS="3"
$env:VERITAS_AUTOSURVEY_FETCH_MAX_CHARS="100000"
```

- [ ] For each task, write sidecar metadata:
  - `id`, `prompt`, `topic`, `language`
  - workspace path
  - started/finished timestamps
  - elapsed seconds
  - max_docs/scout_docs/batch_size/fetch_max_chars
  - generator provider/model from env
  - kept doc count, rejected count, duplicate count, fetch error count if available
  - success/failure status and error message
  - warnings from citation export
- [ ] On task failure, write metadata but do not write a fake article row unless an explicit `--write-failed-placeholders` flag is passed.
- [ ] Failed tasks must be rerunnable by `--resume`.

Example command:

```powershell
C:\Users\asdf\.conda\envs\agent\python.exe -m benchmarks.drb.veritas_runner `
  --drb-root vendor\deep_research_bench `
  --model-name veritas_autosurvey_gpt5mini_m15 `
  --max-docs 15 `
  --scout-docs 3 `
  --batch-size 5 `
  --limit 2 `
  --resume
```

### 4. Flat baseline prompt and agent

- [ ] Add `core/prompts/drb_benchmark.py`.
- [ ] Put all flat-baseline prompts there:
  - search-query generation prompt
  - final flat report prompt
  - citation/reference formatting rules
- [ ] Add `benchmarks/drb/flat_agent.py`.
- [ ] Implement a flat baseline that does **not** call AutoSurvey tools except primitive web search/fetch:
  1. Ask the same LLM for at most `search_query_count` queries.
  2. Run existing `web_search` for those queries.
  3. Deduplicate URLs.
  4. Fetch at most `max_docs` pages with existing `fetch_webpage`.
  5. Build a bounded source packet with numeric source ids.
  6. Ask the LLM once to write a structured report with inline numeric citations and a `References` section.
- [ ] The final flat report prompt must say:
  - answer in the same language as the user task
  - cite every substantive factual claim with `[n]`
  - cite only fetched sources
  - do not invent URLs
  - do not describe tool logs
  - end with `## References`
- [ ] Do not run cleanup, source-quality gates, batch summaries, gap extraction, replan, RAG indexing, or final-report normalizer in the flat baseline.
- [ ] Deterministic URL deduplication and source-packet truncation are allowed as budget control, not research orchestration.

### 5. Flat baseline runner

- [ ] Add `benchmarks/drb/flat_runner.py` with module CLI.
- [ ] Default `--drb-root`, `--query-file`, and `--out` the same way as Veritas runner.
- [ ] Use the same LLM provider/model env as the Veritas runner.
- [ ] Use the same web search and fetch primitives as Veritas.
- [ ] Store sidecar metadata analogous to Veritas runner, but do not store full fetched bodies in JSONL/JSON metadata.
- [ ] Store optional debug source packets under `runs/drb/<model_name>/task_<id>/`, not in official DRB raw JSONL.

Example command:

```powershell
C:\Users\asdf\.conda\envs\agent\python.exe -m benchmarks.drb.flat_runner `
  --drb-root vendor\deep_research_bench `
  --model-name flat_gpt5mini_web_m15 `
  --max-docs 15 `
  --search-query-count 5 `
  --fetch-max-chars 100000 `
  --limit 2 `
  --resume
```

### 6. DRB official evaluation integration

- [ ] Add `benchmarks/drb/README.md` and `benchmarks/drb/commands.md` explaining internal vendored setup.
- [ ] Do not install DRB evaluator dependencies into root `requirements.txt` unless Veritas runtime needs them. Prefer documenting a separate evaluator environment.
- [ ] Provide two documented options:

Option A: use the existing `agent` env if dependency conflicts are acceptable:

```powershell
C:\Users\asdf\.conda\envs\agent\python.exe -m pip install -r vendor\deep_research_bench\requirements.txt
```

Option B: create a separate evaluator env:

```powershell
conda create -n drb-eval python=3.11 -y
conda run -n drb-eval python -m pip install -r vendor\deep_research_bench\requirements.txt
```

- [ ] Document evaluator env vars without secrets:

```powershell
$env:LLM_BACKEND="openai"
$env:OPENAI_API_KEY="..."
$env:JINA_API_KEY="..."
$env:RACE_MODEL="gpt-5.5"
$env:FACT_MODEL="gpt-5.4-mini"
```

- [ ] Do not commit changes to `vendor/deep_research_bench/run_benchmark.sh` just to change `TARGET_MODELS`.
- [ ] Prefer a helper that prints the exact `TARGET_MODELS=(...)` line or creates a local ignored script:

```powershell
C:\Users\asdf\.conda\envs\agent\python.exe -m benchmarks.drb.commands `
  --drb-root vendor\deep_research_bench `
  --models veritas_autosurvey_gpt5mini_m15 flat_gpt5mini_web_m15 `
  --print-target-models-line
```

If implementing `benchmarks.drb.commands` is too much, `commands.md` is enough for this increment.

- [ ] Add validation command before official evaluation:

```powershell
C:\Users\asdf\.conda\envs\agent\python.exe -m benchmarks.drb.validate_raw_data `
  vendor\deep_research_bench\data\test_data\raw_data\veritas_autosurvey_gpt5mini_m15.jsonl
```

- [ ] Document official evaluation:

```bash
cd vendor/deep_research_bench
bash run_benchmark.sh
```

### 7. Result analysis

- [ ] Add `benchmarks/drb/analyze_results.py`.
- [ ] Input arguments:
  - `--drb-root vendor/deep_research_bench`
  - `--models veritas_autosurvey_gpt5mini_m15 flat_gpt5mini_web_m15`
  - `--out-dir bench_results/drb/<comparison_name>`
- [ ] Parse official DRB outputs robustly. If DRB output format changes, fail with a clear message and list the files found.
- [ ] Produce:

```text
bench_results/drb/<comparison>/summary.csv
bench_results/drb/<comparison>/paired_deltas.csv
bench_results/drb/<comparison>/comparison_report.md
```

- [ ] Compute at least:
  - aggregate RACE Overall, Comprehensiveness, Depth/Insight, Instruction Following, Readability
  - aggregate FACT Citation Accuracy and Effective Citations
  - paired per-task deltas when per-task files are available
  - mean delta
  - median delta
  - win rate
  - bootstrap 95% CI with a fixed seed
  - language/topic breakdown when `query.jsonl` topic/language metadata is available
- [ ] If official DRB only exposes aggregate result files in the checked-out version, write aggregate comparison and clearly mark paired analysis as unavailable until per-task files are located.

### 8. Tests

Add focused `unittest` tests. Do not use real network or real LLM in unit tests.

#### `tests/test_drb_vendor_layout.py`

- [ ] `default_drb_root()` resolves to `vendor/deep_research_bench`.
- [ ] `validate_drb_root()` checks required files.
- [ ] `drb_raw_output_path()` returns `<drb-root>/data/test_data/raw_data/<model>.jsonl`.
- [ ] Traversal-like `--drb-root` paths are rejected when a CLI accepts only repo-internal roots.
- [ ] If `git` is available, generated Veritas/flat output patterns are ignored.
- [ ] If `vendor/deep_research_bench/.git` exists and `.gitmodules` does not register it, the test warns or fails depending on Codex's selected vendor mode.

#### `tests/test_drb_benchmark_io.py`

- [ ] normal JSONL file loads correctly
- [ ] concatenated JSON object stream loads correctly
- [ ] embedded newline in prompt is preserved
- [ ] `limit`, `task_ids`, `languages`, `topics` filters work
- [ ] official raw output writer emits only `id`, `prompt`, `article` by default
- [ ] resume helper detects completed ids

#### `tests/test_drb_citation_adapter.py`

- [ ] `[doc_000]` converts to `[1]`
- [ ] bare `doc_000`, `doc-000`, and `doc000` convert to `[1]`
- [ ] first-appearance order or configured deterministic order is stable
- [ ] `final_url` is preferred over `url`
- [ ] duplicate URLs do not create duplicate reference rows unless intentionally configured
- [ ] `final.md` source file is not modified
- [ ] unmapped doc ids create metadata warnings and do not crash
- [ ] code fences and existing external markdown links are not corrupted

#### `tests/test_drb_flat_baseline.py`

- [ ] flat agent uses injected fake LLM/search/fetch callables
- [ ] max search query count and max fetched docs are enforced
- [ ] fetched URLs are deduplicated
- [ ] final report prompt contains numeric source ids and URL mapping
- [ ] final article includes inline numeric citations and `References`
- [ ] flat agent does not import or call `AutoSurveyWorkflow`, `QueryPlanTool`, `DocumentCleanupTool`, `DocumentSummarizeTool`, or `FinalReportTool`

#### `tests/test_drb_analysis.py`

- [ ] paired delta calculation is correct
- [ ] win rate is correct
- [ ] bootstrap CI is deterministic under fixed seed
- [ ] missing per-task official outputs produce a clear degraded aggregate-only report

---

## Manual Smoke Sequence

Run these after unit tests pass.

```powershell
# 0. Environment
$env:VERITAS_AUTOSURVEY_LLM_PROVIDER="openai"
$env:VERITAS_AUTOSURVEY_OPENAI_MODEL="gpt-5-mini"
$env:OPENAI_API_KEY="..."
$env:VERITAS_MAX_DOCS="15"
$env:VERITAS_BATCH_SIZE="5"
$env:VERITAS_SCOUT_DOCS="3"
$env:VERITAS_AUTOSURVEY_FETCH_MAX_CHARS="100000"

# 1. Veritas smoke: 2 tasks
C:\Users\asdf\.conda\envs\agent\python.exe -m benchmarks.drb.veritas_runner `
  --drb-root vendor\deep_research_bench `
  --model-name veritas_autosurvey_gpt5mini_m15 `
  --max-docs 15 --scout-docs 3 --batch-size 5 --limit 2 --resume

# 2. Flat smoke: 2 tasks
C:\Users\asdf\.conda\envs\agent\python.exe -m benchmarks.drb.flat_runner `
  --drb-root vendor\deep_research_bench `
  --model-name flat_gpt5mini_web_m15 `
  --max-docs 15 --search-query-count 5 --fetch-max-chars 100000 --limit 2 --resume

# 3. Validate raw data
C:\Users\asdf\.conda\envs\agent\python.exe -m benchmarks.drb.validate_raw_data `
  vendor\deep_research_bench\data\test_data\raw_data\veritas_autosurvey_gpt5mini_m15.jsonl
C:\Users\asdf\.conda\envs\agent\python.exe -m benchmarks.drb.validate_raw_data `
  vendor\deep_research_bench\data\test_data\raw_data\flat_gpt5mini_web_m15.jsonl

# 4. Confirm generated artifacts are not staged

git status --short
```

Manual checks:

- Article language matches prompt language.
- Veritas export contains numeric `[n]` citations, not `[doc_NNN]` markers.
- `## References` contains actual URLs.
- Flat baseline did not exceed `max_docs` or `fetch_max_chars`.
- Sidecar metadata records elapsed time and source counts.
- `git status --short` does not show generated raw JSONL, `runs/drb`, or evaluator results.

Then run a stratified 10-task pilot. Do not blindly use the first 10 ids. Choose 5 Chinese + 5 English across multiple topics after inspecting the vendored `query.jsonl`.

Example:

```powershell
C:\Users\asdf\.conda\envs\agent\python.exe -m benchmarks.drb.veritas_runner `
  --drb-root vendor\deep_research_bench `
  --model-name veritas_autosurvey_gpt5mini_m15_pilot10 `
  --max-docs 15 `
  --task-ids 1,8,16,25,40,51,60,70,84,96 `
  --resume
```

Adjust ids after inspecting `vendor/deep_research_bench/data/prompt_data/query.jsonl`.

---

## Official DRB Evaluation Sequence

Only run this when the smoke/pilot outputs validate.

```powershell
# Set evaluator keys. Do not commit these.
$env:LLM_BACKEND="openai"
$env:OPENAI_API_KEY="..."
$env:JINA_API_KEY="..."
$env:RACE_MODEL="gpt-5.5"
$env:FACT_MODEL="gpt-5.4-mini"
```

If using Git Bash:

```bash
cd vendor/deep_research_bench
# Edit TARGET_MODELS locally or use a local ignored helper script.
bash run_benchmark.sh
```

After official results exist:

```powershell
C:\Users\asdf\.conda\envs\agent\python.exe -m benchmarks.drb.analyze_results `
  --drb-root vendor\deep_research_bench `
  --models veritas_autosurvey_gpt5mini_m15 flat_gpt5mini_web_m15 `
  --out-dir bench_results\drb\gpt5mini_m15
```

Do not commit:

```text
vendor/deep_research_bench/results/race/veritas_*/
vendor/deep_research_bench/results/fact/veritas_*/
vendor/deep_research_bench/results/race/flat_*/
vendor/deep_research_bench/results/fact/flat_*/
bench_results/drb/
```

If a curated benchmark result needs to be shared in git later, create a separate, intentionally reviewed directory such as:

```text
docs/benchmark_reports/drb/2026-06-<date>/comparison_report.md
```

and include only summarized tables, methodology, and reproducibility manifest. Do not commit raw fetched pages or full generated article corpora unless explicitly approved.

---

## Report Back From Claude

Claude must update `ARCHITECTURE.md` implementation log and report back with:

- changed files list
- whether DRB is full-vendored or a submodule
- upstream DRB commit or snapshot status from `VENDOR_INFO.md`
- `.gitignore` changes and `git check-ignore` results for representative generated paths
- exact runner commands added
- generated output paths
- JSONL schema and sidecar metadata schema
- citation conversion behavior, including unmapped citation handling
- confirmation that chat `AutoSurveyTool` is not used
- confirmation that flat baseline does not use AutoSurvey planner/cleanup/summarize/final tools
- tests run and results
- manual smoke/pilot results if run
- remaining limitations, especially:
  - official DRB evaluator not run unless API keys were available
  - full 100-task benchmark not run unless explicitly performed
  - per-task paired analysis unavailable if the vendored DRB checkout does not expose per-task result files
  - DRB evaluator dependencies may require a separate env if they conflict with Veritas runtime dependencies

---

## Codex Post-Implementation Review Focus

### Vendor safety checks

- [ ] `vendor/deep_research_bench/` exists and required DRB files are present.
- [ ] No unregistered nested `.git` directory exists under `vendor/deep_research_bench/`.
- [ ] `VENDOR_INFO.md` records upstream URL, commit/snapshot, vendor mode, and refresh procedure.
- [ ] Upstream `LICENSE` is preserved.
- [ ] `.gitignore` prevents generated raw outputs, evaluator outputs, `runs/drb`, and `bench_results/drb` from being committed.
- [ ] No API keys, `.env`, logs, or full fetched bodies are staged.
- [ ] `run_benchmark.sh` is not modified just to set local `TARGET_MODELS`, unless Codex explicitly approves a tracked evaluator wrapper.

### Required correctness checks

- [ ] `benchmarks/drb/veritas_runner.py` does not call `AutoSurveyTool`.
- [ ] Veritas runner uses direct AutoSurvey workflow wiring or a justified `AgentRuntime.run_autosurvey()` path without chat memory/context injection.
- [ ] Flat runner does not import/call `AutoSurveyWorkflow`, `QueryPlanTool`, `DocumentCleanupTool`, `DocumentSummarizeTool`, or `FinalReportTool`.
- [ ] Citation adapter does not mutate `final.md`.
- [ ] Official raw JSONL contains only `id`, `prompt`, and `article`.
- [ ] Sidecar metadata does not contain full raw web bodies or secrets.
- [ ] Unit tests do not perform network calls.

### Fairness checks

- [ ] Same model/provider can be configured for both runners.
- [ ] Same `max_docs` and `fetch_max_chars` are enforced.
- [ ] Flat baseline has web search and fetch primitives.
- [ ] Flat baseline is not secretly stronger by using Veritas cleanup/source-quality/replan.
- [ ] Both outputs use numeric citation styles suitable for DRB FACT extraction.
- [ ] Time/cost metadata is recorded for both systems.

### Maintainability checks

- [ ] Benchmark code is isolated under `benchmarks/drb/`.
- [ ] Prompt strings are in `core/prompts/drb_benchmark.py`.
- [ ] Helpers are small pure functions where possible.
- [ ] No broad abstractions or factories for a single benchmark path.
- [ ] No changes to production UI/API.
- [ ] `ARCHITECTURE.md` implementation log is concise and diff-based.

---

## Suggested `ARCHITECTURE.md` Implementation Log Entry

Claude should append a concise entry similar to this after implementation:

```markdown
### 2026-06-05 — Vendored DeepResearch Bench AutoSurvey Benchmark Harness

**기능**: Veritas repo 내부 `vendor/deep_research_bench/`에 DeepResearch Bench를 vendoring하고, DRB `query.jsonl`을 읽어 Veritas AutoSurvey와 flat LLM+web baseline의 DRB-compatible `raw_data/<model>.jsonl`을 생성하는 benchmark-only harness를 추가했다. 공식 RACE/FACT 결과는 vendored DRB evaluator로 실행하고, 결과는 paired delta report로 분석한다.

**변경 파일**
- (new) `vendor/deep_research_bench/` — official DRB source/data prompt snapshot, `VENDOR_INFO.md` 포함. Generated raw/eval outputs는 ignore.
- (edit) `.gitignore` — DRB generated outputs, `runs/drb/`, `bench_results/drb/` ignore.
- (new) `benchmarks/drb/drb_vendor.py` — vendored DRB root/path validation helpers.
- (new) `benchmarks/drb/drb_io.py` — robust JSON object loader, task filtering, official raw JSONL writer, resume helpers.
- (new) `benchmarks/drb/citation_adapter.py` — Veritas `[doc_NNN]` citations를 numeric `[n]` + URL references로 export-only 변환.
- (new) `benchmarks/drb/veritas_runner.py` — direct AutoSurvey workflow runner; chat `AutoSurveyTool` 경로 미사용.
- (new) `core/prompts/drb_benchmark.py` — flat baseline query/report prompts.
- (new) `benchmarks/drb/flat_agent.py` / `flat_runner.py` — search/fetch primitive만 쓰는 flat baseline.
- (new) `benchmarks/drb/validate_raw_data.py` / `analyze_results.py` — DRB raw JSONL validation and paired comparison.
- (new/edit) `tests/test_drb_*.py` — vendor layout, IO, citation, baseline, analysis unit tests with fake dependencies.

**엔지니어링 결정**
- DRB는 sibling repo가 아니라 `vendor/deep_research_bench/`에 full-vendor snapshot으로 둔다. Upstream commit/snapshot은 `VENDOR_INFO.md`에 기록한다.
- benchmark harness는 production AutoSurvey pipeline을 수정하지 않고 `benchmarks/drb/`에 격리했다.
- Veritas export는 `final.md`를 변경하지 않고 DRB용 article string에서만 citation을 numeric URL citation으로 바꾼다.
- flat baseline은 같은 LLM·같은 search/fetch primitive·같은 document budget을 쓰지만 AutoSurvey의 planner/cleanup/summarize/replan/final tool은 쓰지 않는다.
- official DRB raw JSONL에는 `id`, `prompt`, `article`만 쓰고, duration/source counts/error details는 sidecar `.meta.jsonl`에 기록한다.
- generated benchmark artifacts는 `.gitignore`로 제외해 teammate는 같은 repo에서 재생성한다.

**테스트**
- `python -m unittest tests.test_drb_vendor_layout -v`
- `python -m unittest tests.test_drb_benchmark_io -v`
- `python -m unittest tests.test_drb_citation_adapter -v`
- `python -m unittest tests.test_drb_flat_baseline -v`
- `python -m unittest tests.test_drb_analysis -v`
```

---

## Non-Goals

- Do not run the full 100-task benchmark as part of implementation unless the user explicitly asks and API/network budget is available.
- Do not tune AutoSurvey quality based on DRB results in this increment.
- Do not add a Veritas GUI page for benchmark results in this increment.
- Do not submit to the DRB leaderboard in this increment.
- Do not create a public marketing claim until official RACE/FACT results have been generated and compared with paired analysis.
- Do not commit generated raw article JSONL, official evaluator outputs, or `bench_results/drb` by default.

---

## Later Follow-Up Tasks

After the first clean pairwise benchmark works:

- Add local-model pair: `flat_local_web_m15` vs `veritas_autosurvey_local_m15`.
- Add `m30` depth variant: `veritas_autosurvey_gpt5mini_m30` and `flat_gpt5mini_web_m30`.
- Add ablations:
  - `veritas_no_replan`
  - `veritas_no_source_quality`
  - `veritas_no_cleanup_structural_gate`
- Add cost model normalization:
  - LLM input/output token estimate
  - search calls
  - fetch calls
  - elapsed time
- Add a curated report release directory for team sharing:

```text
docs/benchmark_reports/drb/<date>/
  methodology.md
  comparison_report.md
  reproducibility_manifest.json
```

- Add a short generated claim pack for internal pitch:
  - “same model, same budget” table
  - RACE/FACT deltas
  - win rate
  - representative task examples where AutoSurvey wins/loses
