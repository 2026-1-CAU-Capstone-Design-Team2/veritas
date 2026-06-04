# Repository Guidelines

## Project Structure & Module Organization
VERITAS is a Python local-first research and document assistant. `main.py` drives CLI AutoSurvey/RAG phases; `launcher.py` starts the desktop/API/model flow. Shared runtime lives in `agent/`, `workflows/`, `tools/`, and `services/`. `api/` contains FastAPI routes and services. `frontend/` contains the PySide6 UI. `core/` holds models and prompts; `llm/`, `storage/`, and `db/` cover clients and persistence. Tests are in `tests/`; generated `runs/` and `test_data/` are ignored.

## Build, Test, and Development Commands
Run commands from the repository root unless noted.

- `python -m pip install -r requirements.txt` installs Python dependencies.
- `python launcher.py` starts the desktop app and manages local model/API startup.
- `python -m api --api --host 127.0.0.1 --port 8000` runs only the FastAPI server.
- `python main.py "research topic" --output-dir ./output --phase all` runs the full CLI AutoSurvey workflow.
- `python -m unittest discover tests` runs the full test suite.

## Planning & Review Instructions
For review or implementation-planning guidance, first make a brief plan, summarize it to the user, then update `INSTRUCTION.md` with the checklist, architecture constraints, verification commands, and review focus.

## Coding Style & Naming Conventions
Use Python 3.11+ conventions, four-space indentation, type hints, and structured payload models. Keep FastAPI route handlers thin; put logic in `api/services/` or `services/`. Long-running FastAPI handlers should be plain `def`. Tool packages follow `tools/<name>_tool/` with `tool_schema.json` plus `BaseTool`.

## Prompt & Citation Guardrails
`final.md` citations must use one canonical marker: `[doc_000]`. Enforce this in prompts or presentation code, not by mutating sources. Do not add deterministic boilerplate removal based on fixed keyword lists such as tags, share labels, footer words, or site-specific phrases. For source quality, prefer model instructions, structural parsing, or language/domain-agnostic upstream extraction.

## Testing Guidelines
Tests use `unittest`, not `pytest`. Name files `test_<topic>.py` and classes `<Topic>Tests(unittest.TestCase)`. Mock LLMs, FastAPI calls, and filesystem-heavy services with callable injection or small fakes. Add focused regression tests for bug fixes.

## Commit & Pull Request Guidelines
Recent commits use prefixes such as `[fix]` and `[feat]` plus concise Korean or English summaries. PRs should include a description, tests run, linked issue/task, and screenshots for UI changes.

## Security & Configuration Tips
Do not commit API keys, `.env` files, generated logs, model files, `runs/`, or `test_data/`. Local app state is stored under `%LOCALAPPDATA%/VERITAS`; keep user documents and personal model paths out of source control.
