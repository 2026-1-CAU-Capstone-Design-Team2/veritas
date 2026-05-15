# Screen Monitoring API Fix Notes

## Summary

The existing backend CLI can act as a proactive document-writing assistant because it starts both parts of the screen assistance pipeline:

1. `screen_context` foreground-window polling
2. `ChatAgent` intervention-consumption loop that generates assistant responses

The current API/frontend path registers the `screen_context` tool, but it does not start the monitoring pipeline. As a result, frontend document-assist features only work from explicitly submitted text and do not behave like the old CLI screen-monitoring assistant.

## Primary Issue

### API runtime creates `ChatAgent` but never starts screen monitoring

Relevant files:

- `api/services/agent_runtime.py`
- `agent/chat_agent.py`
- `main.py`

CLI path:

- `main.py` builds the registry with `enable_screen_context=not args.no_screen_context`.
- `main.py` then enters `ChatAgent.chat_loop(...)`.
- `ChatAgent.chat_loop(...)` calls `start_screen_monitoring(...)` when `enable_screen_context=True` and `mode == "auto"`.
- `start_screen_monitoring(...)` starts:
  - `screen_context` tool action `start_polling`
  - the background `_screen_intervention_loop`

API path:

- `AgentRuntime.__init__()` builds the registry and may register `screen_context`.
- `AgentRuntime.__init__()` creates `self.chat_agent`.
- `AgentRuntime.answer_chat()` calls `ask_rag()` or `ask_auto()`.
- No API/runtime code calls `self.chat_agent.start_screen_monitoring(...)`.

Impact:

- `ScreenContextService.start_polling()` is not called.
- Foreground-window captures are not continuously created.
- Intervention candidates are not queued.
- `_screen_intervention_loop()` is not running, so even queued interventions would not be converted into assistant messages.

## Secondary Issue

### Frontend document-assist APIs bypass screen intervention flow

Relevant files:

- `api/services/document_assist_service.py`
- `api/services/write_service.py`
- `api/services/draft_chat_service.py`
- `frontend/controllers/agent_controller.py`

Current behavior:

- `document_assist_service.analyze_document(...)` receives explicit text from the frontend and sends it to `get_runtime().answer_chat(...)`.
- `document_assist_service.send_chat_message(...)` also sends explicit user text to `answer_chat(...)`.
- `write_service.prediction_event_stream(...)` uses stored typing context and sends a direct prompt to `answer_chat(...)`.
- None of these services consume `screen_context` pending interventions or subscribe to proactive screen-assist events.

Impact:

- The frontend document assistant is request/response only.
- It does not expose CLI-style automatic screen assist behavior.
- Screen monitoring and frontend document assist are currently separate mechanisms.

## Secondary Issue

### Mojibake in Korean prompts can degrade document-assist quality

Relevant files:

- `api/services/document_assist_service.py`
- `api/services/write_service.py`
- `api/services/draft_chat_service.py`

Examples:

- Strings such as `?ㅼ쓬 臾몄꽌瑜?...` appear in prompt construction.

Impact:

- This is not the direct cause of screen monitoring not starting.
- It can significantly degrade LLM output quality for document analysis, drafting, and typing prediction.

## Behavior Conditions To Preserve

The proactive screen intervention detector is intentionally gated. Even after monitoring is started, an intervention is only queued when all relevant checks pass.

Relevant file:

- `services/screen_tool_funcs/intervention_detector.py`

Important gates:

- active app type must be one of `document`, `presentation`, `spreadsheet`, `code_editor`
- sufficient dwell history, currently `min_history_count=5`
- stable paragraph text
- typing pause, currently `min_idle_captures=2`
- cooldown/deduplication pass

Do not assume no assistant response means monitoring is broken. Use diagnostics to distinguish:

- polling not started
- capture failure
- readable text not extracted
- intervention blocked by detector checks
- intervention queued but not consumed
- LLM generation failed

## Suggested Fix Plan

### 1. Add API/runtime lifecycle for screen monitoring

Add an explicit method on `AgentRuntime`, for example:

- `start_screen_monitoring(...)`
- `stop_screen_monitoring()`
- `screen_monitoring_status()`

Implementation should delegate to `self.chat_agent.start_screen_monitoring(...)` and `self.chat_agent.stop_screen_monitoring()`.

The runtime must decide where generated proactive answers go. Options:

- store them in repository state for frontend polling
- publish them over an SSE endpoint
- append them to the appropriate chat/session history

Do not just call `start_screen_monitoring()` without an `on_answer` strategy, unless console-only behavior is acceptable for the API process.

### 2. Add API endpoints for screen monitoring

Possible endpoints:

- `POST /api/v1/screen-monitoring/start`
- `POST /api/v1/screen-monitoring/stop`
- `GET /api/v1/screen-monitoring/status`
- `GET /api/v1/screen-monitoring/events/stream`

The status endpoint should expose at least:

- whether `screen_context` tool is registered
- whether `ScreenContextService` polling is active
- last polling error
- latest capture event metadata
- pending intervention count
- capture log path for debugging

The existing `screen_context` tool action `status` already returns useful service-level fields.

### 3. Connect frontend document assistant to proactive events

The frontend should have a way to receive screen-assist responses that were generated without an explicit user message.

Recommended shape:

- API stores proactive answers as document-assist/chat events.
- Frontend subscribes via SSE or periodically polls.
- UI renders proactive answers separately from explicit user messages if needed.

### 4. Keep explicit `/screen` debug path

Current `ChatAgent.ask_auto()` supports explicit screen commands:

- `/screen capture_once`
- `/screen status`
- `/screen debug`

Keep this available in API chat messages because it is useful for manual debugging.

### 5. Fix mojibake prompts separately

Repair corrupted Korean prompt literals in:

- `api/services/document_assist_service.py`
- `api/services/write_service.py`
- `api/services/draft_chat_service.py`

This can be handled independently from the monitoring lifecycle fix.

## Verification Checklist

After implementing the lifecycle fix:

- Starting API server registers `screen_context` when `VERITAS_ENABLE_SCREEN_CONTEXT != "0"`.
- Calling the new start endpoint starts `ScreenContextService` polling.
- `screen_context` status reports `polling: true`.
- Capture logs are written under the runtime output root, e.g. `runs/api/screen_context/capture_logs/...`.
- With `VERITAS_SCREEN_DEBUG=1`, logs show:
  - `[screen_context][capture]`
  - `[screen_context][decision]`
  - `[screen_context][intervention]` when queued
  - `[screen_context][queue]` when consumed
  - `[screen_context][assist]` when an answer is generated
- Frontend receives and displays proactive screen-assist answers.
- `stop` endpoint stops both polling and intervention consumption.

## Risk Notes

- `ChatAgent` has one shared `chat_history`. If proactive API monitoring is used across multiple frontend workspaces/sessions, history isolation may need to be addressed.
- `AgentRuntime.run_autosurvey(...)` builds a separate registry with `enable_screen_context=False`; that is probably correct and should not be changed unless survey runs need screen context.
- Starting monitoring automatically on API boot may surprise users because it captures foreground-window context. Prefer explicit opt-in from UI or config.
