"""LLM Client for llama-server with OpenAI-compatible API.

Supports both chat completions and embeddings via /v1/chat/completions
and /v1/embeddings endpoints.
"""

import json
import re
import time
from typing import Any, Callable

from openai import OpenAI


class LLMClient:
    """OpenAI-compatible client for llama-server (LLM + Embeddings)."""

    SAMPLING_PARAMS = {
        "temperature": 1.0,
        "top_p": 0.95,
        "presence_penalty": 1.5,
    }
    EXTRA_SAMPLING_PARAMS = {
        "top_k": 20,
        "min_p": 0.0,
        "repeat_penalty": 1.0,
    }
    JSON_SAMPLING_PARAMS = {
        "temperature": 0.0,
        "top_p": 0.15,
        "presence_penalty": 0.0,
    }
    JSON_EXTRA_SAMPLING_PARAMS = {
        "top_k": 5,
        "min_p": 0.0,
        "repeat_penalty": 1.05,
    }

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        *,
        embed_host: str | None = None,
        embed_port: int | None = None,
        stream_summary: bool = False,
        stream_reasoning: bool = False,
        trace_latency: bool = True,
    ):
        # Chat completions client
        self.chat_base_url = f"http://{host}:{port}/v1"
        self.client = OpenAI(base_url=self.chat_base_url, api_key="sk-no-key-required")
        models = self.client.models.list().data
        if not models:
            raise RuntimeError("No models available from llama-server.")
        self.model = models[0].id

        # Embedding client. If only one embedding endpoint arg is provided,
        # inherit the missing value from chat endpoint.
        resolved_embed_host = embed_host or host
        resolved_embed_port = embed_port or port
        self.embed_base_url = f"http://{resolved_embed_host}:{resolved_embed_port}/v1"

        # Embedding client (same server or separate server)
        if resolved_embed_host != host or resolved_embed_port != port:
            self.embed_client = OpenAI(
                base_url=self.embed_base_url,
                api_key="sk-no-key-required",
            )
            embed_models = self.embed_client.models.list().data
            if not embed_models:
                raise RuntimeError("No models available from embedding server.")
            self.embed_model = embed_models[0].id
        else:
            self.embed_client = self.client
            self.embed_model = self.model

        self.stream_summary = stream_summary
        self.stream_reasoning = stream_reasoning
        self.trace_latency = trace_latency

    def ask(
        self,
        system_prompt: str,
        user_prompt: str,
        reasoning: bool = False,
        *,
        stream: bool = False,
        stream_label: str = "",
        sampling_params: dict[str, Any] | None = None,
        extra_sampling_params: dict[str, Any] | None = None,
        force_json: bool = False,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_runner: Callable[[str, dict[str, Any]], Any] | None = None,
        max_tool_rounds: int = 4,
    ) -> str:
        """Generate a chat completion response."""
        think_tag = "/think" if reasoning else "/no_think"
        start = time.perf_counter()

        sampled_params = {**self.SAMPLING_PARAMS, **(sampling_params or {})}
        sampled_extra = {**self.EXTRA_SAMPLING_PARAMS, **(extra_sampling_params or {})}
        system_text = system_prompt.strip()
        if force_json:
            system_text = (
                f"{system_text}\n"
                "Return a strict JSON object only. "
                "Do not include markdown fences, commentary, or extra wrapper text."
            )

        extra_body = {
            **sampled_extra,
            "enable_thinking": reasoning,
            "enable_reasoning": reasoning,
            "chat_template_kwargs": {
                "enable_thinking": reasoning,
                "enable_reasoning": reasoning,
            },
        }

        if stream and tools:
            print(
                "[llm][tools] stream=True is not supported with tool calls; "
                "falling back to non-stream mode"
            )
            stream = False

        if stream:
            chunks = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": f"{think_tag}\n{user_prompt}"},
                ],
                stream=True,
                **sampled_params,
                extra_body=extra_body,
            )
            text = self._consume_stream(
                chunks, stream_label=stream_label, filter_think=not reasoning
            )
        else:
            text = self._ask_with_optional_tools(
                system_text=system_text,
                user_text=f"{think_tag}\n{user_prompt}",
                sampled_params=sampled_params,
                extra_body=extra_body,
                response_format=response_format,
                tools=tools,
                tool_runner=tool_runner,
                max_tool_rounds=max_tool_rounds,
            )

        if self.trace_latency:
            elapsed = time.perf_counter() - start
            tag = f" [{stream_label}]" if stream_label else ""
            mode = "think" if reasoning else "no_think"
            print(f"[llm]{tag} mode={mode} elapsed={elapsed:.2f}s")

        # Strip <think> tags if reasoning is disabled
        if not reasoning:
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

        return text.strip()

    def ask_json(
        self,
        system_prompt: str,
        user_prompt: str,
        reasoning: bool = False,
        max_retries: int = 2,
        *,
        stream: bool = False,
        stream_label: str = "",
        tools: list[dict[str, Any]] | None = None,
        tool_runner: Callable[[str, dict[str, Any]], Any] | None = None,
        max_tool_rounds: int = 4,
    ) -> dict[str, Any]:
        """Generate a chat completion and parse as JSON."""
        last_error: Exception | None = None
        prefer_response_format = True
        for attempt in range(max_retries + 1):
            attempt_no = attempt + 1
            stage = "first-attempt" if attempt == 0 else "retry"
            tag = f" [{stream_label}]" if stream_label else ""
            print(f"[llm][json]{tag} {stage}={attempt_no}/{max_retries + 1}")

            try:
                text = self.ask(
                    system_prompt,
                    user_prompt,
                    reasoning=reasoning,
                    stream=stream,
                    stream_label=stream_label,
                    sampling_params=self.JSON_SAMPLING_PARAMS,
                    extra_sampling_params=self.JSON_EXTRA_SAMPLING_PARAMS,
                    force_json=True,
                    response_format=(
                        {"type": "json_object"}
                        if (prefer_response_format and not stream and not tools)
                        else None
                    ),
                    tools=tools,
                    tool_runner=tool_runner,
                    max_tool_rounds=max_tool_rounds,
                )
            except Exception as e:
                last_error = e
                if prefer_response_format and not stream:
                    prefer_response_format = False
                    print(
                        f"[llm][json]{tag} response_format-fallback "
                        f"reason={e}"
                    )
                    if attempt < max_retries:
                        continue
                if attempt < max_retries:
                    continue
                break

            try:
                return self._extract_json(text)
            except json.JSONDecodeError as e:
                last_error = e
                print(
                    f"[llm][json]{tag} parse-failed "
                    f"attempt={attempt_no}/{max_retries + 1} error={e}"
                )
                if attempt < max_retries:
                    continue

        if isinstance(last_error, Exception):
            raise last_error
        raise json.JSONDecodeError("Failed to parse JSON", "", 0)

    def _ask_with_optional_tools(
        self,
        *,
        system_text: str,
        user_text: str,
        sampled_params: dict[str, Any],
        extra_body: dict[str, Any],
        response_format: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        tool_runner: Callable[[str, dict[str, Any]], Any] | None,
        max_tool_rounds: int,
    ) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]

        tool_schemas = list(tools or [])
        tools_enabled = bool(tool_schemas)
        tool_round_budget = max(1, int(max_tool_rounds))
        tool_rounds_used = 0

        while True:
            request_kwargs = self._build_request_kwargs(
                messages=messages,
                sampled_params=sampled_params,
                extra_body=extra_body,
                response_format=response_format,
                tools=(tool_schemas if tools_enabled else None),
            )

            try:
                response = self.client.chat.completions.create(**request_kwargs)
            except Exception as e:
                if tools_enabled:
                    print(f"[llm][tools] disabled after API error: {e}")
                    tools_enabled = False
                    continue
                raise

            message = response.choices[0].message
            text = self._coerce_message_content(getattr(message, "content", ""))
            tool_calls = list(getattr(message, "tool_calls", []) or [])

            if not tools_enabled or not tool_calls:
                return text

            if tool_runner is None:
                raise RuntimeError(
                    "Model returned tool calls but no `tool_runner` was provided."
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": text or "",
                    "tool_calls": [self._tool_call_to_message(tc) for tc in tool_calls],
                }
            )

            for tool_call in tool_calls:
                messages.append(self._execute_tool_call(tool_call, tool_runner))

            tool_rounds_used += 1
            if tool_rounds_used >= tool_round_budget:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Tool-call budget reached. "
                            "Provide the best final answer now without additional tool calls."
                        ),
                    }
                )
                tools_enabled = False

    def _build_request_kwargs(
        self,
        *,
        messages: list[dict[str, Any]],
        sampled_params: dict[str, Any],
        extra_body: dict[str, Any],
        response_format: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            **sampled_params,
            "extra_body": extra_body,
        }
        if response_format is not None:
            request_kwargs["response_format"] = response_format
        if tools:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = "auto"
        return request_kwargs

    def _coerce_message_content(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text is not None:
                        parts.append(str(text))
                else:
                    parts.append(str(item))
            return "".join(parts)
        return str(content)

    def _tool_call_to_message(self, tool_call: Any) -> dict[str, Any]:
        function_obj = getattr(tool_call, "function", None)
        return {
            "id": str(getattr(tool_call, "id", "")),
            "type": "function",
            "function": {
                "name": str(getattr(function_obj, "name", "") or ""),
                "arguments": str(getattr(function_obj, "arguments", "") or "{}"),
            },
        }

    def _execute_tool_call(
        self,
        tool_call: Any,
        tool_runner: Callable[[str, dict[str, Any]], Any],
    ) -> dict[str, Any]:
        function_obj = getattr(tool_call, "function", None)
        tool_name = str(getattr(function_obj, "name", "") or "")
        raw_arguments = str(getattr(function_obj, "arguments", "") or "").strip()
        tool_call_id = str(getattr(tool_call, "id", "") or "")

        parsed_arguments: dict[str, Any] = {}
        if raw_arguments:
            try:
                loaded_args = json.loads(raw_arguments)
                if isinstance(loaded_args, dict):
                    parsed_arguments = loaded_args
                else:
                    parsed_arguments = {"_args": loaded_args}
            except Exception as e:
                parsed_arguments = {
                    "_invalid_arguments": raw_arguments,
                    "_parse_error": str(e),
                }

        print(f"[llm][tools] call={tool_name}")
        try:
            output = tool_runner(tool_name, parsed_arguments)
        except Exception as e:
            output = {
                "error": f"Tool execution failed for '{tool_name}': {e}",
                "arguments": parsed_arguments,
            }

        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": self._serialize_tool_output(output),
        }

    def _serialize_tool_output(self, output: Any) -> str:
        if output is None:
            return "{}"
        if isinstance(output, str):
            return output
        try:
            return json.dumps(output, ensure_ascii=False)
        except Exception:
            return str(output)

    def embed(self, text: str) -> list[float]:
        """Generate embedding vector for a single text.

        Args:
            text: Input text to embed
        """
        start = time.perf_counter()

        response = self.embed_client.embeddings.create(
            model=self.embed_model,
            input=text,
            encoding_format="float",
        )
        embedding = response.data[0].embedding

        if self.trace_latency:
            elapsed = time.perf_counter() - start
            dim = len(embedding)
            print(f"[embed] elapsed={elapsed:.2f}s dim={dim}")

        return embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed
        """
        if not texts:
            return []

        start = time.perf_counter()

        # Keep embedding batches small because local llama-server embedding backends
        # can fail when a single request contains too many long inputs.
        batch_size = 8
        all_embeddings: list[list[float]] = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            try:
                response = self.embed_client.embeddings.create(
                    model=self.embed_model,
                    input=batch,
                    encoding_format="float",
                )
                batch_embeddings = sorted(response.data, key=lambda x: x.index)
                all_embeddings.extend([e.embedding for e in batch_embeddings])
            except Exception as e:
                # Fallback to one-by-one embedding so a single oversized request does
                # not fail the entire indexing pass.
                fallback_failed = False
                for text in batch:
                    try:
                        response = self.embed_client.embeddings.create(
                            model=self.embed_model,
                            input=text,
                            encoding_format="float",
                        )
                        all_embeddings.append(response.data[0].embedding)
                    except Exception as fallback_error:
                        fallback_failed = True
                        raise RuntimeError(
                            "Embedding request failed. "
                            f"endpoint={self.embed_base_url}, model={self.embed_model}. "
                            "If llama-server is separate for embeddings, run with --embeddings "
                            "and pass --embed-port (and optionally --embed-host)."
                        ) from fallback_error

                if fallback_failed:
                    raise RuntimeError(
                        "Embedding batch request failed. "
                        f"endpoint={self.embed_base_url}, model={self.embed_model}"
                    ) from e

        if self.trace_latency:
            elapsed = time.perf_counter() - start
            print(f"[embed_batch] elapsed={elapsed:.2f}s count={len(texts)}")

        return all_embeddings



    def _consume_stream(
        self, stream_iter: Any, stream_label: str = "", filter_think: bool = False
    ) -> str:
        """Consume streaming response and return combined text."""
        collected_text: list[str] = []
        reasoning_chunks: list[str] = []
        in_think_block = False
        think_buffer = ""

        prefix = f"[{stream_label}] " if stream_label else ""
        print(f"[stream] {prefix}start")

        for chunk in stream_iter:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                collected_text.append(content)

                # Filter out <think> blocks during streaming if requested
                if filter_think:
                    think_buffer += content
                    # Process buffer to extract non-think content
                    while True:
                        if in_think_block:
                            end_idx = think_buffer.find("</think>")
                            if end_idx != -1:
                                think_buffer = think_buffer[end_idx + 8 :]
                                in_think_block = False
                            else:
                                break
                        else:
                            start_idx = think_buffer.find("<think>")
                            if start_idx != -1:
                                print(think_buffer[:start_idx], end="", flush=True)
                                think_buffer = think_buffer[start_idx + 7 :]
                                in_think_block = True
                            else:
                                # Keep potential partial tag in buffer
                                safe_end = max(0, len(think_buffer) - 7)
                                print(think_buffer[:safe_end], end="", flush=True)
                                think_buffer = think_buffer[safe_end:]
                                break
                else:
                    print(content, end="", flush=True)

            if self.stream_reasoning:
                reasoning_text = getattr(delta, "reasoning_content", None)
                if reasoning_text:
                    reasoning_chunks.append(reasoning_text)
                    print(reasoning_text, end="", flush=True)

        # Flush remaining buffer
        if filter_think and think_buffer and not in_think_block:
            print(think_buffer, end="", flush=True)

        print("\n[stream] end")

        combined = "".join(collected_text)
        if self.stream_reasoning and reasoning_chunks and "<think>" not in combined:
            combined = f"<think>{''.join(reasoning_chunks)}</think>\n{combined}"
        return combined

    def _extract_json(self, text: str) -> dict[str, Any]:
        """Extract JSON object from text that may contain thinking tags or markdown."""
        cleaned = self._sanitize_json_text(text)
        candidates = self._build_json_candidates(cleaned)

        last_error: json.JSONDecodeError | None = None
        for candidate in candidates:
            try:
                parsed = self._parse_json_candidate(candidate)
                if isinstance(parsed, dict):
                    return parsed
                raise json.JSONDecodeError("JSON payload is not an object", candidate, 0)
            except json.JSONDecodeError as e:
                last_error = e

        if last_error is not None:
            raise last_error
        raise json.JSONDecodeError("No JSON object found", cleaned, 0)

    def _sanitize_json_text(self, text: str) -> str:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        return text.strip()

    def _build_json_candidates(self, text: str) -> list[str]:
        candidates: list[str] = []

        if text:
            candidates.append(text)

        code_block = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if code_block:
            candidates.append(code_block.group(1).strip())

        balanced = self._first_balanced_object(text)
        if balanced:
            candidates.append(balanced)

        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = candidate.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        return deduped

    def _first_balanced_object(self, text: str) -> str:
        start_idx = text.find("{")
        if start_idx == -1:
            return ""

        depth = 0
        for idx, char in enumerate(text[start_idx:], start=start_idx):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start_idx : idx + 1]
        return ""

    def _parse_json_candidate(self, candidate: str) -> Any:
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            relaxed = re.sub(r",\s*([}\]])", r"\1", candidate)
            return json.loads(relaxed)
