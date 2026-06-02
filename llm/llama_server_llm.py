"""LLM Client for llama-server with OpenAI-compatible API.

Supports both chat completions and embeddings via /v1/chat/completions
and /v1/embeddings endpoints.
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterator

import httpx
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
    TOOL_DECISION_SAMPLING_PARAMS = {
        "temperature": 0.0,
        "top_p": 0.2,
        "presence_penalty": 0.0,
    }
    TOOL_DECISION_EXTRA_SAMPLING_PARAMS = {
        "top_k": 5,
        "min_p": 0.0,
        "repeat_penalty": 1.0,
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
        max_parallel: int | None = None,
    ):
        # Chat completions client
        self.chat_base_url = f"http://{host}:{port}/v1"
        self._chat_host = host
        self._chat_port = port
        self.client = OpenAI(base_url=self.chat_base_url, api_key="sk-no-key-required")
        # Detect the served model id + the server's context window so downstream
        # tools can size their input budgets to the model instead of guessing.
        self.refresh_model_info()

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
            # Defer detection if the embedding server is not up yet (e.g. it
            # failed to load, or we are constructing the runtime to switch away
            # from a model that won't start) — see refresh_model_info.
            try:
                embed_models = self.embed_client.models.list().data
                self.embed_model = embed_models[0].id if embed_models else ""
            except Exception as exc:  # noqa: BLE001 - server not ready yet
                self.embed_model = ""
                print(f"[llm] embedding model detection deferred (server not ready): {exc}")
        else:
            self.embed_client = self.client
            self.embed_model = self.model

        self.stream_summary = stream_summary
        self.stream_reasoning = stream_reasoning
        self.trace_latency = trace_latency

        # Degree of concurrency for batch LLM work (per-document cleanup /
        # summarize, embedding batches). ``1`` = fully serial and is the
        # default, so behavior is identical to the historical single-threaded
        # path unless parallel decoding is explicitly enabled. The value is
        # meant to match llama-server's ``-np`` slot count. When not passed
        # explicitly it is read once from ``VERITAS_LLM_PARALLEL`` — a single
        # env var that turns parallel decoding on at every entry point (CLI,
        # API, tests) without any per-call-site configuration.
        if max_parallel is None:
            try:
                max_parallel = int(os.getenv("VERITAS_LLM_PARALLEL", "1") or "1")
            except ValueError:
                max_parallel = 1
        self.max_parallel = max(1, int(max_parallel))

    def refresh_model_info(self) -> None:
        """(Re)detect the served model id + context window from the server.

        Called at construction and again after a live model swap (the
        llama-server was restarted with a different GGUF on the same
        host:port). Mutating in place keeps this client's object identity
        stable, so every tool / service already holding a reference picks up
        the new model + n_ctx without any rewiring.

        Tolerant of the server not being up yet: if the selected model failed
        to load (e.g. OOM) or we are constructing the runtime to switch away
        from a broken model, detection is *deferred* (model id left blank)
        instead of raising — so ``AgentRuntime`` can still be built and a model
        switch can recover. It is called again after the server (re)starts.
        """
        try:
            models = self.client.models.list().data
        except Exception as exc:  # noqa: BLE001 - server not ready yet
            self.model = getattr(self, "model", "") or ""
            self.n_ctx = self._detect_n_ctx(self._chat_host, self._chat_port)
            print(f"[llm] model detection deferred (server not ready): {exc}")
            return
        if not models:
            self.model = ""
            self.n_ctx = self._detect_n_ctx(self._chat_host, self._chat_port)
            print("[llm] server reported no models yet; detection deferred")
            return
        self.model = models[0].id
        self.n_ctx = self._detect_n_ctx(self._chat_host, self._chat_port)
        print(f"[llm] model={self.model} n_ctx={self.n_ctx}")

    def map_parallel(
        self,
        items: list[Any],
        worker: Callable[[Any], Any],
        *,
        max_workers: int | None = None,
        label: str = "",
    ) -> list[Any]:
        """Apply ``worker`` to every item, returning results in input order.

        This is the single primitive every batch LLM loop (per-document
        cleanup / summarize, embedding batches) routes through, so enabling
        parallel decoding is a one-knob change rather than a per-call-site mode:

        * ``max_parallel == 1`` (the default) runs the items **serially in the
          calling thread** — same order, same exception propagation, no thread
          pool created — so the behavior is identical to the historical
          ``for`` loop.
        * ``max_parallel > 1`` fans the items out across a
          :class:`~concurrent.futures.ThreadPoolExecutor`. Threads (not async)
          are deliberate: each worker spends virtually all of its wall time
          blocked on a llama-server HTTP round-trip, and CPython releases the
          GIL across that socket I/O, so threads yield real concurrency that
          maps directly onto llama-server's ``-np`` slots — without rewriting
          the whole synchronous call graph as async.

        Results are always returned in input order regardless of completion
        order. If one or more workers raise, the earliest (lowest input index)
        exception is re-raised once the rest settle, mirroring how a serial
        loop fails on the first bad item, so existing ``try/except`` blocks
        around call sites keep working unchanged.
        """
        count = len(items)
        if count == 0:
            return []
        workers = self.max_parallel if max_workers is None else max_workers
        workers = max(1, min(int(workers), count))
        if workers == 1:
            return [worker(item) for item in items]

        results: list[Any] = [None] * count
        errors: list[tuple[int, BaseException]] = []
        prefix = f"llm-map:{label}" if label else "llm-map"
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix) as executor:
            future_to_index = {
                executor.submit(worker, item): index
                for index, item in enumerate(items)
            }
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    results[index] = future.result()
                except BaseException as exc:  # noqa: BLE001 - faithfully re-raised below
                    errors.append((index, exc))
        if errors:
            errors.sort(key=lambda pair: pair[0])
            raise errors[0][1]
        return results

    def _detect_n_ctx(self, host: str, port: int, *, default: int = 8192) -> int:
        """Query llama-server /props for the context window size, in tokens.

        Falls back to a conservative default when the endpoint is unavailable
        (e.g. a non-llama-server OpenAI-compatible backend).
        """
        try:
            response = httpx.get(f"http://{host}:{port}/props", timeout=5.0)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return default

        generation_settings = payload.get("default_generation_settings") or {}
        for value in (payload.get("n_ctx"), generation_settings.get("n_ctx")):
            try:
                n_ctx = int(value)
            except (TypeError, ValueError):
                continue
            if n_ctx > 0:
                return n_ctx
        return default

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
        timeout_sec: float | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Generate a chat completion response.

        ``reasoning_effort`` is accepted for interface compatibility with
        :class:`llm.openai_chat_llm.OpenAIChatLLMClient` (AutoSurvey tools pass
        it unconditionally) and ignored — local llama-server models have no
        effort dial; reasoning on/off is the ``reasoning`` flag (/think tag).
        """
        del reasoning_effort
        think_tag = "/think" if reasoning else "/no_think"
        start = time.perf_counter()

        sampled_params = {**self.SAMPLING_PARAMS, **(sampling_params or {})}
        sampled_extra = {**self.EXTRA_SAMPLING_PARAMS, **(extra_sampling_params or {})}
        system_text = system_prompt.strip()
        if force_json:
            if reasoning:
                system_text = (
                    f"{system_text}\n"
                    "Think privately if needed, then put a strict JSON object in the final answer. "
                    "Do not include markdown fences, commentary, or extra wrapper text after thinking."
                )
            else:
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
            stream_kwargs = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": f"{think_tag}\n{user_prompt}"},
                ],
                "stream": True,
                **sampled_params,
                "extra_body": extra_body,
            }
            if timeout_sec is not None:
                stream_kwargs["timeout"] = self._request_timeout(timeout_sec)

            chunks = self.client.chat.completions.create(**stream_kwargs)
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
                timeout_sec=timeout_sec,
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

    def iter_ask(
        self,
        system_prompt: str,
        user_prompt: str,
        reasoning: bool = False,
        *,
        stream_label: str = "",
        sampling_params: dict[str, Any] | None = None,
        extra_sampling_params: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> Iterator[str]:
        """Stream a chat completion as visible chunks.

        Yields one chunk per delta with <think> blocks filtered out when
        reasoning is disabled. The caller is responsible for accumulating the
        full text and writing it to history.
        """
        think_tag = "/think" if reasoning else "/no_think"
        sampled_params = {**self.SAMPLING_PARAMS, **(sampling_params or {})}
        sampled_extra = {**self.EXTRA_SAMPLING_PARAMS, **(extra_sampling_params or {})}
        extra_body = {
            **sampled_extra,
            "enable_thinking": reasoning,
            "enable_reasoning": reasoning,
            "chat_template_kwargs": {
                "enable_thinking": reasoning,
                "enable_reasoning": reasoning,
            },
        }
        stream_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt.strip()},
                {"role": "user", "content": f"{think_tag}\n{user_prompt}"},
            ],
            "stream": True,
            **sampled_params,
            "extra_body": extra_body,
        }
        if timeout_sec is not None:
            stream_kwargs["timeout"] = self._request_timeout(timeout_sec)

        start = time.perf_counter()
        prefix = f"[{stream_label}] " if stream_label else ""
        print(f"[stream-iter] {prefix}start")

        chunks = self.client.chat.completions.create(**stream_kwargs)
        filter_think = not reasoning
        buffer = ""
        in_think = False
        total = 0

        for chunk in chunks:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None) or ""
            if not content:
                continue
            if not filter_think:
                total += len(content)
                yield content
                continue
            buffer += content
            while True:
                if in_think:
                    end_idx = buffer.find("</think>")
                    if end_idx != -1:
                        buffer = buffer[end_idx + 8 :]
                        in_think = False
                        continue
                    break
                start_idx = buffer.find("<think>")
                if start_idx != -1:
                    visible = buffer[:start_idx]
                    if visible:
                        total += len(visible)
                        yield visible
                    buffer = buffer[start_idx + 7 :]
                    in_think = True
                    continue
                # Keep potential partial open tag in buffer
                safe_end = max(0, len(buffer) - 7)
                visible = buffer[:safe_end]
                if visible:
                    total += len(visible)
                    yield visible
                buffer = buffer[safe_end:]
                break

        if filter_think and buffer and not in_think:
            total += len(buffer)
            yield buffer
        elif filter_think and in_think and total == 0:
            # Stream ended inside an unclosed <think> with nothing visible emitted
            # — the model ignored /no_think and spent its whole budget reasoning.
            # Surfacing the reasoning text beats returning an empty / broken result.
            leftover = buffer.strip()
            if leftover:
                total += len(leftover)
                yield leftover

        if self.trace_latency:
            elapsed = time.perf_counter() - start
            print(f"[stream-iter] {prefix}end chars={total} elapsed={elapsed:.2f}s")

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
        timeout_sec: float | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Generate a chat completion and parse as JSON.

        ``reasoning_effort`` is accepted for interface compatibility with the
        OpenAI adapter and ignored (see :meth:`ask`).
        """
        del reasoning_effort
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
                        if (prefer_response_format and not reasoning and not stream and not tools)
                        else None
                    ),
                    tools=tools,
                    tool_runner=tool_runner,
                    max_tool_rounds=max_tool_rounds,
                    timeout_sec=timeout_sec,
                )
            except Exception as e:
                last_error = e
                print(
                    f"[llm][json]{tag} request-failed "
                    f"attempt={attempt_no}/{max_retries + 1} error={e}"
                )
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

    def collect_tool_outputs(
        self,
        system_prompt: str,
        user_prompt: str,
        reasoning: bool = False,
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_runner: Callable[[str, dict[str, Any]], Any] | None = None,
        max_tool_calls: int = 1,
        stream_label: str = "",
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        """Ask the model whether to call tools and return executed tool outputs.

        This intentionally does not ask the model for the final answer after tool
        execution. Callers can use the returned tool outputs as context for a
        normal streaming answer.
        """
        think_tag = "/think" if reasoning else "/no_think"
        start = time.perf_counter()

        tool_schemas = list(tools or [])
        if not tool_schemas:
            return {"content": "", "tool_outputs": []}
        if tool_runner is None:
            raise RuntimeError("`tool_runner` is required when tools are provided.")

        sampled_params = dict(self.TOOL_DECISION_SAMPLING_PARAMS)
        sampled_extra = dict(self.TOOL_DECISION_EXTRA_SAMPLING_PARAMS)
        extra_body = {
            **sampled_extra,
            "enable_thinking": reasoning,
            "enable_reasoning": reasoning,
            "chat_template_kwargs": {
                "enable_thinking": reasoning,
                "enable_reasoning": reasoning,
            },
        }

        messages = [
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": f"{think_tag}\n{user_prompt}"},
        ]
        request_kwargs = self._build_request_kwargs(
            messages=messages,
            sampled_params=sampled_params,
            extra_body=extra_body,
            response_format=None,
            tools=tool_schemas,
            timeout_sec=timeout_sec,
        )

        response = self.client.chat.completions.create(**request_kwargs)
        message = response.choices[0].message
        text = self._coerce_message_content(getattr(message, "content", ""))
        tool_calls = list(getattr(message, "tool_calls", []) or [])

        tool_outputs: list[dict[str, Any]] = []
        call_budget = max(1, int(max_tool_calls))
        for tool_call in tool_calls[:call_budget]:
            tool_message = self._execute_tool_call(tool_call, tool_runner)
            tool_outputs.append(
                {
                    "name": str(tool_message.get("name") or ""),
                    "content": str(tool_message.get("content") or ""),
                }
            )

        if self.trace_latency:
            elapsed = time.perf_counter() - start
            tag = f" [{stream_label}]" if stream_label else ""
            mode = "think" if reasoning else "no_think"
            print(
                f"[llm][tools-decision]{tag} mode={mode} "
                f"calls={len(tool_outputs)} elapsed={elapsed:.2f}s"
            )

        if not reasoning:
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

        return {"content": text.strip(), "tool_outputs": tool_outputs}

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
        timeout_sec: float | None = None,
    ) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]

        tool_schemas = list(tools or [])
        tools_enabled = bool(tool_schemas)
        tool_round_budget = max(1, int(max_tool_rounds))
        tool_rounds_used = 0
        last_tool_outputs: list[str] = []

        while True:
            request_kwargs = self._build_request_kwargs(
                messages=messages,
                sampled_params=sampled_params,
                extra_body=extra_body,
                response_format=response_format,
                tools=(tool_schemas if tools_enabled else None),
                timeout_sec=timeout_sec,
            )

            try:
                label = "with-tools" if tools_enabled else "plain"
                print(
                    "[llm][request] "
                    f"mode={label} timeout={timeout_sec} "
                    f"tools={len(tool_schemas) if tools_enabled else 0} "
                    f"messages={len(messages)}"
                )
                response = self.client.chat.completions.create(**request_kwargs)
                print("[llm][response] received")
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
                if not text.strip() and tool_rounds_used and last_tool_outputs:
                    fallback_text = self._fallback_text_from_tool_outputs(last_tool_outputs)
                    if fallback_text:
                        return fallback_text
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
                tool_message = self._execute_tool_call(tool_call, tool_runner)
                messages.append(tool_message)
                last_tool_outputs.append(str(tool_message.get("content") or ""))

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

    def _fallback_text_from_tool_outputs(self, tool_outputs: list[str]) -> str:
        for raw_output in reversed(tool_outputs):
            text = str(raw_output or "").strip()
            if not text:
                continue

            try:
                payload = json.loads(text)
            except Exception:
                return text

            if isinstance(payload, dict):
                for key in ("answer", "content", "result"):
                    value = payload.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
                return json.dumps(payload, ensure_ascii=False)

            if isinstance(payload, str) and payload.strip():
                return payload.strip()

        return ""

    def _build_request_kwargs(
        self,
        *,
        messages: list[dict[str, Any]],
        sampled_params: dict[str, Any],
        extra_body: dict[str, Any],
        response_format: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        timeout_sec: float | None = None,
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
        if timeout_sec is not None:
            request_kwargs["timeout"] = self._request_timeout(timeout_sec)
        return request_kwargs

    def _request_timeout(self, timeout_sec: float) -> httpx.Timeout:
        timeout = max(float(timeout_sec), 1.0)
        short_timeout = min(timeout, 10.0)
        return httpx.Timeout(
            timeout=timeout,
            connect=short_timeout,
            read=timeout,
            write=short_timeout,
            pool=short_timeout,
        )

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

    def check_embedding_endpoint(self) -> None:
        """Fail fast if the configured embedding endpoint cannot embed text."""
        try:
            self.embed("veritas embedding health check")
        except Exception as exc:
            raise RuntimeError(
                "Embedding endpoint is not usable. "
                f"endpoint={self.embed_base_url}, model={self.embed_model}. "
                f"Underlying error: {type(exc).__name__}: {exc}. "
                "If this is llama-server, start the embedding server with "
                "--embeddings."
            ) from exc

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed
        """
        if not texts:
            return []

        start = time.perf_counter()

        # Keep embedding batches small because local llama-server embedding
        # backends can fail when a single request contains too many long
        # inputs. Each fixed-size batch is one HTTP request; the batches
        # themselves are dispatched through ``map_parallel`` so that, when
        # parallel decoding is enabled (and the embedding server is started
        # with ``-np > 1``), independent batches are embedded concurrently.
        # With ``max_parallel == 1`` this collapses back to the previous
        # sequential loop.
        batch_size = 8
        batches = [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]

        def _embed_one_batch(batch: list[str]) -> list[list[float]]:
            try:
                response = self.embed_client.embeddings.create(
                    model=self.embed_model,
                    input=batch,
                    encoding_format="float",
                )
                ordered = sorted(response.data, key=lambda item: item.index)
                return [item.embedding for item in ordered]
            except Exception as e:
                # Fallback to one-by-one embedding so a single oversized request
                # does not fail the entire indexing pass.
                batch_embeddings: list[list[float]] = []
                for text in batch:
                    try:
                        response = self.embed_client.embeddings.create(
                            model=self.embed_model,
                            input=text,
                            encoding_format="float",
                        )
                        batch_embeddings.append(response.data[0].embedding)
                    except Exception as fallback_error:
                        # Surface the underlying server error verbatim — the
                        # generic wrapper alone hides the actual cause (e.g. a
                        # 500 "input is too large to process. increase the
                        # physical batch size" when the embedding server's
                        # -b / -ub is smaller than the longest input).
                        raise RuntimeError(
                            "Embedding request failed. "
                            f"endpoint={self.embed_base_url}, model={self.embed_model}. "
                            f"Underlying error: {type(fallback_error).__name__}: {fallback_error}. "
                            "If a separate embedding server is used, confirm it is started "
                            "with --embeddings and that its physical batch size (-b / -ub) "
                            "is large enough for the longest input chunk."
                        ) from fallback_error
                return batch_embeddings

        batch_results = self.map_parallel(batches, _embed_one_batch, label="embed")
        all_embeddings: list[list[float]] = []
        for batch_embeddings in batch_results:
            all_embeddings.extend(batch_embeddings)

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
        printed_any = False

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
                                visible = think_buffer[:start_idx]
                                if visible:
                                    print(visible, end="", flush=True)
                                    printed_any = True
                                think_buffer = think_buffer[start_idx + 7 :]
                                in_think_block = True
                            else:
                                # Keep potential partial tag in buffer
                                safe_end = max(0, len(think_buffer) - 7)
                                visible = think_buffer[:safe_end]
                                if visible:
                                    print(visible, end="", flush=True)
                                    printed_any = True
                                think_buffer = think_buffer[safe_end:]
                                break
                else:
                    print(content, end="", flush=True)
                    printed_any = True

            reasoning_text = getattr(delta, "reasoning_content", None)
            if reasoning_text:
                reasoning_chunks.append(reasoning_text)
                if self.stream_reasoning:
                    print(reasoning_text, end="", flush=True)
                    printed_any = True

        # Flush remaining buffer
        if filter_think and think_buffer and not in_think_block:
            print(think_buffer, end="", flush=True)
            printed_any = True

        combined = "".join(collected_text)
        if not printed_any:
            fallback_text = self._visible_stream_fallback(
                combined=combined,
                reasoning_chunks=reasoning_chunks,
            )
            if fallback_text:
                print(fallback_text, end="", flush=True)
                combined = fallback_text

        print("\n[stream] end")

        if self.stream_reasoning and reasoning_chunks and "<think>" not in combined:
            combined = f"<think>{''.join(reasoning_chunks)}</think>\n{combined}"
        return combined

    def _visible_stream_fallback(
        self,
        *,
        combined: str,
        reasoning_chunks: list[str],
    ) -> str:
        text = str(combined or "").strip()
        if not text and reasoning_chunks:
            text = "".join(reasoning_chunks).strip()
        if not text:
            return ""

        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"^```(?:markdown|text)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
        return text

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
