from __future__ import annotations

import json

from core.prompts import (
    SYSTEM_PROMPT,
    TOOL_CHAT_FINAL_PROMPT_TEMPLATE,
    TOOL_CHAT_SYSTEM_PROMPT,
    TOOL_CHAT_USER_PROMPT_TEMPLATE,
)
from tools.llm_tooling import build_llm_tooling


class ChatAgent:
    """General chat orchestration with schema-driven tool use.

    Responsibilities:
    - maintain chat history and append exactly one assistant answer per user turn;
    - expose a stage-specific allowlist of high-level tools to the LLM;
    - collect tool results, then ask the LLM to synthesize a final user-facing answer;
    - avoid hard-coded keyword/regex routing for tool selection.

    Tool selection is delegated to the LLM using the system prompt and tool schema
    descriptions. Execution constraints such as tool allowlists and per-tool caps
    remain enforced by code.
    """

    DEFAULT_OPTIONAL_TOOL_NAMES = (
        "current_time",
        "rag_search",
        "autosurvey",
    )
    TOOL_DECISION_TIMEOUT_SEC = 45
    FINAL_ANSWER_TIMEOUT_SEC = 180

    def __init__(
        self,
        *,
        llm,
        rag_service,
        tool_registry=None,
        optional_tool_names: tuple[str, ...] | None = None,
        max_history_turns: int = 3,
        max_tool_calls: int = 1,
    ) -> None:
        self.llm = llm
        self.rag_service = rag_service
        self.tool_registry = tool_registry
        self.optional_tool_names = optional_tool_names or self.DEFAULT_OPTIONAL_TOOL_NAMES
        self.max_history_turns = max_history_turns
        self.max_tool_calls = max(1, int(max_tool_calls))
        self.chat_history: list[tuple[str, str]] = []

    def ask_rag(self, question: str, *, stream: bool = False) -> str:
        """Strict document-grounded Q&A mode for explicit --phase rag sessions."""
        answer = self.rag_service.answer(question, stream=stream, use_history=True)
        self._append_history(question, answer)
        return answer

    def ask_auto(self, question: str, *, stream: bool = False) -> str:
        """General chat mode with schema-driven optional tool use."""
        explicit_command = self._parse_explicit_command(question)
        if explicit_command:
            command, command_text = explicit_command
            tool_outputs = self._run_explicit_tool_command(
                command=command,
                command_text=command_text,
            )
            answer = self._answer_from_current_turn(
                question=command_text or question,
                tool_outputs=tool_outputs,
                stream=stream,
            )
            self._append_history(question, answer)
            return answer

        tool_outputs = self._collect_tool_outputs(question)
        answer = self._answer_from_current_turn(
            question=question,
            tool_outputs=tool_outputs,
            stream=stream,
        )
        self._append_history(question, answer)
        return answer

    def _append_history(self, question: str, answer: str) -> None:
        self.chat_history.append((question, answer))
        if self.rag_service is not None:
            self.rag_service.chat_history = list(self.chat_history)

    def _format_recent_history(self) -> str:
        if not self.chat_history:
            return "(No previous conversation)"
        recent = self.chat_history[-self.max_history_turns :]
        parts: list[str] = []
        for i, (user_q, assistant_a) in enumerate(recent, start=1):
            parts.append(f"Turn {i} User: {user_q}")
            parts.append(f"Turn {i} Assistant: {assistant_a}")
        return "\n".join(parts)

    def _chat_system_prompt(self) -> str:
        return TOOL_CHAT_SYSTEM_PROMPT.format(base_system_prompt=SYSTEM_PROMPT)

    def _build_tool_decision_prompt(self, question: str) -> str:
        return TOOL_CHAT_USER_PROMPT_TEMPLATE.format(
            history=self._format_recent_history(),
            question=question,
        )

    def _parse_explicit_command(self, question: str) -> tuple[str, str] | None:
        text = str(question or "").strip()
        if not text.startswith("/"):
            return None

        head, _, tail = text.partition(" ")
        command = head[1:].strip().lower()
        if command not in {"autosurvey", "rag"}:
            return None

        return command, tail.strip()

    def _run_explicit_tool_command(
        self,
        *,
        command: str,
        command_text: str,
    ) -> list[dict[str, str]]:
        if self.tool_registry is None:
            return [
                {
                    "name": command,
                    "content": json.dumps(
                        {"error": "Tool registry is not available."},
                        ensure_ascii=False,
                    ),
                }
            ]

        if not command_text:
            return [
                {
                    "name": command,
                    "content": json.dumps(
                        {"error": "A request after the slash command is required."},
                        ensure_ascii=False,
                    ),
                }
            ]

        if command == "autosurvey":
            return self._call_registry_tool(
                "autosurvey",
                request=command_text,
            )

        if command == "rag":
            return self._call_registry_tool(
                "rag_search",
                query=command_text,
                use_history=True,
            )

        return []

    def _call_registry_tool(self, tool_name: str, **arguments) -> list[dict[str, str]]:
        try:
            result = self.tool_registry.call(tool_name, **arguments)
        except Exception as e:
            return [
                {
                    "name": tool_name,
                    "content": json.dumps({"error": str(e)}, ensure_ascii=False),
                }
            ]

        if not result.success:
            error = result.error or f"{tool_name} failed"
            return [
                {
                    "name": tool_name,
                    "content": json.dumps({"error": error}, ensure_ascii=False),
                }
            ]

        payload = result.data if result.data is not None else {"content": result.content or ""}
        try:
            content = json.dumps(payload, ensure_ascii=False)
        except Exception:
            content = str(payload)
        return [{"name": tool_name, "content": content}]

    def _collect_tool_outputs(self, question: str) -> list[dict[str, str]]:
        llm_tools, llm_tool_runner = build_llm_tooling(
            self.tool_registry,
            stage_label="chat",
            allowed_tool_names=self.optional_tool_names,
        )
        if not llm_tools or not llm_tool_runner:
            return []

        try:
            decision = self.llm.collect_tool_outputs(
                self._chat_system_prompt(),
                self._build_tool_decision_prompt(question),
                reasoning=False,
                tools=llm_tools,
                tool_runner=llm_tool_runner,
                max_tool_calls=self.max_tool_calls,
                stream_label="chat:tools",
                timeout_sec=self.TOOL_DECISION_TIMEOUT_SEC,
            )
        except Exception as e:
            print(f"[chat][tools] tool decision failed; answering directly: {e}")
            return []
        raw_outputs = decision.get("tool_outputs", [])
        if not isinstance(raw_outputs, list):
            return []

        outputs: list[dict[str, str]] = []
        for item in raw_outputs:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            content = str(item.get("content") or "").strip()
            if name and content:
                outputs.append({"name": name, "content": content})
        return outputs

    def _answer_from_current_turn(
        self,
        *,
        question: str,
        tool_outputs: list[dict[str, str]],
        stream: bool = False,
    ) -> str:
        prompt = TOOL_CHAT_FINAL_PROMPT_TEMPLATE.format(
            history=self._format_recent_history(),
            question=question,
            tool_results=self._format_tool_results(tool_outputs),
        )
        try:
            answer = self.llm.ask(
                self._chat_system_prompt(),
                prompt,
                reasoning=False,
                stream=stream,
                stream_label="chat:final" if tool_outputs else "chat",
                timeout_sec=self.FINAL_ANSWER_TIMEOUT_SEC,
            )
        except Exception as e:
            answer = f"[chat][error] failed to generate answer: {e}"
            print(answer)
        return answer.strip()

    def _format_tool_results(self, tool_outputs: list[dict[str, str]]) -> str:
        if not tool_outputs:
            return "(No tool was used for this turn.)"

        parts: list[str] = []
        for index, output in enumerate(tool_outputs, start=1):
            name = output.get("name", "unknown_tool")
            content = output.get("content", "")
            pretty_content = self._pretty_json(content)
            parts.append(
                f"TOOL RESULT {index}\n"
                f"tool_name: {name}\n"
                f"tool_output:\n{pretty_content}"
            )
        return "\n\n---\n\n".join(parts)

    def _pretty_json(self, text: str) -> str:
        try:
            payload = json.loads(text)
        except Exception:
            return text
        try:
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception:
            return text

    def chat_loop(self, *, mode: str = "auto") -> None:
        doc_count = self.rag_service.get_document_count() if self.rag_service is not None else 0
        mode_label = "RAG" if mode == "rag" else "schema-driven tool chat"
        allowed = ", ".join(self.optional_tool_names)
        print(f"\n[Chat] {mode_label}. {doc_count} RAG chunks indexed. Type 'exit' to quit.")
        if mode == "auto":
            print(f"[Chat] Exposed tools: {allowed}\n")
            print("[Chat] Explicit commands: /autosurvey <request>, /rag <question>\n")
        else:
            print()

        while True:
            try:
                question = input("User: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[Chat] Goodbye!")
                break

            if not question:
                continue
            if question.lower() in ("exit", "quit", "q"):
                print("[Chat] Goodbye!")
                break

            print()
            if mode == "rag":
                self.ask_rag(question, stream=True)
            elif mode == "auto":
                self.ask_auto(question, stream=True)
            else:
                raise ValueError(f"Unsupported chat mode: {mode}")
            print()
