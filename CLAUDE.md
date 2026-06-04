# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

VERITAS is a **local-first AI research & writing assistant** (Windows desktop app). LLM inference, embeddings, retrieval, and storage all run on the user's PC via `llama-server`; only AutoSurvey web search leaves the machine. Four core features: **AutoSurvey** (autonomous web research → report), **RAG chat** (grounded on collected + local documents), **schema-driven tool chat** (the LLM decides which tool to call from prompts/schemas), and **Proactive assist** (observes the user writing and proposes ghostwrites/suggestions).

Source comments and most docs are in Korean. UI text is Korean.

## Read these first

The repo is already well-documented. Before deep work, read the doc closest to the task — don't rediscover what they already explain:

- **`ARCHITECTURE.md`** — the authoritative map: layers, threading model, data flows, directory responsibilities, "where do I change X" table, and the full Proactive subsystem spec (gates, telemetry formats, JSONL schemas, adding a task type).
- **`MEMORY_ARCHITECTURE.md`** — the chat memory layer (working/FIFO/recall/summary tiers, `memory.sqlite3`, budget, profiles, flush).
- **Per-directory `README.md`** — `tools/`, `services/`, `services/proactive/`, `api/`, `frontend/`, `llm/`, etc. each have one.
- **`README.md`** (root) — feature overview, CLI options, env vars, storage layout.

## Environment & commands

This project runs in the conda env **`agent`** (Python 3.13). The base env does **not** have the deps. Always use the agent env's interpreter:

```powershell
# interpreter
C:\Users\asdf\.conda\envs\agent\python.exe        # or: conda run -n agent python

# install deps
conda run -n agent python -m pip install -r requirements.txt

# run the full desktop app (does model setup, spawns 2 llama-servers + API + UI)
conda run -n agent python launcher.py
#   --console-logs      all child stdout to console
#   --screen-debug      only [screen_debug] lines (screen capture pipeline)
#   --proactive-debug   only [proactive] lines (proactive decisions)

# run the FastAPI backend alone (port 8000)
conda run -n agent python -m api --api --port 8000

# run the CLI pipeline (no GUI; requires a running chat+embed llama-server)
conda run -n agent python main.py "research topic" --output-dir ./output --phase all
#   --phase: all | plan | collect | summarize | final | rag | chat
```

Ports: chat LLM `8080`, embeddings `8081`, API `8000`. The **API process owns the llama-server lifecycle** (so a settings model-switch can restart it); the launcher sets `VERITAS_MANAGE_LLAMA=1` and waits on `/api/v1/health`.

`launcher.py` ties all children to a Windows Job Object with `KILL_ON_JOB_CLOSE`, so a hard kill of the launcher reaps the llama-servers too (no orphaned port-8080 process).

## Tests

`unittest` only (no pytest). Files `tests/test_<topic>.py`, classes `<Topic>Tests(unittest.TestCase)`. External deps (LLM, FastAPI) are mocked by **callable injection**, not patching.

```powershell
conda run -n agent python -m unittest discover -s tests           # all
conda run -n agent python -m unittest tests.test_proactive_evaluator -v   # one file
```

`tests/bench_*.py` are benchmarks, not unit tests. `tests/fixtures/` holds shared data.

## Architecture in one breath

Three entrypoints, one shared core:

```
[CLI] main.py        [Desktop] frontend/ ──HTTP──▶ api/ (FastAPI)
        └──────────────┬──────────────────────────────┘
   shared core:  agent/ (ChatAgent)  workflows/ (AutoSurveyWorkflow)  tools/  services/
   infra:        llm/  storage/ (ChromaDB)  db/ (SQLite)  core/ (prompts, models)
   state:        runs/<workspace>/  +  %LOCALAPPDATA%/VERITAS/veritas.db
```

- **`frontend/` never calls the core directly** — it is a PySide6 app that talks to `api/` over HTTP. `api/services/agent_runtime.py` holds the `AgentRuntime` **singleton** (current workspace's LLM, tool registry, workflow, chat agent, proactive orchestrator) guarded by `_workspace_lock`.
- Long-running FastAPI handlers are **plain `def`** (run on the threadpool), not `async def`, so they don't block the event loop.
- **Vocabulary:** *Tool* = one callable capability (`tools/<name>/` with `tool_schema.json` + a `BaseTool`); *Workflow* = a deterministic pipeline of tools; *Service* = owns state/logic; *Agent* = the chat loop bridging the LLM and the tool registry.

## Cross-cutting invariants (these are enforced or load-bearing — don't break them)

1. **No keyword/regex routing for tool selection.** Chat exposes a stage allowlist of schemas and lets the LLM decide. Code only enforces resource caps, tool boundaries, persistence, and deterministic workflow steps. (Exception: explicit `/autosurvey` and `/rag` slash commands and the `site:` source constraint are intentional deterministic bypasses.)
2. **All LLM prompt copy lives in `core/prompts/`** (`autosurvey.py`, `chat.py`, `cleanup.py`, `draft.py`, `editor.py`, `verify.py`, `proactive.py`, `memory.py`, `gateway.py`). Never inline prompt strings in domain/generator code.
3. **Local documents (`local_private`) never go to an external API** — blocked at the code level. Any task whose evidence includes local docs runs on the local LLM only, even when OpenAI acceleration is on.
4. **OpenAI is optional and AutoSurvey-only** (term grounding, query plan, doc summary, final report). Chat, RAG, embeddings, verification, and local-corpus processing are always local. See `llm/autosurvey_llm_factory.py` and `VERITAS_AUTOSURVEY_LLM_PROVIDER`.
5. **Proactive guard rails** (each has a regression test that fails on violation — see `ARCHITECTURE.md` §Proactive):
   - No hard-coded vocabulary-keyword features (no `"근거"/"출처"` word lists) → `tests/test_proactive_features.py::NoKeywordModulesTests`.
   - Never import `services/proactive/legacy_bandit/` in production → `tests/test_proactive_api.py`.
   - No raw document text in proactive JSONL/JSON (char counts + anchor hashes only; `raw_text_saved` must stay `false`) → `tests/test_proactive_api.py`.
6. **Verification layer** derives signals from artifact text + algorithms (BM25/RRF/embeddings/graph), not baked-in keyword dictionaries or domain assumptions.

## Common change recipes

- **Add a tool:** `tools/<name>/tool_schema.json` + `BaseTool` impl → export in `tools/<name>/__init__.py` → register in `tools/loader.py` → add to a stage allowlist only if that stage should expose it. (Procedure detail in `tools/README.md`.)
- **Change the research pipeline:** `workflows/autosurvey_workflow.py` (term_grounding → query_plan → collect → summarize → gap/replan loop → final_report). Steps emit `progress_callback` events consumed by the API ring buffer.
- **Add/modify an API endpoint:** router in `api/api_routes/<feature>.py` + logic in `api/services/<feature>_service.py`.
- **Add/modify a desktop screen:** `frontend/ui/pages/` + an HTTP call in `frontend/controllers/agent_controller.py`.
- **Change verification or cross-check:** algorithms in `services/verification/` (sections · reliability · consensus · crosscheck); API shaping in `api/services/verify_view.py`; UI in `frontend/ui/pages/verify_page.py`.
- **Add a Proactive task type:** follow the exact 9-step walkthrough in `ARCHITECTURE.md` (`proposal_models.TaskType` → `core/prompts/proactive.py` lead-ins → `candidates._maybe_*` → `evaluator` branches → `generator` action → tests).

## Where state lives

| Store | Location | Contents |
|---|---|---|
| Workspace artifacts | `runs/<workspace>/` (or `--output-dir`) | raw HTML/markdown, doc/batch summaries, plan/grounding/index JSON, `final.md` |
| Vector index | `runs/<workspace>/chromadb/` | RAG embeddings (web + local docs) |
| Local corpus | `runs/<workspace>/local/`, `knowledge/` | manifest, extracted text, table profiles, `sources.json` |
| Verification | `runs/<workspace>/verification/` | sections/reliability/consensus + `crosscheck.json` |
| Proactive adaptation | `runs/<workspace>/proactive_policy/` | `user_adaptation.json`, append-only `*.jsonl` (no raw text), `proactive.log` |
| Chat memory | `runs/<workspace>/memory/memory.sqlite3` | working/FIFO/recall/summary tiers + `invocations.jsonl` |
| App metadata | `%LOCALAPPDATA%/VERITAS/veritas.db` | workspace list, documents, activity log, `app_state` |
| Models, logs | `%LOCALAPPDATA%/VERITAS/{models,logs}/` | GGUF files, child-process logs |

A **workspace = one folder under `runs/`**. `db/workspace_sync.py` reconciles the `runs/` disk folders with SQLite rows at boot.
