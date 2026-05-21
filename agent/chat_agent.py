from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from typing import Any, Callable, Iterator

from core.prompts import (
    SCREEN_INTERVENTION_SYSTEM_PROMPT,
    SCREEN_INTERVENTION_USER_PROMPT_TEMPLATE,
    SCREEN_SCENARIO_GUIDANCE,
    SCREEN_SCENARIO_GUIDANCE_DEFAULT,
    SYSTEM_PROMPT,
    TOOL_CHAT_FINAL_PROMPT_TEMPLATE,
    TOOL_CHAT_SYSTEM_PROMPT,
    TOOL_CHAT_USER_PROMPT_TEMPLATE,
)
from tools.llm_tooling import build_llm_tooling


_STYLE_GUIDANCE_BY_REGISTER = {
    "합쇼체": "사용자는 격식체(합쇼체, '~습니다/~ㅂ니다')로 작성합니다. 답변도 동일한 합쇼체 격식 문장으로 작성하세요.",
    "해요체": "사용자는 해요체('~요')로 작성합니다. 답변도 친근하고 정중한 해요체로 작성하세요.",
    "음슴체": "사용자는 개조식·음슴체('~음/~함/~됨')로 작성합니다. 답변도 간결한 개조식 음슴체 종결로 작성하세요.",
    "평서체": "사용자는 문어체 평서문('~다/~이다/~한다')으로 작성합니다. 답변도 동일한 '~다' 평서체 문어체로, 객관적·학술적 어조를 유지하세요.",
    "반말체": "사용자는 반말체로 작성합니다. 답변도 편한 반말로 작성하세요.",
}


def detect_korean_style(text: str) -> str:
    """문서의 종결어미를 휴리스틱으로 분석해 문체 지시문을 반환.

    문장 종결부의 어미를 합쇼체/해요체/음슴체/평서체/반말체로 분류해 최빈
    register를 고른다. 한국어 종결 단서가 2문장 미만으로 부족하면 빈 문자열을
    반환해 호출자가 주입을 생략하도록 한다.
    """
    counts = {"합쇼체": 0, "해요체": 0, "음슴체": 0, "평서체": 0, "반말체": 0}
    for segment in re.split(r"[.!?\n\r·]+", str(text or "")):
        words = segment.strip().rstrip("\"'”’」』)]》").split()
        if not words:
            continue
        last = words[-1]
        if re.search(r"(니다|니까|십시오)$", last):
            counts["합쇼체"] += 1
        elif re.search(r"(에요|예요|아요|어요|세요|해요|네요|죠|요)$", last):
            counts["해요체"] += 1
        elif re.search(r"(음|함|됨|임|슴|봄|옴|짐|킴)$", last):
            counts["음슴체"] += 1
        elif re.search(
            r"(이다|한다|된다|었다|았다|였다|는다|린다|진다|난다|싶다|없다|있다|단다|온다|간다|다)$",
            last,
        ):
            counts["평서체"] += 1
        elif re.search(r"(거든|구나|군|네|지|야|어|걸)$", last):
            counts["반말체"] += 1
    if sum(counts.values()) < 2:
        return ""
    register = max(counts, key=lambda key: counts[key])
    if counts[register] == 0:
        return ""
    return _STYLE_GUIDANCE_BY_REGISTER[register]


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
        "screen_context",
    )
    TOOL_DECISION_TIMEOUT_SEC = 45
    FINAL_ANSWER_TIMEOUT_SEC = 180
    SCREEN_INTERVENTION_POLL_SEC = 2.0
    SCREEN_INTERVENTION_TIMEOUT_SEC = 180
    SCREEN_INTERVENTION_TTL_SEC = 300.0

    def __init__(
        self,
        *,
        llm,
        rag_service,
        tool_registry=None,
        optional_tool_names: tuple[str, ...] | None = None,
        max_history_turns: int = 3,
        max_tool_calls: int = 1,
        screen_debug: bool = False,
    ) -> None:
        self.llm = llm
        self.rag_service = rag_service
        self.tool_registry = tool_registry
        self.optional_tool_names = optional_tool_names or self.DEFAULT_OPTIONAL_TOOL_NAMES
        self.max_history_turns = max_history_turns
        self.max_tool_calls = max(1, int(max_tool_calls))
        self.screen_debug = screen_debug
        self.chat_history: list[tuple[str, str]] = []
        self._conversation_lock = threading.RLock()
        self._screen_stop_event = threading.Event()
        self._screen_monitor_thread: threading.Thread | None = None
        self._screen_answer_callback: Callable[[str, dict[str, Any], bool], None] | None = None
        self._last_handled_screen_event_id = ""

    def ask_rag(self, question: str, *, stream: bool = False) -> str:
        """Strict document-grounded Q&A mode for explicit --phase rag sessions."""
        with self._conversation_lock:
            answer = self.rag_service.answer(question, stream=stream, use_history=True)
            self._append_history(question, answer)
            return answer

    def ask_auto(self, question: str, *, stream: bool = False) -> str:
        """General chat mode with schema-driven optional tool use."""
        with self._conversation_lock:
            explicit_command = self._parse_explicit_command(question)
            if explicit_command:
                command, command_text = explicit_command
                if command == "screen" and command_text.lower() in {"debug", "raw"}:
                    answer = self._run_screen_debug_command()
                    self._append_history(question, answer)
                    return answer
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

    def ask_explicit_tool(self, command: str, question: str, *, stream: bool = False) -> str:
        """Run the same forced tool path used by CLI slash commands.

        This is intended for UI controls that choose a tool mode without making
        the user type `/autosurvey` or `/rag` into the prompt field.
        """
        normalized_command = str(command or "").strip().lower()
        if normalized_command not in {"autosurvey", "rag"}:
            raise ValueError(f"Unsupported explicit chat tool: {command}")

        command_text = str(question or "").strip()
        with self._conversation_lock:
            tool_outputs = self._run_explicit_tool_command(
                command=normalized_command,
                command_text=command_text,
            )
            answer = self._answer_from_current_turn(
                question=command_text,
                tool_outputs=tool_outputs,
                stream=stream,
            )
            self._append_history(command_text, answer)
            return answer

    def ask_auto_iter(self, question: str) -> Iterator[str]:
        """Generator variant of ask_auto. Tool decision runs non-streaming,
        the final answer is streamed as chunks, and history is updated when
        the stream completes.
        """
        with self._conversation_lock:
            explicit_command = self._parse_explicit_command(question)
            if explicit_command:
                command, command_text = explicit_command
                if command == "screen" and command_text.lower() in {"debug", "raw"}:
                    answer = self._run_screen_debug_command()
                    self._append_history(question, answer)
                    if answer:
                        yield answer
                    return
                tool_outputs = self._run_explicit_tool_command(
                    command=command,
                    command_text=command_text,
                )
                question_text = command_text or question
                yield from self._stream_final_answer(
                    question=question_text,
                    tool_outputs=tool_outputs,
                    history_question=question,
                )
                return

            tool_outputs = self._collect_tool_outputs(question)
            yield from self._stream_final_answer(
                question=question,
                tool_outputs=tool_outputs,
                history_question=question,
            )

    def ask_explicit_tool_iter(self, command: str, question: str) -> Iterator[str]:
        normalized_command = str(command or "").strip().lower()
        if normalized_command not in {"autosurvey", "rag"}:
            raise ValueError(f"Unsupported explicit chat tool: {command}")

        command_text = str(question or "").strip()
        with self._conversation_lock:
            tool_outputs = self._run_explicit_tool_command(
                command=normalized_command,
                command_text=command_text,
            )
            yield from self._stream_final_answer(
                question=command_text,
                tool_outputs=tool_outputs,
                history_question=command_text,
            )

    def ask_rag_iter(self, question: str) -> Iterator[str]:
        with self._conversation_lock:
            collected: list[str] = []
            try:
                for chunk in self.rag_service.iter_answer(question, use_history=True):
                    collected.append(chunk)
                    yield chunk
            except AttributeError:
                # rag_service does not support iter_answer yet; fall back to one-shot.
                answer = self.rag_service.answer(question, stream=False, use_history=True)
                collected.append(answer)
                yield answer
            self._append_history(question, "".join(collected))

    def _stream_final_answer(
        self,
        *,
        question: str,
        tool_outputs: list[dict[str, str]],
        history_question: str,
    ) -> Iterator[str]:
        prompt = TOOL_CHAT_FINAL_PROMPT_TEMPLATE.format(
            history=self._format_recent_history(),
            question=question,
            tool_results=self._format_tool_results(tool_outputs),
        )
        collected: list[str] = []
        try:
            for chunk in self.llm.iter_ask(
                self._chat_system_prompt(),
                prompt,
                reasoning=False,
                stream_label="chat:final" if tool_outputs else "chat",
                timeout_sec=self.FINAL_ANSWER_TIMEOUT_SEC,
            ):
                if not chunk:
                    continue
                collected.append(chunk)
                yield chunk
        except Exception as e:
            error_text = f"[chat][error] failed to generate answer: {e}"
            print(error_text)
            collected.append(error_text)
            yield error_text

        final_text = "".join(collected).strip()
        self._append_history(history_question, final_text)

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
        if command not in {"autosurvey", "rag", "screen"}:
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

        if command in {"autosurvey", "rag"} and not command_text:
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

        if command == "screen":
            action = command_text or "capture_once"
            return self._call_registry_tool(
                "screen_context",
                action=action,
            )

        return []

    def _run_screen_debug_command(self) -> str:
        if self.tool_registry is None:
            return self._pretty_payload({"error": "Tool registry is not available."})

        try:
            result = self.tool_registry.call("screen_context", action="capture_once")
        except Exception as e:
            return self._pretty_payload({"error": str(e)})

        payload = {
            "action": "capture_once",
            "success": result.success,
            "error": result.error,
            "content_chars": len(result.content or ""),
            "data": result.data if result.data is not None else {},
        }
        return self._pretty_payload(payload)

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

    def has_screen_context(self) -> bool:
        return bool(self.tool_registry is not None and self.tool_registry.has("screen_context"))

    def start_screen_monitoring(
        self,
        *,
        on_answer: Callable[[str, dict[str, Any], bool], None] | None = None,
        stream: bool = False,
    ) -> bool:
        """Start screen polling and proactive intervention consumption."""
        if not self.has_screen_context():
            return False
        if self._screen_monitor_thread and self._screen_monitor_thread.is_alive():
            self._screen_answer_callback = on_answer
            return True

        start_result = self.tool_registry.call("screen_context", action="start_polling")
        if not start_result.success:
            print(f"[screen_context][warn] failed to start polling: {start_result.error}")
            return False

        self._screen_answer_callback = on_answer
        self._screen_stop_event.clear()
        self._screen_monitor_thread = threading.Thread(
            target=self._screen_intervention_loop,
            kwargs={"stream": stream},
            daemon=True,
        )
        self._screen_monitor_thread.start()
        return True

    def stop_screen_monitoring(self) -> None:
        self._screen_stop_event.set()
        if self._screen_monitor_thread and self._screen_monitor_thread.is_alive():
            self._screen_monitor_thread.join(timeout=self.SCREEN_INTERVENTION_POLL_SEC + 1)
        self._screen_monitor_thread = None
        self._screen_answer_callback = None
        if self.has_screen_context():
            result = self.tool_registry.call("screen_context", action="stop_polling")
            if not result.success:
                print(f"[screen_context][warn] failed to stop polling: {result.error}")

    def answer_screen_intervention(
        self,
        intervention: dict[str, Any],
        *,
        stream: bool = False,
    ) -> str:
        """Generate a proactive answer from one queued screen intervention."""
        with self._conversation_lock:
            query = self._screen_intervention_query(intervention)
            knowledge_context = self._screen_knowledge_context(query)
            intervention_type = intervention.get("intervention_type") or "none"
            scenario_guidance = SCREEN_SCENARIO_GUIDANCE.get(
                intervention_type, SCREEN_SCENARIO_GUIDANCE_DEFAULT
            )
            prompt = SCREEN_INTERVENTION_USER_PROMPT_TEMPLATE.format(
                history=self._format_recent_history(),
                app_context=self._pretty_payload(
                    intervention.get("app_context") or intervention.get("app") or {}
                ),
                writing_context=self._pretty_payload(
                    self._screen_prompt_writing_context(intervention)
                ),
                routing_hint=self._pretty_payload(intervention.get("tool_routing_hint") or {}),
                scenario_guidance=scenario_guidance,
                style_guidance=self._screen_style_guidance(intervention),
                knowledge_context=knowledge_context,
            )
            try:
                if stream:
                    answer = self._stream_screen_answer(prompt, intervention)
                else:
                    answer = self.llm.ask(
                        SCREEN_INTERVENTION_SYSTEM_PROMPT,
                        prompt,
                        reasoning=False,
                        stream=False,
                        stream_label="screen_context",
                        timeout_sec=self.SCREEN_INTERVENTION_TIMEOUT_SEC,
                    )
            except Exception as e:
                answer = f"[screen_context][error] failed to generate answer: {e}"
                print(answer)

            if not str(answer or "").strip():
                answer = "[screen_context][warn] LLM returned an empty screen assist answer."
            history_question = self._screen_history_question(intervention)
            self._append_history(history_question, answer)
            return answer.strip()

    def _stream_screen_answer(self, prompt: str, intervention: dict[str, Any]) -> str:
        """Consume the screen-intervention answer as a token stream, pushing
        cumulative partial text to the answer callback (done=False) so the UI
        renders it incrementally. Returns the full accumulated text; the loop
        emits the final done=True update. Partial emits are throttled by char
        count so the event buffer is not hammered per token."""
        chunks: list[str] = []
        last_emit_len = 0
        min_emit_chars = 12
        for chunk in self.llm.iter_ask(
            SCREEN_INTERVENTION_SYSTEM_PROMPT,
            prompt,
            reasoning=False,
            stream_label="screen_context",
            timeout_sec=self.SCREEN_INTERVENTION_TIMEOUT_SEC,
        ):
            if not chunk:
                continue
            chunks.append(chunk)
            accumulated = "".join(chunks)
            if (
                self._screen_answer_callback is not None
                and len(accumulated) - last_emit_len >= min_emit_chars
            ):
                last_emit_len = len(accumulated)
                try:
                    self._screen_answer_callback(accumulated, intervention, False)
                except Exception as e:
                    print(f"[screen_context][assist][warn] partial emit failed: {e}")
        return "".join(chunks)

    
    #single queue version with peek + separate consume
    def _screen_intervention_loop(self, *, stream: bool) -> None:
        while not self._screen_stop_event.wait(self.SCREEN_INTERVENTION_POLL_SEC):
            try:
                pending = self._peek_screen_interventions(limit=1)
                if not pending:
                    continue
                intervention = pending[0]
                # peek만 — LLM 처리 동안 큐에 남겨 producer가 점유중으로 인식하게 함
                try:
                    if not self._fresh_screen_interventions([intervention]):
                        continue  # dup/stale → finally에서 제거
                    event_id = str(intervention.get("event_id") or "-")
                    if self.screen_debug:
                        query = self._screen_intervention_query(intervention)
                        print(
                            "[screen_context][assist] "
                            f"event={event_id} generating query={query[:160]!r}"
                        )
                    answer = self.answer_screen_intervention(intervention, stream=stream)
                    if self.screen_debug:
                        print(
                            "[screen_context][assist] "
                            f"event={event_id} answer_chars={len(answer)}"
                        )
                    if self._screen_answer_callback is not None:
                        self._screen_answer_callback(answer, intervention, True)
                    else:
                        print("\n[Screen Assist]")
                        print(answer)
                        print()
                finally:
                    # 처리·스킵 결과와 무관하게 큐에서 1개 제거 — producer 점유 해제용,
                    # 실패해도 같은 항목 무한 재시도 방지
                    self._remove_screen_intervention()
            except Exception as e:
                print(f"[screen_context][assist][error] {type(e).__name__}: {e}")


    def _peek_screen_interventions(self, *, limit: int) -> list[dict[str, Any]]:
        """큐에서 제거하지 않고 대기 개입을 읽기만 한다."""
        if not self.has_screen_context():
            return []
        try:
            result = self.tool_registry.call(
                "screen_context", action="pending_interventions", limit=limit,
            )
        except Exception as e:
            print(f"[screen_context][warn] failed to peek interventions: {e}")
            return []
        if not result.success:
            print(f"[screen_context][warn] failed to peek interventions: {result.error}")
            return []
        data = result.data if isinstance(result.data, dict) else {}
        return [item for item in data.get("interventions", []) if isinstance(item, dict)]

    def _remove_screen_intervention(self) -> None:
        """큐 맨 앞 1개를 제거한다 (peek 후 처리/스킵을 마친 항목)."""
        if not self.has_screen_context():
            return
        try:
            result = self.tool_registry.call(
                "screen_context", action="consume_interventions", limit=1,
            )
        except Exception as e:
            print(f"[screen_context][warn] failed to remove intervention: {e}")
            return
        if not result.success:
            print(f"[screen_context][warn] failed to remove intervention: {result.error}")
            return
        if self.screen_debug:
            data = result.data if isinstance(result.data, dict) else {}
            ids = [str(i.get("event_id") or "-") for i in data.get("interventions", []) if isinstance(i, dict)]
            if ids:
                print(f"[screen_context][queue] removed event_ids={ids}")


    """
    "multiple size queue version" 
    def _screen_intervention_loop(self, *, stream: bool) -> None:
        while not self._screen_stop_event.wait(self.SCREEN_INTERVENTION_POLL_SEC):
            try:
                interventions = self._consume_screen_interventions(limit=1)
                for intervention in self._fresh_screen_interventions(interventions):
                    event_id = str(intervention.get("event_id") or "-")
                    if self.screen_debug:
                        query = self._screen_intervention_query(intervention)
                        print(
                            "[screen_context][assist] "
                            f"event={event_id} generating query={query[:160]!r}"
                        )
                    answer = self.answer_screen_intervention(intervention, stream=stream)
                    if self.screen_debug:
                        print(
                            "[screen_context][assist] "
                            f"event={event_id} answer_chars={len(answer)}"
                        )
                    if self._screen_answer_callback is not None:
                        self._screen_answer_callback(answer, intervention)
                    else:
                        print("\n[Screen Assist]")
                        print(answer)
                        print()
            except Exception as e:
                # Keep screen monitoring alive even if LLM generation, printing, or
                # an external callback fails for one proactive intervention.
                print(f"[screen_context][assist][error] {type(e).__name__}: {e}")

    def _consume_screen_interventions(self, *, limit: int) -> list[dict[str, Any]]:
        if not self.has_screen_context():
            return []
        try:
            result = self.tool_registry.call(
                "screen_context",
                action="consume_interventions",
                limit=limit,
            )
        except Exception as e:
            print(f"[screen_context][warn] failed to consume interventions: {e}")
            return []
        if not result.success:
            print(f"[screen_context][warn] failed to consume interventions: {result.error}")
            return []
        data = result.data if isinstance(result.data, dict) else {}
        interventions = data.get("interventions", [])
        valid = [item for item in interventions if isinstance(item, dict)]
        if self.screen_debug and valid:
            ids = [str(item.get("event_id") or "-") for item in valid]
            print(f"[screen_context][queue] consumed={len(valid)} event_ids={ids}")
        return valid
    """

    def _fresh_screen_interventions(
        self,
        interventions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        fresh: list[dict[str, Any]] = []
        for intervention in interventions:
            event_id = str(intervention.get("event_id") or "").strip()
            if event_id and event_id == self._last_handled_screen_event_id:
                self._log_screen_drop(
                    intervention,
                    reason="duplicate_event_id",
                )
                continue
            if self._is_stale_screen_intervention(intervention):
                self._log_screen_drop(
                    intervention,
                    reason="stale_intervention",
                )
                continue
            if event_id:
                self._last_handled_screen_event_id = event_id
            fresh.append(intervention)
        return fresh

    def _is_stale_screen_intervention(self, intervention: dict[str, Any]) -> bool:
        captured_at = str(intervention.get("captured_at") or "").strip()
        if not captured_at:
            return False
        try:
            captured = datetime.fromisoformat(captured_at)
        except ValueError:
            return False
        age_sec = (datetime.now() - captured).total_seconds()
        return age_sec > self.SCREEN_INTERVENTION_TTL_SEC

    def _log_screen_drop(self, intervention: dict[str, Any], *, reason: str) -> None:
        if not self.screen_debug:
            return
        event_id = str(intervention.get("event_id") or "-")
        captured_at = str(intervention.get("captured_at") or "")
        age_text = "-"
        if captured_at:
            try:
                age_text = f"{(datetime.now() - datetime.fromisoformat(captured_at)).total_seconds():.1f}s"
            except ValueError:
                age_text = "invalid"
        print(
            "[screen_context][queue] "
            f"drop event={event_id} reason={reason} age={age_text} "
            f"ttl={self.SCREEN_INTERVENTION_TTL_SEC:.0f}s"
        )

    def _screen_intervention_query(self, intervention: dict[str, Any]) -> str:
        writing = intervention.get("writing_context") if isinstance(intervention, dict) else {}
        if not isinstance(writing, dict):
            writing = {}
        candidates = [
            writing.get("recent_sentences"),
            writing.get("focused_sentence"),
            writing.get("changed_text"),
            writing.get("current_paragraph"),
        ]
        for candidate in candidates:
            text = " ".join(str(candidate or "").split()).strip()
            if text:
                return text[:1000]
        return "screen writing context"

    def _screen_prompt_writing_context(self, intervention: dict[str, Any]) -> dict[str, Any]:
        writing = intervention.get("writing_context") if isinstance(intervention, dict) else {}
        if not isinstance(writing, dict):
            return {}

        recent_sentences = " ".join(
            str(writing.get("recent_sentences") or "").split()
        ).strip()
        focused_sentence = " ".join(
            str(writing.get("focused_sentence") or "").split()
        ).strip()
        changed_text = " ".join(str(writing.get("changed_text") or "").split()).strip()
        current_paragraph = " ".join(
            str(writing.get("current_paragraph") or "").split()
        ).strip()

        scoped_text = (
            recent_sentences
            or focused_sentence
            or changed_text
            or current_paragraph[:1000]
        )
        full_text_chars = writing.get(
            "full_text_chars",
            len(str(writing.get("full_text") or "")),
        )
        return {
            "recent_sentences": scoped_text,
            "focused_sentence": focused_sentence,
            "changed_text": changed_text[:500],
            "paragraph_source": writing.get("paragraph_source") or "",
            "paragraph_rect": writing.get("paragraph_rect"),
            "full_text_chars": full_text_chars,
            "confidence": writing.get("confidence", 0.0),
            "scope_note": "Only the latest 1-2 sentences are provided for this intervention.",
        }

    def _screen_style_guidance(self, intervention: dict[str, Any]) -> str:
        """사용자 문서 원문에서 문체를 감지해 프롬프트용 지시문을 만든다.
        full_text(전체 문서)는 리뷰 시나리오의 지시문 오버라이드에 오염되지 않으므로
        문체 분석 소스로 가장 안전하다. 단서가 부족하면 일반 지침으로 폴백."""
        writing = intervention.get("writing_context") if isinstance(intervention, dict) else {}
        if not isinstance(writing, dict):
            writing = {}
        sample = str(writing.get("full_text") or writing.get("current_paragraph") or "")
        return detect_korean_style(sample[:3000]) or (
            "뚜렷한 문체 단서가 없으면 화면 텍스트의 언어와 어조를 그대로 따르세요."
        )

    def _screen_history_question(self, intervention: dict[str, Any]) -> str:
        app = intervention.get("app_context") or intervention.get("app") or {}
        writing = intervention.get("writing_context") or {}
        title = app.get("title") if isinstance(app, dict) else ""
        focused = writing.get("focused_sentence") if isinstance(writing, dict) else ""
        focused_text = " ".join(str(focused or "").split()).strip()
        title_text = " ".join(str(title or "").split()).strip()
        if focused_text:
            return f"[screen_context] {focused_text[:200]}"
        if title_text:
            return f"[screen_context] active window: {title_text[:200]}"
        return "[screen_context] proactive intervention"

    def _screen_knowledge_context(self, query: str) -> str:
        if self.rag_service is None:
            return "(No knowledge base service is available.)"
        try:
            if self.rag_service.get_document_count() <= 0:
                return "(The knowledge base is empty.)"
            documents = self.rag_service.retrieve(query, use_history=False)
            context = self.rag_service.format_retrieved_documents(documents)
            return context if context.strip() else "(No relevant knowledge-base documents found.)"
        except Exception as e:
            return f"(Knowledge-base lookup failed: {e})"

    def _pretty_payload(self, payload: Any) -> str:
        try:
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception:
            return str(payload)

    def chat_loop(self, *, mode: str = "auto", enable_screen_context: bool = False) -> None:
        doc_count = self.rag_service.get_document_count() if self.rag_service is not None else 0
        mode_label = "RAG" if mode == "rag" else "schema-driven tool chat"
        allowed = ", ".join(self.optional_tool_names)
        print(f"\n[Chat] {mode_label}. {doc_count} RAG chunks indexed. Type 'exit' to quit.")
        if mode == "auto":
            print(f"[Chat] Exposed tools: {allowed}\n")
            print("[Chat] Explicit commands: /autosurvey <request>, /rag <question>, /screen [action], /screen debug\n")
        else:
            print()

        screen_monitoring_started = False
        if enable_screen_context and mode == "auto":
            screen_monitoring_started = self.start_screen_monitoring(stream=False)
            if screen_monitoring_started:
                print("[Chat] Screen context monitoring is active.\n")

        try:
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
        finally:
            if screen_monitoring_started:
                self.stop_screen_monitoring()
