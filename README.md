# Veritas

Veritas is a local research assistant that combines an AutoSurvey workflow, local
RAG over generated markdown outputs, and schema-driven chat tool use.

## 2026-05 UI/API Integration Notes

- API/CLI entrypoints now use separate OpenAI-compatible servers by default:
  chat completions on `127.0.0.1:8080`, embeddings on `127.0.0.1:8081`.
- `launcher.py` is the future `veritas-launcher.exe` entrypoint. On first run it
  checks `%LOCALAPPDATA%\VERITAS\models\llm` and
  `%LOCALAPPDATA%\VERITAS\models\embedding`, offers the Qwen3.5 GGUF choices,
  downloads missing models from Hugging Face with progress, then starts the two
  `llama-server` processes, the FastAPI server, and the PySide UI.
  The embedding server is started with `--embeddings`; without that flag
  `POST /api/v1/verify/jobs` can fail when the verification pipeline first
  calls `/v1/embeddings`.
  By default child-process logs are written to `%LOCALAPPDATA%\VERITAS\logs`.
  For development, run `python launcher.py --console-logs` or set
  `VERITAS_LOG_MODE=console` to stream llama-server/API/UI logs into the same
  terminal. If a server is already running on ports 8000/8080/8081, the
  launcher reuses it and cannot attach to that old process's stdout; stop the
  existing process first to see live logs from a fresh launch.
- The frontend Research page runs `/api/v1/research/jobs` on a Qt worker thread
  so the UI stays responsive during long AutoSurvey runs.
- Completed research jobs return document titles/links from `summary/index.json`,
  document counts, indexed chunk count, elapsed seconds, `finalPath`, and the
  final markdown report.
- The frontend Document page loads the latest AutoSurvey `final.md` through the
  documents API and renders it as markdown.
- Each AutoSurvey API run is stored as its own folder under `runs/`. The API
  performs a lightweight term-grounding pass first, then creates the workspace
  folder from the first `grounded_terms` string before ChromaDB opens any files.
- Runs subdirectories are treated as UI workspaces. The sidebar workspace
  dropdown refreshes from `/api/v1/workspaces`, which scans `runs/`.

### 2026-05-13 Proactive assistance + streaming UX

- Proactive screen-monitoring is now reachable from the API/UI. `AgentRuntime`
  exposes `start_screen_monitoring`, `stop_screen_monitoring`,
  `screen_monitoring_status`, and `get_screen_events_since`; these are wired to
  `POST /api/v1/screen-monitoring/start`, `POST /api/v1/screen-monitoring/stop`,
  `GET /api/v1/screen-monitoring/status`, and
  `GET /api/v1/screen-monitoring/events?since=<seq>&limit=<n>`.
- The proactive intervention loop is started automatically when the floating
  "AI 보조창" (DocumentAssistWindow) is shown and stopped when it is hidden.
  Generated screen-assist answers land in a runtime-side ring buffer; the
  frontend `ScreenEventPollWorker` (QThread) polls every ~3s and renders new
  answers as cards in the assist window's `SuggestionList`.
- Chat is fully asynchronous. The OpenAI streaming path is exposed via
  `LLMClient.iter_ask`, threaded through `ChatAgent.ask_auto_iter` /
  `ask_explicit_tool_iter` / `ask_rag_iter`, and surfaced over Server-Sent
  Events at `POST /api/v1/chat/messages/stream` and
  `POST /api/v1/document-assist/chat/messages/stream`. The events are
  `start` / `delta` / `done` / `error`.
- `frontend/controllers/chat_bus.py` introduces a `ChatBus` singleton plus a
  `ChatStreamWorker(QThread)`. Sending a chat message from any panel goes
  through the bus; the main thread returns immediately (no UI freeze) and the
  bus broadcasts `userMessageQueued`, `assistantStreamStarted`, `assistantChunk`,
  `assistantCompleted`, and `assistantFailed`.
- The main chat page (`WritePage`) and the floating assist window
  (`DocumentAssistWindow`) both subscribe to the bus and stay in sync — the
  same user/assistant bubbles appear in both views and chunk-by-chunk streaming
  updates happen simultaneously. Backend-side, `document_assist_service` was
  unified to route through `draft_chat_service`, so both panels share the same
  `chat_history.json` per workspace.
- AutoSurvey emits live progress events. `AutoSurveyWorkflow` accepts a
  `progress_callback` and emits at term grounding, query plan (initial/replan),
  per-query web search, per-URL fetch, batch summarize, and final report. The
  runtime keeps a `_research_progress` ring buffer exposed at
  `GET /api/v1/research/progress?since=<seq>&limit=<n>`. The Research page runs
  `ResearchProgressPoller(QThread)` and displays the latest single line in
  gray, replaced live as the agent acts (ChatGPT/Claude style).
- The Research result card was redesigned: a colored status pill (green
  `● 완료`, red `● 오류` with a click-to-open `QMessageBox`, blue `● 진행 중`),
  three info tiles (`작업 이름` / `저장 경로` / `수집된 문서 수`), and one
  `DocumentBar` per collected document. Each bar shows the title, a clickable
  URL hyperlink (uses `QDesktopServices.openUrl`, auto-prepends `https://`
  when the URL has no scheme), and a `doc_NNN.md ↗` button that opens the
  corresponding `summary/doc_<docId>.md` in the OS default viewer.
- AutoSurvey runs now publish the new workspace **immediately after
  term-grounding** instead of at completion. `AgentRuntime.run_autosurvey`
  calls a new `_publish_new_workspace` step right after
  `_reserve_workspace_dir`: it writes `summary/request.txt` (so the
  directory passes `_scan_run_workspaces`' "has any research evidence"
  filter even before the first document lands), upserts the workspace
  row + `current_workspace_id` into the in-memory catalog and the
  SQLite `app_state`, and emits a new `workspace_created` progress
  event with `{ workspaceId, name, path }`. The Research page picks up
  the event and updates the info tiles (작업 이름 / 저장 경로) plus
  emits a light `workspaceCreated` signal that `MainWindow` routes to
  `sidebar.set_current_workspace(name)` and, crucially, to clearing
  the chat panels — `WritePage.chat_panel.clear_messages()` and
  `DocumentAssistWindow.hydrate_history([])` — so the previous
  workspace's chat history doesn't bleed into the new workspace's
  context. This signal intentionally does *not* trigger the heavy
  `_on_workspace_changed` cascade so the live `DocumentBar` timeline
  in the Research page survives the workspace adoption.
- Workspace lifecycle is now consistent across the on-disk `runs/`
  directory and the local SQLite DB at `%LOCALAPPDATA%/VERITAS/veritas.db`.
  `db/workspace_sync.py` exposes two helpers:
  `reconcile_workspaces_with_disk(runs_root)` runs at both app launches
  (PySide `frontend/ui/main.py` and `AgentRuntime.__init__`) and prunes DB
  rows whose backing folder is gone — so workspaces a user manually
  removed from `runs/` while the app was offline no longer linger in the
  dashboard's "최근 작업". `delete_workspace(workspace_id, runs_root)`
  performs the user-initiated delete: it switches the runtime off the
  target workspace if it was active, removes `runs/<id>/`, and drops
  rows from `workspaces` / `documents` / `activity_logs` / `app_state`
  in one transaction. Demo seed rows (whose recorded `path` is outside
  `runs_root`) are deliberately preserved by both helpers.
- The dashboard "최근 작업 워크스페이스" panel now renders a red 삭제
  button on each row. Clicking it opens a confirmation popup
  ("{workspace_name} 워크스페이스가 삭제됩니다. 계속 하시겠습니까?" with
  예 / 아니오), and on confirmation calls
  `DELETE /api/v1/workspaces/{workspaceId}` which delegates to
  `db.workspace_sync.delete_workspace` and reloads the bootstrap state so
  the sidebar dropdown also drops the workspace.
- `AgentRuntime` no longer materializes a `runs/api/` directory when a real
  workspace already exists. At boot it scans `runs/` for the most-recently
  modified directory that contains real research evidence (a `final.md`, a
  `summary/index.json`, or any `doc_*.md`) and attaches to that workspace
  directly. The `runs/api/` slot is only used as a one-time scratch home
  for the very first session before any research has been produced; it is
  also removed on boot, and again whenever `set_workspace` transitions off
  of it, when it has no meaningful content. `set_workspace("default")` is
  resolved to the same most-recent workspace, so a stale "default" id
  coming from the frontend doesn't accidentally recreate the directory.
- The Research result card clears its in-memory `_doc_bars` map at the
  start of `_load_existing_result` so that switching workspaces does not
  reconcile workspace B's documents on top of workspace A's leftover bars.
  Bar identity is by `doc_id`, which is workspace-relative ("001" in
  workspace A is a different document from "001" in workspace B), so the
  reconciliation pass — which is correct for live updates within a single
  run — has to be reset across workspace boundaries.
- Frontend async dispatch and operation mutex go through a single
  `frontend/controllers/job_manager.py` `JobManager` singleton. Every
  long-running call to the backend (AutoSurvey, chat, draft, feedback
  analyze, workspace switch, ...) is tagged with a
  `JobCategory` constant and submitted via `JobManager.submit(category, fn,
  ...)` which runs `fn` on a worker `QThread` and emits `busy_changed` on
  state transitions. A central block matrix (`_BLOCKS_THIS` in `job_manager.py`)
  encodes which categories block which: e.g. `RESEARCH` blocks `CHAT`,
  `DRAFT`, `FEEDBACK`, `DOC_ANALYZE`, and `WORKSPACE_SWITCH`. Views connect
  to `JobManager.busy_changed` once and disable their inputs via
  `is_blocked(category)` — so while AutoSurvey is running the chat input
  bars (in `WritePage` and the floating `DocumentAssistWindow`), the
  workspace-switch button in the sidebar, the draft "초안 생성" button, and
  the feedback upload button are all greyed out automatically and cannot
  be invoked. The existing `ChatStreamWorker` reuses the same mutex via
  `JobManager.register/unregister(CHAT)`, so chat streams and JobManager-
  submitted operations participate in the same exclusion model. Adding a
  new heavy operation is now a single-place change: pick a `JobCategory`,
  call `submit(...)`, and the UI gating falls out automatically from
  `_BLOCKS_THIS`.
- FastAPI handlers that perform synchronous blocking work (`POST /research/jobs`,
  `POST /workspaces/switch`, `POST /chat/messages`, `POST /draft/generate`,
  `POST /draft/{id}/regenerate`, `POST /document-assist/analyze`,
  `POST /document-assist/chat/messages`, `POST /feedback/analyze`,
  `POST /screen-monitoring/{start,stop}`) are declared as plain `def`, not
  `async def`. FastAPI dispatches `def` handlers to its thread pool instead of
  running them on the event loop, so a long-running call (AutoSurvey, LLM
  inference, registry rebuild) cannot freeze every other request. Without
  this, the progress poller, workspace switch, and page-refresh calls all
  queued behind the in-flight research job and the UI appeared frozen.
- Collected-document bars stream in live as `AutoSurveyWorkflow` runs.
  `_fetch_one` emits a `doc_fetched` progress event after a non-duplicate
  record is committed, carrying `{doc_id, title, url, final_url, domain}`;
  `run_summarize` emits one `doc_summarized` event per successfully summarized
  doc with the absolute `summary_path`. The Research page (controller) owns a
  `_doc_bars: dict[doc_id → DocumentBar]` model: `doc_fetched` creates the bar
  in pending state (greyed-out "요약 대기 중" button) and `doc_summarized`
  flips it to ready via the single mutation method `DocumentBar.set_summary_ready`.
  The final job response is then reconciled in `_reconcile_documents` so any
  bars that polling missed are appended and any pending bars get their summary
  path filled in.
- The Document page now renders `final.md` through Python's `markdown` library
  (`tables` / `fenced_code` / `sane_lists` / `nl2br` extensions) and calls
  `QTextEdit.setHtml`, because Qt's built-in `setMarkdown` has known GFM-table
  rendering bugs (alignment row mis-parsed, tables breaking when adjacent to
  other blocks). `frontend/ui/markdown_view.py` falls back to `setMarkdown` if
  the optional `markdown` package is missing.

The project is built around one principle:

```text
Intent decisions belong to the LLM through prompts and tool schemas.
Code should enforce execution boundaries, not route by user-message keywords.
```

## Current Alignment Notes

This README has been updated to match the current codebase.

- `term_grounding_tool.py` is now LLM-only. It imports
  `TERM_GROUNDING_PROMPT` from `core/prompts.py` and sends only
  `user_request` plus `max_terms` to the LLM. The old regex, stopword,
  language-detection, heuristic term extraction, and fallback extraction logic
  has been removed.
- `term_grounding` no longer creates search queries. It only returns
  `request_language`, `grounded_terms`, `candidate_entities`, and
  `disambiguation_notes`.
- `agent/chat_agent.py` no longer filters chat tools with hard-coded regex or
  word lists before the LLM sees them. Chat mode exposes the stage allowlist and
  lets the LLM decide whether to call a tool from the prompt and schemas.
- `tools/llm_tooling.py` still supports an optional `expose_predicate` helper,
  but the current `ChatAgent` does not use it.
- Chat-triggered `autosurvey` is registered as a high-level adapter in
  `main.py` and is capped at 5 newly collected documents per invocation.
- In chat, `/autosurvey <request>` and `/rag <question>` bypass LLM tool
  selection and call the requested tool path explicitly.
- AutoSurvey recognizes explicit reference-site constraints such as
  `site:https://example.com` and forces those sites into the collection plan.
- Standalone `--phase plan` calls the planner directly. The full `--phase all`
  workflow is the path that runs term grounding before initial planning.

## Architecture

```text
main.py
  CLI parsing, LLM setup, registry setup, workflow wiring, chat entrypoints

agent/
  ChatAgent: multi-turn chat, chat history, schema-driven tool calls

core/
  prompts.py: system, grounding, planning, summarization, RAG, chat prompts
  models.py: shared records/models

llm/
  llama_server_llm.py: OpenAI-compatible llama-server client

workflows/
  autosurvey_workflow.py: deterministic AutoSurvey orchestration

tools/
  registry.py, tool.py, llm_tooling.py
  current_time_tool/
  rag_tool/
  autosurvey_tool/
  web_search_tool/
  fetch_webpage_tool/
  term_grounding_tool/
  query_plan_tool/
  document_summarize_tool/
  final_report_tool/
  screen_context_tool/

services/
  rag_service.py: indexing, retrieval, document-grounded answers
  run_store_tool_funcs/: output/state persistence
  screen_tool_funcs/: foreground-window OCR/UIA capture, intervention detector

storage/
  vector_store.py: ChromaDB vector store wrapper

api/
  api.py, main.py: FastAPI app + uvicorn entrypoint
  api_routes/: per-feature routers (research, chat, document-assist,
    documents, workspaces, write, feedback, screen-monitoring, ...)
  services/: agent_runtime (shared LLM/registry/chat agent), draft_chat,
    document_assist, research, screen_monitoring, ...

frontend/
  main.py, ui/: PySide6 desktop UI
  controllers/agent_controller.py: HTTP client wrapper
  controllers/chat_bus.py: ChatBus singleton + ChatStreamWorker(QThread)
  ui/markdown_view.py: markdown -> HTML renderer with table support
```

## Run Modes

`--output-dir` is required for every run.

```bash
# Full AutoSurvey, then schema-driven chat unless --no-rag is passed
python main.py "research topic" --output-dir ./output --phase all

# Full AutoSurvey with a required reference site
python main.py "research topic site:https://example.com" --output-dir ./output --phase all

# General chat with schema-driven tool use
python main.py --output-dir ./output --phase chat

# Strict document-grounded RAG chat
python main.py --output-dir ./output --phase rag

# Individual AutoSurvey phases
python main.py "research topic" --output-dir ./output --phase plan
python main.py --output-dir ./output --phase collect
python main.py --output-dir ./output --phase summarize
python main.py --output-dir ./output --phase final
```

If `--phase all` is used without an instruction but markdown files already exist
under `--output-dir`, Veritas enters schema-driven chat mode. Otherwise an
instruction is required.

## AutoSurvey Flow

The full workflow in `AutoSurveyWorkflow.run_all()` is:

```text
1. Save the user request and reset query state.
2. Run term_grounding.
3. Extract explicit `site:` reference-site constraints, if present.
4. Fetch and batch-summarize reference-site URLs directly when possible.
5. Build the initial query plan from the request, grounded terms, and reference sites.
6. Add site-scoped search queries for each reference site.
7. Run a scout collection cycle.
8. Batch-summarize scout documents.
9. Replan if batch summaries reveal relevant gaps.
10. Continue collect -> batch-summarize -> replan until max_docs or no queries remain.
11. Per-document summaries: summarize every collected clean_md once (after the loop).
12. Write the final report.
13. Index clean_md into ChromaDB for RAG.
```

Batch summary and per-document summary are independent consumers of each
document's clean Markdown (`clean_md/<doc_id>.md`), not a chain. Batch summary
runs inside the collect loop (it drives gap analysis / replan) and reads
clean_md directly. Per-document summaries (`summary/doc_*.md`) are UX
descriptors — source cards, citations, the verification view — so they do not
feed replan and are generated once after the loop instead of every cycle.

Internal AutoSurvey tools:

```text
term_grounding      LLM extracts important literal terms only.
query_plan          LLM builds search queries and coverage points.
web_search          Searches the web for planned queries.
fetch_webpage       Fetches and preprocesses web pages.
document_summarize  Builds per-document and batch summaries from clean_md.
final_report        Produces the final markdown report.
```

`query_plan` owns search-query generation. `term_grounding` only anchors the
planner with important terms.

## Reference Sites

If the user request contains one or more `site:` constraints, AutoSurvey treats
them as required reference sources:

```text
research topic site:https://example.com site:docs.python.org/3/
```

The workflow normalizes those constraints, fetches the reference URL directly
when possible, summarizes it, and injects site-scoped search queries such as:

```text
site:example.com research topic
site:docs.python.org/3 research topic
```

This is intentionally implemented in workflow code because it is an explicit
source constraint from the user, not an LLM intent guess.

## Web Search Provider

`web_search_tool.py` uses DuckDuckGo HTML search with the installed `ddgs`
package as a fallback. It does not require an API key, Docker, public instance
probing, or provider-specific configuration. The tool preserves the query
generated by the planner except for basic whitespace normalization. If
DuckDuckGo returns no parseable organic results, the tool reports a successful
empty result set instead of failing the collect phase.

Example:

```powershell
python main.py "research topic" --output-dir ./output --phase all
```

`site:` constraints are preserved in the query string. Support depends on how
DuckDuckGo handles the submitted search syntax.

## Web Fetching

`fetch_webpage_tool.py` uses Crawl4AI's HTTP-only crawler strategy
(`AsyncHTTPCrawlerStrategy`, backed by aiohttp — no Playwright browser) as its
*only* fetch path. There is no fallback extractor: a URL Crawl4AI cannot fetch
or extract is reported as a failure, so every document that *is* stored was
fetched by Crawl4AI and can be persisted directly as clean Markdown.

```text
1. DuckDuckGo returns result URLs.
2. fetch_webpage fetches each URL via Crawl4AI's HTTP-only strategy.
3. Crawl4AI (DefaultMarkdownGenerator + PruningContentFilter) converts the HTML
   to clean, structure-preserving Markdown and strips boilerplate.
4. The clean Markdown is stored under clean_md/<doc_id>.md.
5. The original HTTP response HTML is archived (untruncated) under
   corpus/raw_html/<doc_id>.html.
6. A URL that fails is skipped; the collect loop moves on to the next result.
```

Storage layout — document text artifacts are always Markdown:

```text
clean_md/<doc_id>.md            # Crawl4AI clean Markdown — RAG source + summary input
corpus/raw_html/<doc_id>.html   # original HTML archive (provenance only)
summary/doc_<n>.md              # per-document summary (UX descriptor)
summary/batch_<n>.md            # batch summary (gap analysis / final-report input)
```

Fetched text is sanitized before writing, and file reads use UTF-8 with replacement
so malformed page encodings do not stop summarization. `requests` /
`beautifulsoup4` are still used by `web_search_tool.py` for DuckDuckGo HTML
search, but no longer for page fetching.

## Term Grounding

`tools/term_grounding_tool/term_grounding_tool.py` now depends on the LLM. If no
LLM is available, it returns an error instead of falling back to rule-based term
extraction.

LLM input:

```json
{
  "user_request": "...",
  "max_terms": 8
}
```

Expected output:

```json
{
  "request_language": "ko",
  "grounded_terms": ["..."],
  "candidate_entities": ["..."],
  "disambiguation_notes": ["..."]
}
```

The prompt in `core/prompts.py` explicitly tells the model to decide
autonomously from the user request text and not rely on heuristic candidate
lists.

## Chat Tool Exposure

Chat mode exposes only these high-level tools to the LLM:

```text
current_time
rag_search
autosurvey
```

These tools are not directly exposed in chat:

```text
web_search
fetch_webpage
term_grounding
query_plan
document_summarize
final_report
```

The chat loop does not call `web_search` directly. Fresh research goes through
the `autosurvey` adapter, and already-indexed local evidence goes through
`rag_search`.

Explicit slash commands bypass LLM tool selection:

```text
/autosurvey <fresh research request>
/rag <question against indexed local documents>
```

The frontend chat mode selector uses the same forced paths: `자료조사` maps to
`/autosurvey`, and `RAG` maps to `/rag`.

Use these commands when you want deterministic tool selection and want to avoid
the LLM deciding whether a tool should be called.

## Chat Tool Responsibilities

### `current_time`

Returns the current local date/time or a requested timezone date/time.

### `rag_search`

Searches the indexed local corpus. It is a thin retrieval wrapper around
`RAGService`; strict RAG mode uses `RAGService.answer()` directly.

### `autosurvey`

Runs `AutoSurveyWorkflow` as one high-level chat tool. Chat-triggered surveys are
intentionally capped by newly collected documents per invocation:

```text
chat autosurvey new-doc cap = 5
CLI AutoSurvey default max_docs = 15
```

After a chat-triggered survey completes, the generated AutoSurvey summaries are
indexed into RAG when `rag_service` and `run_store_service` are available.

## Chat Turn Handling

`ChatAgent.ask_auto()` follows this sequence:

```text
1. Receive the current user message.
2. Expose the chat allowlist schemas to the LLM.
3. Let the LLM decide whether to call at most one tool by default.
4. Execute the selected tool through ToolRegistry.
5. Ask the LLM to synthesize a final answer from the current message and tool result.
6. Append exactly one (user, assistant) pair to chat history.
7. Mirror chat history into RAGService.chat_history.
```

Tool outputs are not dumped directly to the user unless the final-answer prompt
chooses to present them. The final answer is generated from the current turn;
recent history is context and should not override the current user message.

## RAG Indexing

Before `--phase rag` or `--phase chat`, `main.py` calls `ensure_rag_index()`.

- If `--markdown-root` is omitted, `--output-dir` is used.
- If AutoSurvey `clean_md/` documents exist and the markdown root is
  `--output-dir`, those clean Markdown documents are indexed. RAG answers are
  grounded in the clean source text, not the lossy per-document summaries.
- Otherwise markdown files under `--markdown-root` are indexed.
- `--reindex` clears and rebuilds the vector index.
- `--rag-results` controls `RAGService.n_results`.

In `--phase rag`, missing indexed documents stop the session. In `--phase chat`,
Veritas warns and continues because chat can still answer directly or use
non-RAG tools.

## CLI Options

| Option | Meaning | Default |
|---|---|---|
| `instruction` | Natural-language research request or question | optional |
| `--output-dir` | Root directory for outputs and persisted state | required |
| `--host` | llama-server host | `127.0.0.1` |
| `--port` | llama-server chat port | `8080` |
| `--embed-host` | optional embedding server host | chat host |
| `--embed-port` | optional embedding server port | chat port |
| `--parallel` | max concurrent LLM requests for batch work (per-doc cleanup/summary, embeddings); should match llama-server `-np` | `VERITAS_LLM_PARALLEL` or `1` (serial) |
| `--phase` | `all`, `plan`, `collect`, `summarize`, `final`, `rag`, `chat` | `all` |
| `--max-docs` | CLI AutoSurvey document cap | `15` |
| `--batch-size` | collection/summarization batch size | `5` |
| `--scout-docs` | scout-cycle document count | `3` |
| `--max-context` | summarization context budget | `16384` |
| `--rag-results` | RAG retrieval count | `5` |
| `--force-plan` | rebuild plan instead of reusing saved plan | false |
| `--overwrite-summaries` | overwrite existing summaries | false |
| `--stream-summary` | stream document summary calls | false |
| `--stream-reasoning` | stream reasoning content when supported | false |
| `--no-trace-latency` | disable LLM latency logs | false |
| `--markdown-root` | markdown directory to index for RAG | `--output-dir` |
| `--no-rag` | skip chat after `--phase all` completes | false |
| `--reindex` | rebuild the vector index | false |
| `--no-screen-context` | disable screen-context polling and proactive chat interventions | false |
| `--screen-interval` | seconds between foreground-window context captures in chat mode | `5.0` |
| `--screen-debug` / `--screen-debug-log` | print screen capture text previews, intervention decision checks, queue drops, and assist generation logs to CLI | false |

## Extension Rules

To add a new tool:

```text
1. Create tools/<tool_name>/tool_schema.json.
2. Implement BaseTool.
3. Export it from tools/<tool_name>/__init__.py.
4. Register it in tools/loader.py or a dedicated wiring point.
5. Add it to a stage allowlist only if that stage should expose it.
6. Describe usage conditions in the tool schema and prompts.
```

Avoid adding:

```text
- regex or keyword routers in chat_agent.py
- per-tool if/else routing based on words in the user message
- term extraction fallback logic in term_grounding_tool.py
- search-query construction inside term_grounding_tool.py
```

Use prompts and schema descriptions for LLM intent decisions. Use code for
resource caps, allowed tool boundaries, persistence, and deterministic workflow
steps.
