# Veritas

Veritas is a local research assistant that combines an AutoSurvey workflow, local
RAG over generated markdown outputs, and schema-driven chat tool use.

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

services/
  rag_service.py: indexing, retrieval, document-grounded answers
  run_store_tool_funcs/: output/state persistence

storage/
  vector_store.py: ChromaDB vector store wrapper
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
4. Fetch and summarize reference-site URLs directly when possible.
5. Build the initial query plan from the request, grounded terms, and reference sites.
6. Add site-scoped search queries for each reference site.
7. Run a scout collection cycle.
8. Summarize scout documents.
9. Replan if summaries reveal relevant gaps.
10. Continue collect -> summarize -> replan until max_docs or no queries remain.
11. Write the final report.
```

Internal AutoSurvey tools:

```text
term_grounding      LLM extracts important literal terms only.
query_plan          LLM builds search queries and coverage points.
web_search          Searches the web for planned queries.
fetch_webpage       Fetches and preprocesses web pages.
document_summarize  Summarizes fetched documents and batches.
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

`fetch_webpage_tool.py` uses the stable requests + BeautifulSoup extraction path only.
Browser-based crawler integration has been removed to avoid Playwright / asyncio
subprocess cleanup failures on Windows and to keep the fetch pipeline easy to debug.

```text
1. DuckDuckGo returns result URLs.
2. fetch_webpage fetches each URL with requests.
3. BeautifulSoup removes noise tags and selects the likely main content node.
4. Cleaned HTML is stored under corpus/raw_html/<doc_id>.html.
5. Extracted plain text is stored under corpus/raw_text/<doc_id>.txt.
6. document_summarize reads the plain text file directly.
```

Storage layout:

```text
corpus/raw_html/<doc_id>.html
corpus/raw_text/<doc_id>.txt
summary/doc_<n>.md
```

Fetched text is sanitized before writing, and file reads use UTF-8 with replacement
so malformed page encodings do not stop summarization.

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
- If AutoSurvey summaries exist and the markdown root is `--output-dir`, those
  summaries are indexed.
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
