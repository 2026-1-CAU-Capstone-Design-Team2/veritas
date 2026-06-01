# AutoSurvey OpenAI API Migration Notes

Last reviewed: 2026-06-01

This document captures the current Veritas AutoSurvey LLM wiring and the
recommended implementation plan for using OpenAI API models only inside the
AutoSurvey research workflow. It is written so another Codex session can pick
up the implementation without re-discovering the same architecture.

## Goal

Use an OpenAI API model for the AutoSurvey research pipeline. The current
development-test default is `gpt-5-mini`; use `gpt-5.4-mini` explicitly when a
higher-quality benchmark baseline is more important than repeated-run cost:

```text
term grounding -> query plan -> web fetch -> document cleanup
-> batch summary -> gap extraction -> replan loop -> final report
```

Keep the rest of Veritas on the local llama-server LLM and local embedding
server:

- Chat / RAG answers after the survey should keep using the local LLM unless a
  future change explicitly opts them into OpenAI.
- Embeddings and Chroma/RAG indexing should remain local.
- Verification flows should remain local unless explicitly changed later.

## Current Code Findings

### The current LLM client is OpenAI-compatible, not OpenAI-hosted

`llm/llama_server_llm.py` uses the OpenAI Python SDK, but it is configured for
the local llama-server endpoint:

- `LLMClient.__init__` sets `self.chat_base_url = f"http://{host}:{port}/v1"`.
- It constructs `OpenAI(base_url=self.chat_base_url, api_key="sk-no-key-required")`.
- The API key is a dummy value required by the SDK; it is not a real OpenAI API
  key input.
- Model detection calls `self.client.models.list()` against the local server.
- Context detection calls llama-server `/props`; OpenAI does not provide that
  endpoint.

Do not solve this by only replacing `"sk-no-key-required"` with a real key.
That would still target `127.0.0.1:8080/v1`, and even if `base_url` is changed,
the request body still contains llama-server/Qwen-specific fields.

### llama-server-specific request fields must not be sent to OpenAI

The current local client adds fields that are appropriate for llama-server but
not for the official OpenAI API:

- `extra_body.top_k`
- `extra_body.min_p`
- `extra_body.repeat_penalty`
- `extra_body.enable_thinking`
- `extra_body.enable_reasoning`
- `extra_body.chat_template_kwargs`
- `/think` and `/no_think` prefixes in the user prompt

An OpenAI-backed client should implement the same high-level methods, but build
a clean OpenAI request.

### AutoSurvey gets its LLM through tool registry injection

`workflows/autosurvey_workflow.py` does not instantiate an LLM. It calls tools
through `self.registry.get(...)`:

- `term_grounding`
- `query_plan`
- `document_cleanup`
- `document_summarize`
- `final_report`

Those tools receive their LLM from `tools/loader.py::build_registry(llm=...)`.

This is good news: the workflow orchestration itself does not need a large
rewrite. The change should happen at the LLM adapter and registry wiring layer.

### `build_registry()` currently uses one LLM for too many roles

`tools/loader.py::build_registry()` passes the same `llm` object into:

- AutoSurvey generation tools:
  - `TermGroundingTool`
  - `QueryPlanTool`
  - `DocumentCleanupTool`
  - `DocumentSummarizeTool`
  - `FinalReportTool`
- `RAGService(llm=llm)`, which needs embeddings.
- `ScreenContextService(... llm=llm)`, when enabled.
- `VerifyFlowPlannerTool(llm=llm)`.

If an OpenAI chat-only client is passed as this single `llm`, RAG indexing and
embedding calls can break or accidentally move to OpenAI. The registry builder
needs separate roles.

Recommended split:

```python
def build_registry(
    llm,                    # default/local general LLM
    run_root,
    *,
    autosurvey_llm=None,     # optional GPT client for research generation
    embedding_llm=None,      # optional, defaults to local llm
    ...
):
    research_llm = autosurvey_llm or llm
    dense_llm = embedding_llm or llm
```

Then use:

- `research_llm` for AutoSurvey tools.
- `dense_llm` for `RAGService`.
- `llm` for chat/screen/verify defaults unless a later feature changes that.

## Recommended Implementation Plan

### 1. Add an OpenAI chat LLM adapter

Create a new file, for example:

```text
llm/openai_chat_llm.py
```

It should expose an interface compatible with the subset used by AutoSurvey:

- `ask(...) -> str`
- `ask_json(...) -> dict[str, Any]`
- `map_parallel(...) -> list[Any]`
- `n_ctx`
- `model`
- `stream_summary`
- `trace_latency`
- `max_parallel`

For initial AutoSurvey support, it does not need to implement embeddings.
If `embed()` or `embed_batch()` is called on this OpenAI chat client, fail
clearly with a message that embeddings should use the local embedding client.

Suggested constructor:

```python
class OpenAIChatLLMClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5-mini",
        n_ctx: int = 400_000,
        max_parallel: int = 2,
        trace_latency: bool = True,
        stream_summary: bool = False,
    ) -> None:
        ...
```

Use `OpenAI(api_key=api_key)` with the SDK default base URL. Do not set
llama-server `extra_body` fields.

`ask()` should:

- Convert `system_prompt` and `user_prompt` to chat messages.
- Ignore `/think` and `/no_think`; do not add them.
- Respect `timeout_sec` by passing an SDK timeout when available.
- If `tools` are passed, either:
  - implement the same tool-call loop as `LLMClient`, or
  - for the first migration, note that AutoSurvey currently sets
    `QueryPlanTool.LLM_EXPOSED_TOOL_NAMES = ()`, so tool calls are not needed
    for the active research path.

`ask_json()` should:

- Prefer a JSON/object response format if supported by the selected API method.
- Reuse or port `_extract_json()` behavior from `LLMClient` so JSON parsing
  remains tolerant of markdown fences and extra text.
- Keep retry behavior compatible with current `LLMClient.ask_json()`.

`map_parallel()` can be copied from `LLMClient`; it is provider-independent.

### 2. Configure the OpenAI client from environment variables

Use environment variables first; settings UI can be added later.

Suggested variables:

```text
VERITAS_AUTOSURVEY_LLM_PROVIDER=local|openai
OPENAI_API_KEY=...
VERITAS_AUTOSURVEY_OPENAI_MODEL=gpt-5-mini
VERITAS_AUTOSURVEY_OPENAI_MAX_PARALLEL=2
VERITAS_AUTOSURVEY_OPENAI_N_CTX=400000
VERITAS_AUTOSURVEY_OPENAI_SERVICE_TIER=auto|default|flex|priority
```

Do not store API keys in repository files. Do not write keys into run outputs,
logs, progress events, or frontend state.

For `gpt-5.5`, use `n_ctx=1050000`. For `gpt-5-mini` and `gpt-5.4-mini`, use
`n_ctx=400000`. These values should be treated as configurable because model
specs can change.

### 3. Add a small factory

Create a provider factory, for example:

```text
llm/autosurvey_llm_factory.py
```

Suggested behavior:

```python
def build_autosurvey_llm(default_llm):
    provider = os.getenv("VERITAS_AUTOSURVEY_LLM_PROVIDER", "local").lower()
    if provider != "openai":
        return default_llm

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "VERITAS_AUTOSURVEY_LLM_PROVIDER=openai requires OPENAI_API_KEY"
        )

    return OpenAIChatLLMClient(
        api_key=api_key,
        model=os.getenv("VERITAS_AUTOSURVEY_OPENAI_MODEL", "gpt-5-mini"),
        n_ctx=int(os.getenv("VERITAS_AUTOSURVEY_OPENAI_N_CTX", "400000")),
        max_parallel=int(os.getenv("VERITAS_AUTOSURVEY_OPENAI_MAX_PARALLEL", "2")),
        service_tier=os.getenv("VERITAS_AUTOSURVEY_OPENAI_SERVICE_TIER", ""),
        trace_latency=os.getenv("VERITAS_TRACE_LATENCY", "1") != "0",
    )
```

### 4. Split registry LLM roles

Update `tools/loader.py::build_registry()` so it accepts optional role-specific
clients.

Recommended function signature:

```python
def build_registry(
    llm,
    run_root: str | Path,
    *,
    autosurvey_llm=None,
    embedding_llm=None,
    ...
):
```

Use `autosurvey_llm or llm` for:

- `TermGroundingTool`
- `QueryPlanTool`
- `DocumentCleanupTool`
- `DocumentSummarizeTool`
- `FinalReportTool`

Use `embedding_llm or llm` for:

- `RAGService`

Keep local `llm` for:

- `VerifyFlowPlannerTool`
- `ScreenContextService`

This keeps AutoSurvey GPT-only and prevents OpenAI from being used after the
survey unless the code explicitly asks for it.

### 5. Wire API research runs to use the AutoSurvey client

In `api/services/agent_runtime.py`:

- Keep `self.llm = LLMClient(...)` as the local runtime LLM.
- In the workspace runtime setup (`_configure_workspace_runtime`), continue
  using local LLM unless there is a reason to use OpenAI there.
- In `run_autosurvey(...)`, build an `autosurvey_llm` via the new factory and
  pass it into `build_registry(...)`.

Target area:

```python
registry, run_store_service, rag_service = build_registry(
    llm=self.llm,
    run_root=workspace_dir,
    autosurvey_llm=autosurvey_llm,
    embedding_llm=self.llm,
    ...
)
```

Important: `_grounding_workspace_from_request()` currently uses `self.llm`
through `workspace_paths.extract_workspace_name_from_request(...)` before the
new workspace registry is built. If the requirement is "AutoSurvey only uses
OpenAI," decide whether workspace-name grounding is part of AutoSurvey. It
probably is. If so, pass `autosurvey_llm` there too, or accept that workspace
name extraction remains local.

### 6. Wire CLI runs if needed

`main.py` currently constructs only one `LLMClient` and passes it into
`build_registry()`.

For CLI support, add the same factory and pass:

```python
autosurvey_llm = build_autosurvey_llm(llm)
registry, run_store_service, rag_service = build_registry(
    llm=llm,
    autosurvey_llm=autosurvey_llm,
    embedding_llm=llm,
    ...
)
```

### 7. Make fetch size configurable before raising limits

The current 25,000 character cap is hardcoded at:

```text
workflows/autosurvey_workflow.py::_fetch_one()
registry.get("fetch_webpage").run(..., max_chars=25000)
```

The fetch tool also defaults to 25,000:

```text
tools/fetch_webpage_tool/fetch_webpage_tool.py::FetchWebpageTool.run()
```

Do not remove the cap entirely. Make it configurable:

```text
VERITAS_FETCH_MAX_CHARS=25000
VERITAS_AUTOSURVEY_FETCH_MAX_CHARS=100000
```

Recommended defaults:

- local llama-server: keep `25_000`.
- `gpt-5-mini`: start with `100_000` for development tests.
- `gpt-5.4-mini`: start with `100_000` for higher-quality baselines.
- `gpt-5.5`: start with `100_000` or `200_000`.

Why not unlimited:

- Larger context windows do not make every long page worth sending whole.
- Long-context pricing can be higher for some models/request sizes.
- Web pages often include nav, footer, related links, cookie notices, and
  repeated boilerplate.
- The current cleanup step is one call per document; very large inputs multiply
  cost quickly across 15+ documents.

### 8. Image support is a separate multimodal feature

OpenAI models such as `gpt-5.4-mini` and `gpt-5.5` support image input, but the
current Veritas fetch path does not pass image pixels to the LLM.

Current fetch path:

- `services/fetch_webpage_tool_funcs/crawl4ai_fetch.py`
- HTTP-only Crawl4AI strategy.
- Converts HTML to Markdown text.
- Stores raw HTML for provenance.
- Does not run a browser.
- Does not download page images.
- Does not attach image inputs to LLM calls.

To actually use page images:

1. Extract image candidates from raw HTML or Markdown.
2. Resolve relative image URLs against the final page URL.
3. Filter tiny icons, logos, trackers, ads, and repeated decorative images.
4. Download selected images or pass accessible URLs to the OpenAI request.
5. Add a multimodal document-analysis step that summarizes relevant images.
6. Persist image notes next to the document, e.g. in `clean_md` or a structured
   sidecar under `summary/`.
7. Ensure image-derived claims include the source `doc_id` and image URL.

Recommended first version:

- Limit to 1-3 images per document.
- Prefer images with large dimensions if dimensions are available.
- Prefer images near the main article body.
- Prefer images with meaningful `alt`, `title`, or surrounding caption text.
- Skip SVG icons and very small images.
- Put image observations into the document text before batch summary, with a
  section like:

```markdown
## Image Observations
- Image: <url>
  Observation: ...
```

Do not assume this is solved by switching models. Model support is necessary,
but the pipeline must still deliver image inputs.

### 9. Keep the final report upgrade optional

Cost-effective model policy:

- Default AutoSurvey generation for development tests: `gpt-5-mini`.
- Higher-quality benchmark baseline: `gpt-5.4-mini`.
- Optional high-quality final report only: `gpt-5.4` or `gpt-5.5`.

This can be added later by giving `FinalReportTool` a separate `final_llm`, or
by adding a second OpenAI client only for that tool in `build_registry()`.

For the first implementation, keep one `autosurvey_llm` for all AutoSurvey
tools to avoid overcomplicating the migration.

## Expected Cost Shape

Always re-check official pricing before implementation or demo. Pricing and
model availability can change.

Official references:

- Pricing: https://developers.openai.com/api/docs/pricing
- `gpt-5-mini`: https://developers.openai.com/api/docs/models/gpt-5-mini
- `gpt-5.4-mini`: https://developers.openai.com/api/docs/models/gpt-5.4-mini
- `gpt-5.4-nano`: https://developers.openai.com/api/docs/models/gpt-5.4-nano
- `gpt-5.5`: https://developers.openai.com/api/docs/models/gpt-5.5
- Image input guide: https://developers.openai.com/api/docs/guides/images-vision
- Prepaid billing: https://help.openai.com/en/articles/8264778
- API billing is separate from ChatGPT: https://help.openai.com/en/articles/8156019

As reviewed on 2026-06-01:

- `gpt-5-mini`: 400K context, text+image input, text output. It is cheaper
  than `gpt-5.4-mini` and fits the current input-heavy development-test
  workload well enough to catch pipeline and prompt regressions.
- `gpt-5.4-mini`: 400K context, text+image input, text output.
- `gpt-5.5`: 1,050K context, text+image input, text output.
- `gpt-5.4-mini` remains the better quality baseline for benchmark reporting.
- `gpt-5.4-nano` is cheaper than `gpt-5-mini`, but the quality risk is higher
  for planning and final synthesis while the input-price savings are modest
  for this pipeline.

For the current 15-document workflow:

- Typical LLM calls: about 22-30 per completed survey before retries.
- Dominant calls:
  - 15 document cleanup calls.
  - 4 batch summary calls.
  - several planning/replanning calls.
  - 1 final report call.
- Current code no longer runs final per-document LLM summaries at the end;
  cleanup writes document metadata and batch summaries feed final synthesis.
- With `VERITAS_AUTOSURVEY_FETCH_MAX_CHARS=100000`, one fetched page can be
  roughly 40K input tokens under the code's conservative 2.5 chars/token
  heuristic. The same document is typically consumed once by cleanup and once
  again inside a batch-summary prompt.

Rough live-test budgeting:

- Mock/unit tests: no API spend.
- 3-document smoke test on `gpt-5-mini`: about USD 0.10-0.50 reserve.
- 15-document demo on `gpt-5-mini`: about USD 0.50-2.00 reserve depending on
  fetch cap, output verbosity, retries, and page length.
- The same 15-document demo on `gpt-5.4-mini` is roughly 3x higher on input
  tokens and over 2x higher on output tokens.
- Development session with repeated runs: USD 10 is a reasonable starting
  credit for `gpt-5-mini`; use a higher cap before benchmark sweeps.

These are estimates, not guarantees. Add telemetry before doing many runs.

Latency policy:

- Keep `llmParallel` at 5 for OpenAI smoke tests when rate limits allow it;
  document cleanup is independent per document, so this improves wall time
  without changing prompts or model quality.
- For faster runs with the same model and prompts, set
  `VERITAS_AUTOSURVEY_OPENAI_SERVICE_TIER=priority`. This can reduce API
  latency but uses priority pricing when the project supports that tier.
  If the API rejects the requested tier for a model/project, the adapter retries
  once with the default project tier so the run can continue.
- Do not reduce `fetch_max_chars`, switch to nano, or lower reasoning/summary
  budgets when the goal is "no quality loss"; those are cost/latency tradeoffs,
  not quality-preserving optimizations.

## Telemetry and Safety Controls

Add explicit logging for each OpenAI call:

- model
- stage label
- input token count if returned by API
- output token count if returned by API
- elapsed time
- estimated cost if pricing is configured

Never log:

- API key
- Authorization header
- full prompts by default, because fetched documents may contain sensitive text

Recommended controls:

```text
VERITAS_AUTOSURVEY_OPENAI_DAILY_USD_LIMIT=...
VERITAS_AUTOSURVEY_OPENAI_RUN_USD_LIMIT=...
VERITAS_AUTOSURVEY_OPENAI_MAX_PARALLEL=2
VERITAS_AUTOSURVEY_OPENAI_SERVICE_TIER=auto
VERITAS_AUTOSURVEY_FETCH_MAX_CHARS=100000
VERITAS_AUTOSURVEY_MAX_IMAGES_PER_DOC=2
```

If exact cost limiting is hard in the first patch, at least implement:

- max parallel calls
- max fetch chars
- max images per document
- clear console warnings when provider is OpenAI

## Testing Plan

### Unit tests

Use fake clients; do not hit OpenAI in unit tests.

Test targets:

- `OpenAIChatLLMClient.ask_json()` extracts JSON from:
  - strict JSON
  - fenced JSON
  - extra explanatory text around JSON
- `OpenAIChatLLMClient.map_parallel()` preserves order and propagates the first
  input-order exception.
- `build_registry()` injects:
  - `autosurvey_llm` into AutoSurvey tools.
  - local `llm` into RAG/embedding service.
- `build_autosurvey_llm(default_llm)` returns default local client when provider
  is not `openai`.
- Missing `OPENAI_API_KEY` fails clearly when provider is `openai`.

### Integration tests without network

Use a fake OpenAI-compatible client object that records calls and returns
deterministic outputs.

Suggested assertions:

- `run_all(max_docs=...)` uses the fake autosurvey client for term grounding,
  planning, cleanup, batch summary, and final report.
- RAG indexing still calls local embedding client.
- No OpenAI client method is called after the AutoSurvey run for chat/RAG unless
  the test explicitly routes it there.

### Manual smoke test

Environment:

```powershell
$env:VERITAS_AUTOSURVEY_LLM_PROVIDER="openai"
$env:OPENAI_API_KEY="..."
$env:VERITAS_AUTOSURVEY_OPENAI_MODEL="gpt-5-mini"
$env:VERITAS_AUTOSURVEY_OPENAI_MAX_PARALLEL="5"
$env:VERITAS_AUTOSURVEY_OPENAI_SERVICE_TIER="priority"
$env:VERITAS_AUTOSURVEY_FETCH_MAX_CHARS="100000"
```

Run a small 3-document survey first.

Check:

- final report exists
- `summary/index.json` has expected document records
- `clean_md/*.md` exists
- `chromadb` indexing still succeeds locally
- chat after survey does not use OpenAI unless explicitly configured
- logs show OpenAI only for AutoSurvey stages

Then run a 15-document demo.

## Known Risks

- OpenAI API model availability and pricing can change; verify docs near the
  implementation date.
- Some OpenAI API models may have different rate limits by project/tier.
- A large fetch cap can quickly increase cost if many pages are long.
- Image support requires real pipeline changes; model switching alone is not
  enough.
- Current Crawl4AI fetch is HTTP-only. It will not capture JS-rendered DOM,
  lazy-loaded images that require browser execution, canvas charts, or content
  behind login/paywalls.
- If an OpenAI client is accidentally passed to `RAGService`, embeddings can
  fail or become unexpectedly billed. Keep role separation explicit.
- If `n_ctx` is left at the local fallback value, GPT models will still behave
  like an 8K model in `DocumentSummarizeTool` budgeting.

## Minimal Patch Checklist

- [ ] Add `llm/openai_chat_llm.py`.
- [ ] Add `llm/autosurvey_llm_factory.py`.
- [ ] Update `tools/loader.py::build_registry()` to accept `autosurvey_llm` and
      `embedding_llm`.
- [ ] Update `api/services/agent_runtime.py::run_autosurvey()` to build and pass
      the AutoSurvey LLM.
- [ ] Decide whether workspace-name grounding uses the AutoSurvey LLM; if yes,
      update `_grounding_workspace_from_request()`.
- [ ] Update `main.py` for CLI parity.
- [ ] Make AutoSurvey fetch max chars configurable.
- [ ] Add tests for adapter JSON parsing and registry role injection.
- [ ] Run a local-provider regression test.
- [ ] Run a 3-document OpenAI smoke test.
- [ ] Run a 15-document OpenAI demo.
