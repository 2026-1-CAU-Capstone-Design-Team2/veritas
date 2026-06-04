from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import httpx
from openai import OpenAI


DEFAULT_AUTOSURVEY_OPENAI_MODEL = "gpt-5-mini"
_SERVICE_TIERS = {"default", "flex", "priority"}
# Valid OpenAI reasoning-effort levels (gpt-5 family reasoning models).
_REASONING_EFFORTS = {"minimal", "low", "medium", "high"}


class OpenAIChatLLMClient:
    """Chat-only OpenAI API client for AutoSurvey generation.

    This adapter intentionally mirrors only the LLM surface AutoSurvey tools
    use. Embeddings stay on the local llama-server client and fail fast here if
    a caller wires the roles incorrectly.
    """

    SAMPLING_PARAMS = {
        "temperature": 1.0,
        "top_p": 0.95,
        "presence_penalty": 1.5,
    }
    JSON_SAMPLING_PARAMS = {
        "temperature": 0.0,
        "top_p": 0.15,
        "presence_penalty": 0.0,
    }

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_AUTOSURVEY_OPENAI_MODEL,
        n_ctx: int = 400_000,
        max_parallel: int = 2,
        service_tier: str | None = None,
        trace_latency: bool = True,
        stream_summary: bool = False,
        client: Any | None = None,
    ) -> None:
        self._api_key = str(api_key or "").strip()
        if client is None and not self._api_key:
            raise ValueError("OpenAIChatLLMClient requires an API key.")
        self._client = client
        self.model = str(model or DEFAULT_AUTOSURVEY_OPENAI_MODEL).strip()
        self.n_ctx = max(1, int(n_ctx))
        self.max_parallel = max(1, int(max_parallel))
        self.service_tier = self._normalize_service_tier(service_tier)
        self.trace_latency = bool(trace_latency)
        self.stream_summary = bool(stream_summary)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def map_parallel(
        self,
        items: list[Any],
        worker: Callable[[Any], Any],
        *,
        max_workers: int | None = None,
        label: str = "",
    ) -> list[Any]:
        count = len(items)
        if count == 0:
            return []
        workers = self.max_parallel if max_workers is None else max_workers
        workers = max(1, min(int(workers), count))
        if workers == 1:
            return [worker(item) for item in items]

        results: list[Any] = [None] * count
        errors: list[tuple[int, BaseException]] = []
        prefix = f"openai-map:{label}" if label else "openai-map"
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix) as executor:
            future_to_index = {
                executor.submit(worker, item): index
                for index, item in enumerate(items)
            }
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    results[index] = future.result()
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    errors.append((index, exc))
        if errors:
            errors.sort(key=lambda pair: pair[0])
            raise errors[0][1]
        return results

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
        """``reasoning_effort`` ("minimal"/"low"/"medium"/"high") caps how many
        reasoning tokens a gpt-5-family model spends before answering. Lower
        effort = faster + cheaper; use "low" for extraction-style calls and
        leave the model default (medium) for synthesis-style calls. Ignored
        for non-reasoning models."""
        del extra_sampling_params
        start = time.perf_counter()

        sampled_params = {**self.SAMPLING_PARAMS, **(sampling_params or {})}
        system_text = str(system_prompt or "").strip()
        if force_json:
            system_text = (
                f"{system_text}\n"
                "Return a strict JSON object only. "
                "Do not include markdown fences, commentary, or extra wrapper text."
            )

        if stream and tools:
            print(
                "[openai][tools] stream=True is not supported with tool calls; "
                "falling back to non-stream mode"
            )
            stream = False

        if stream:
            request_kwargs = self._build_request_kwargs(
                messages=[
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": str(user_prompt or "")},
                ],
                sampled_params=sampled_params,
                response_format=None,
                tools=None,
                timeout_sec=timeout_sec,
                reasoning_effort=reasoning_effort,
            )
            request_kwargs["stream"] = True
            chunks = self._create_completion(request_kwargs)
            text = self._consume_stream(chunks, stream_label=stream_label)
        else:
            text = self._ask_with_optional_tools(
                system_text=system_text,
                user_text=str(user_prompt or ""),
                sampled_params=sampled_params,
                response_format=response_format,
                tools=tools,
                tool_runner=tool_runner,
                max_tool_rounds=max_tool_rounds,
                timeout_sec=timeout_sec,
                reasoning_effort=reasoning_effort,
            )

        if self.trace_latency:
            elapsed = time.perf_counter() - start
            tag = f" [{stream_label}]" if stream_label else ""
            mode = "reasoning" if reasoning else "plain"
            print(f"[openai][llm]{tag} model={self.model} mode={mode} elapsed={elapsed:.2f}s")

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
        timeout_sec: float | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        prefer_response_format = True
        for attempt in range(max_retries + 1):
            attempt_no = attempt + 1
            stage = "first-attempt" if attempt == 0 else "retry"
            tag = f" [{stream_label}]" if stream_label else ""
            print(f"[openai][json]{tag} {stage}={attempt_no}/{max_retries + 1}")

            try:
                text = self.ask(
                    system_prompt,
                    user_prompt,
                    reasoning=reasoning,
                    stream=stream,
                    stream_label=stream_label,
                    sampling_params=self.JSON_SAMPLING_PARAMS,
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
                    reasoning_effort=reasoning_effort,
                )
            except Exception as exc:  # noqa: BLE001 - retries preserve current behavior
                last_error = exc
                print(
                    f"[openai][json]{tag} request-failed "
                    f"attempt={attempt_no}/{max_retries + 1} error={exc}"
                )
                if prefer_response_format and not stream:
                    prefer_response_format = False
                    print(
                        f"[openai][json]{tag} response_format-fallback "
                        f"reason={exc}"
                    )
                    if attempt < max_retries:
                        continue
                if attempt < max_retries:
                    continue
                break

            try:
                return self._extract_json(text)
            except json.JSONDecodeError as exc:
                last_error = exc
                print(
                    f"[openai][json]{tag} parse-failed "
                    f"attempt={attempt_no}/{max_retries + 1} error={exc}"
                )
                if attempt < max_retries:
                    continue

        if isinstance(last_error, Exception):
            raise last_error
        raise json.JSONDecodeError("Failed to parse JSON", "", 0)

    def embed(self, _text: str) -> list[float]:
        raise RuntimeError(
            "OpenAIChatLLMClient is chat-only. Use the local embedding LLM "
            "client for embeddings."
        )

    def embed_batch(self, _texts: list[str]) -> list[list[float]]:
        raise RuntimeError(
            "OpenAIChatLLMClient is chat-only. Use the local embedding LLM "
            "client for embeddings."
        )

    def _ask_with_optional_tools(
        self,
        *,
        system_text: str,
        user_text: str,
        sampled_params: dict[str, Any],
        response_format: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        tool_runner: Callable[[str, dict[str, Any]], Any] | None,
        max_tool_rounds: int,
        timeout_sec: float | None = None,
        reasoning_effort: str | None = None,
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
                response_format=response_format,
                tools=(tool_schemas if tools_enabled else None),
                timeout_sec=timeout_sec,
                reasoning_effort=reasoning_effort,
            )

            try:
                label = "with-tools" if tools_enabled else "plain"
                print(
                    "[openai][request] "
                    f"model={self.model} mode={label} timeout={timeout_sec} "
                    f"tools={len(tool_schemas) if tools_enabled else 0} "
                    f"messages={len(messages)}"
                )
                response = self._create_completion(request_kwargs)
                print("[openai][response] received")
            except Exception as exc:
                if tools_enabled:
                    print(f"[openai][tools] disabled after API error: {exc}")
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

    def _build_request_kwargs(
        self,
        *,
        messages: list[dict[str, Any]],
        sampled_params: dict[str, Any],
        response_format: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        timeout_sec: float | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            **self._clean_sampling_params(sampled_params),
        }
        if response_format is not None:
            request_kwargs["response_format"] = response_format
        if tools:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = "auto"
        if self.service_tier:
            request_kwargs["service_tier"] = self.service_tier
        effort = self._normalize_reasoning_effort(reasoning_effort)
        if effort and self._supports_reasoning_effort():
            request_kwargs["reasoning_effort"] = effort
        if timeout_sec is not None:
            request_kwargs["timeout"] = self._request_timeout(timeout_sec)
        return request_kwargs

    def _normalize_service_tier(self, service_tier: str | None) -> str | None:
        tier = str(service_tier or "").strip().lower()
        if not tier or tier == "auto":
            return None
        if tier not in _SERVICE_TIERS:
            raise ValueError(
                "OpenAI service_tier must be one of: auto, default, flex, priority."
            )
        return tier

    def _normalize_reasoning_effort(self, reasoning_effort: str | None) -> str | None:
        """Validate a per-call reasoning effort. Invalid values are dropped with
        a warning (the model then uses its own default) — a typo must degrade
        to default behavior, never crash a survey mid-run."""
        effort = str(reasoning_effort or "").strip().lower()
        if not effort:
            return None
        if effort not in _REASONING_EFFORTS:
            print(
                "[openai][reasoning-effort] "
                f"invalid effort={effort!r}; expected one of {sorted(_REASONING_EFFORTS)}. "
                "Using the model default."
            )
            return None
        return effort

    def _supports_reasoning_effort(self) -> bool:
        """gpt-5 family models are reasoning models and accept reasoning_effort.
        Older chat models (gpt-4o family) reject the parameter, so it is never
        sent for them."""
        return self._uses_fixed_sampling_defaults()

    def _create_completion(self, request_kwargs: dict[str, Any]) -> Any:
        try:
            return self.client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            # Optional request fields are dropped (one at a time) when the API
            # rejects them, so a model/tier mismatch degrades instead of failing
            # the survey. reasoning_effort falls back first, then service_tier.
            if "reasoning_effort" in request_kwargs and self._is_reasoning_effort_error(exc):
                fallback_kwargs = dict(request_kwargs)
                effort = fallback_kwargs.pop("reasoning_effort", "")
                print(
                    "[openai][reasoning-effort] "
                    f"effort={effort} rejected; retrying without it"
                )
                return self._create_completion(fallback_kwargs)
            if "service_tier" not in request_kwargs or not self._is_service_tier_error(exc):
                raise
            fallback_kwargs = dict(request_kwargs)
            tier = fallback_kwargs.pop("service_tier", "")
            print(
                "[openai][service-tier] "
                f"tier={tier} rejected; retrying with default project tier"
            )
            return self.client.chat.completions.create(**fallback_kwargs)

    def _is_service_tier_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "service_tier" in text or "service tier" in text

    def _is_reasoning_effort_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "reasoning_effort" in text or "reasoning effort" in text

    def _clean_sampling_params(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._uses_fixed_sampling_defaults():
            allowed = {
                "max_tokens",
                "max_completion_tokens",
            }
        else:
            allowed = {
                "temperature",
                "top_p",
                "presence_penalty",
                "frequency_penalty",
                "max_tokens",
                "max_completion_tokens",
            }
        return {
            key: value
            for key, value in params.items()
            if key in allowed and value is not None
        }

    def _uses_fixed_sampling_defaults(self) -> bool:
        normalized = self.model.strip().lower()
        return (
            normalized == "gpt-5"
            or normalized.startswith("gpt-5-")
            or normalized.startswith("gpt-5.")
        )

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

    def _consume_stream(self, stream_iter: Any, stream_label: str = "") -> str:
        collected: list[str] = []
        prefix = f"[{stream_label}] " if stream_label else ""
        print(f"[openai][stream] {prefix}start")
        for chunk in stream_iter:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None) or ""
            if content:
                collected.append(content)
                print(content, end="", flush=True)
        print("\n[openai][stream] end")
        return "".join(collected)

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

    def _tool_call_to_message(self, tool_call: Any) -> dict[str, Any]:
        function_obj = getattr(tool_call, "function", None)
        return {
            "id": str(getattr(tool_call, "id", "") or ""),
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
            except Exception as exc:
                parsed_arguments = {
                    "_invalid_arguments": raw_arguments,
                    "_parse_error": str(exc),
                }

        print(f"[openai][tools] call={tool_name}")
        try:
            output = tool_runner(tool_name, parsed_arguments)
        except Exception as exc:
            output = {
                "error": f"Tool execution failed for '{tool_name}': {exc}",
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

    def _extract_json(self, text: str) -> dict[str, Any]:
        cleaned = self._sanitize_json_text(text)
        candidates = self._build_json_candidates(cleaned)

        last_error: json.JSONDecodeError | None = None
        for candidate in candidates:
            try:
                parsed = self._parse_json_candidate(candidate)
                if isinstance(parsed, dict):
                    return parsed
                raise json.JSONDecodeError("JSON payload is not an object", candidate, 0)
            except json.JSONDecodeError as exc:
                last_error = exc

        if last_error is not None:
            raise last_error
        raise json.JSONDecodeError("No JSON object found", cleaned, 0)

    def _sanitize_json_text(self, text: str) -> str:
        text = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.DOTALL)
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
