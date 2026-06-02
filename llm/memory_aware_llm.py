"""LLM 진입 wrapper — memory engaged(call*) + raw passthrough(ask*)."""

from __future__ import annotations

from typing import Any, Callable, Iterator

from core.memory.request import CallRequest
from services.memory_tools_funcs.llm_tools import MEMORY_TOOL_SCHEMAS, build_memory_tool_runner
from services.memory_tools_funcs.runtime import MemoryRuntime


class MemoryAwareLLMClient:
    """raw_llm + memory_runtime 합류 wrapper."""

    def __init__(self, *, raw_llm, memory_runtime: MemoryRuntime) -> None:
        self.raw = raw_llm
        self.memory_runtime = memory_runtime

    def _memory_tools(self, req: CallRequest):
        """req.enable_memory_tools=True일 때 (schemas, runner) 반환. 아니면 (None, None)."""
        if not req.enable_memory_tools:
            return None, None
        return MEMORY_TOOL_SCHEMAS, build_memory_tool_runner(self.memory_runtime)

    # ── memory-engaged API ──────────────────────────────────────────

    def call(self, req: CallRequest) -> str:
        """prepare → raw.chat → commit."""
        prepared = self.memory_runtime.prepare(req)
        tools, tool_runner = self._memory_tools(req)
        text = self.raw.chat(
            prepared.messages,
            reasoning=False,
            stream_label=prepared.stream_label,
            sampling_params=prepared.sampling_params,
            extra_sampling_params=prepared.extra_sampling_params,
            timeout_sec=prepared.timeout_sec,
            force_json=prepared.constraints.json_strict,
            tools=tools,
            tool_runner=tool_runner,
        )
        self.memory_runtime.commit(prepared, text)
        return text

    def iter_call(self, req: CallRequest) -> Iterator[str]:
        """prepare → raw.iter_chat → commit (streaming). 스트리밍 중 tool 호출은 raw가 비-stream으로 폴백."""
        prepared = self.memory_runtime.prepare(req)
        tools, tool_runner = self._memory_tools(req)
        if tools:
            text = self.raw.chat(
                prepared.messages,
                reasoning=False,
                stream=False,
                stream_label=prepared.stream_label,
                sampling_params=prepared.sampling_params,
                extra_sampling_params=prepared.extra_sampling_params,
                timeout_sec=prepared.timeout_sec,
                force_json=prepared.constraints.json_strict,
                tools=tools,
                tool_runner=tool_runner,
            )
            self.memory_runtime.commit(prepared, text)
            if text:
                yield text
            return

        chunks: list[str] = []
        for chunk in self.raw.iter_chat(
            prepared.messages,
            reasoning=False,
            stream_label=prepared.stream_label,
            sampling_params=prepared.sampling_params,
            extra_sampling_params=prepared.extra_sampling_params,
            timeout_sec=prepared.timeout_sec,
        ):
            chunks.append(chunk)
            yield chunk
        self.memory_runtime.commit(prepared, "".join(chunks))

    def call_json(self, req: CallRequest) -> dict[str, Any]:
        """prepare → raw.chat_json → commit."""
        prepared = self.memory_runtime.prepare(req)
        tools, tool_runner = self._memory_tools(req)
        result = self.raw.chat_json(
            prepared.messages,
            reasoning=False,
            stream_label=prepared.stream_label,
            timeout_sec=prepared.timeout_sec,
            tools=tools,
            tool_runner=tool_runner,
        )
        self.memory_runtime.commit(prepared, str(result))
        return result

    # ── legacy passthrough (memory NOT engaged) ────────────────────
    # 마이그레이션 안 된 기존 tool들이 그대로 동작하도록 raw_llm으로 직접 forward.

    def ask(self, *args, **kwargs) -> str:
        return self.raw.ask(*args, **kwargs)

    def iter_ask(self, *args, **kwargs) -> Iterator[str]:
        return self.raw.iter_ask(*args, **kwargs)

    def ask_json(self, *args, **kwargs) -> dict[str, Any]:
        return self.raw.ask_json(*args, **kwargs)

    def collect_tool_outputs(self, *args, **kwargs) -> dict[str, Any]:
        return self.raw.collect_tool_outputs(*args, **kwargs)

    # ── embed / model info / 기타 pass-through ─────────────────────

    def embed(self, text: str) -> list[float]:
        return self.raw.embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return self.raw.embed_batch(texts)

    def check_embedding_endpoint(self) -> None:
        return self.raw.check_embedding_endpoint()

    def refresh_model_info(self) -> None:
        """raw에서 모델 정보 갱신 후 memory_runtime의 n_ctx도 갱신."""
        result = self.raw.refresh_model_info()
        try:
            self.memory_runtime.update_n_ctx(int(getattr(self.raw, "n_ctx", 0) or 0))
        except Exception:
            pass
        try:
            self.memory_runtime.token_counter.reset_remote()
        except Exception:
            pass
        return result

    def map_parallel(
        self,
        items: list[Any],
        worker: Callable[[Any], Any],
        *,
        max_workers: int | None = None,
        label: str = "",
    ) -> list[Any]:
        return self.raw.map_parallel(items, worker, max_workers=max_workers, label=label)

    @property
    def model(self) -> str:
        return self.raw.model

    @property
    def n_ctx(self) -> int:
        return self.raw.n_ctx

    @property
    def max_parallel(self) -> int:
        return self.raw.max_parallel

    @max_parallel.setter
    def max_parallel(self, value: int) -> None:
        self.raw.max_parallel = value
