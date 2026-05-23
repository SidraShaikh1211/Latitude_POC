"""Anthropic Claude client wrapper.

Centralizes:
  - structured-output calls (Pydantic-validated)
  - agent loops (tool-calling, bounded iterations)
  - prompt caching (cache_control on system + tools + criterion text)
  - usage / cost tracking via the returned `Usage` object

All calls share one Anthropic AsyncClient. Tool-call result format and
cache_control quirks are isolated here so callers see only `await
client.structured_output(...)` or `await client.agent_loop(...)`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from anthropic import AsyncAnthropic
from anthropic.types import (
    MessageParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlock,
)
from pydantic import BaseModel, ValidationError

from app.settings import settings


T = TypeVar("T", bound=BaseModel)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cost_usd += other.cost_usd


# Pricing for Claude Sonnet 4.5/4.6 as of late 2025 (USD per million tokens).
# Cache reads are ~90% cheaper than fresh input; cache creation is ~25% more
# expensive than fresh input. Update when the actual model id is confirmed.
_PRICING = {
    "input_per_million": 3.0,
    "output_per_million": 15.0,
    "cache_read_per_million": 0.30,
    "cache_creation_per_million": 3.75,
}


def _estimate_cost(usage: Usage) -> float:
    p = _PRICING
    return (
        usage.input_tokens * p["input_per_million"] / 1_000_000
        + usage.output_tokens * p["output_per_million"] / 1_000_000
        + usage.cache_read_tokens * p["cache_read_per_million"] / 1_000_000
        + usage.cache_creation_tokens * p["cache_creation_per_million"] / 1_000_000
    )


def _usage_from_response(resp: Any) -> Usage:
    u = getattr(resp, "usage", None)
    if u is None:
        return Usage()
    usage = Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )
    usage.cost_usd = _estimate_cost(usage)
    return usage


# ---------------------------------------------------------------------------


@dataclass
class StructuredResult:
    parsed: BaseModel | dict
    usage: Usage
    raw_text: str = ""


@dataclass
class AgentTrace:
    tool_calls: list[dict] = field(default_factory=list)
    final_text: str = ""
    iterations: int = 0
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""


class AnthropicClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key or settings.anthropic_api_key
        self._model = model or settings.anthropic_model
        self._client = AsyncAnthropic(api_key=self._api_key, max_retries=max_retries)

    @property
    def model(self) -> str:
        return self._model

    async def health_check(self) -> tuple[bool, str]:
        """One-line probe: is the configured model reachable?"""
        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=8,
                messages=[{"role": "user", "content": "ok"}],
            )
            text = ""
            for block in resp.content:
                if getattr(block, "type", "") == "text":
                    text = block.text
                    break
            return True, f"model={self._model} response={text!r}"
        except Exception as e:
            return False, f"model={self._model} error={e!s}"

    # ------------------------------------------------------------------
    # Structured output (one-shot, JSON-mode via tool-use coercion)
    # ------------------------------------------------------------------

    async def structured_output(
        self,
        *,
        system: str | list[TextBlockParam],
        user: str,
        schema: type[T],
        max_tokens: int = 4096,
        cache_system: bool = True,
    ) -> StructuredResult:
        """Ask Claude to return JSON matching `schema`. Uses tool-use under the
        hood to enforce JSON output (more reliable than free-form JSON requests).
        """
        tool_name = f"return_{schema.__name__.lower()}"
        json_schema = schema.model_json_schema()

        tool_def: ToolParam = {
            "name": tool_name,
            "description": f"Return a {schema.__name__} object.",
            "input_schema": json_schema,
        }

        system_blocks = self._system_blocks(system, cache=cache_system)

        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system_blocks,
            tools=[tool_def],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user}],
        )
        usage = _usage_from_response(resp)

        tool_input: dict | None = None
        raw_text = ""
        for block in resp.content:
            btype = getattr(block, "type", "")
            if btype == "tool_use":
                tool_input = block.input  # type: ignore[attr-defined]
            elif btype == "text":
                raw_text = block.text  # type: ignore[attr-defined]

        if tool_input is None:
            raise RuntimeError(
                f"structured_output: model did not call the {tool_name} tool"
            )

        try:
            parsed = schema.model_validate(tool_input)
        except ValidationError as e:
            # Anthropic sometimes returns nested arrays as JSON-encoded strings.
            # Try one pass of coercion before failing.
            coerced = _coerce_nested_strings(tool_input)
            try:
                parsed = schema.model_validate(coerced)
            except ValidationError:
                raise RuntimeError(
                    f"structured_output: schema validation failed: {e}\n  input={tool_input!r}"
                ) from e

        return StructuredResult(parsed=parsed, usage=usage, raw_text=raw_text)

    # ------------------------------------------------------------------
    # Agent loop (tool-calling, bounded iterations)
    # ------------------------------------------------------------------

    async def agent_loop(
        self,
        *,
        system: str | list[TextBlockParam],
        user: str,
        tools: list[ToolParam],
        tool_handler: Callable[[str, dict], Awaitable[Any]],
        max_iterations: int = 5,
        max_tokens: int = 4096,
        cache_system: bool = True,
        cache_tools: bool = True,
        final_tool_name: str | None = None,
    ) -> AgentTrace:
        """Run a tool-calling agent loop.

        `tool_handler` is called with (tool_name, tool_input_dict) and must
        return JSON-serializable output. The loop continues until the model
        stops calling tools, `final_tool_name` is called (treated as the
        terminal action), or `max_iterations` is reached.
        """
        if cache_tools and tools:
            tools = self._cache_tools(tools)

        system_blocks = self._system_blocks(system, cache=cache_system)
        messages: list[MessageParam] = [{"role": "user", "content": user}]

        trace = AgentTrace()

        for iteration in range(max_iterations):
            trace.iterations = iteration + 1
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system_blocks,
                tools=tools,
                messages=messages,
            )
            trace.usage.add(_usage_from_response(resp))
            trace.stop_reason = resp.stop_reason or ""

            tool_uses: list[ToolUseBlock] = []
            text_parts: list[str] = []
            for block in resp.content:
                if getattr(block, "type", "") == "tool_use":
                    tool_uses.append(block)  # type: ignore[arg-type]
                elif getattr(block, "type", "") == "text":
                    text_parts.append(block.text)  # type: ignore[attr-defined]

            if not tool_uses:
                trace.final_text = "\n".join(text_parts).strip()
                return trace

            assistant_content = [c.model_dump() for c in resp.content]
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results: list[ToolResultBlockParam] = []
            for tu in tool_uses:
                tool_name = tu.name
                tool_input = tu.input or {}
                try:
                    output = await tool_handler(tool_name, tool_input)
                    result_text = json.dumps(output, default=str)
                    is_error = False
                except Exception as e:
                    result_text = json.dumps({"error": str(e)})
                    is_error = True
                trace.tool_calls.append({
                    "iteration": iteration + 1,
                    "name": tool_name,
                    "input": tool_input,
                    "output": result_text,
                    "is_error": is_error,
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result_text,
                    "is_error": is_error,
                })
                if final_tool_name and tool_name == final_tool_name:
                    trace.final_text = "\n".join(text_parts).strip()
                    return trace

            messages.append({"role": "user", "content": tool_results})

        trace.final_text = "(max_iterations reached)"
        return trace

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _system_blocks(
        self, system: str | list[TextBlockParam], cache: bool
    ) -> list[TextBlockParam]:
        if isinstance(system, str):
            block: TextBlockParam = {"type": "text", "text": system}
            if cache and len(system) > 800:  # rough proxy for >~1024 tokens
                block["cache_control"] = {"type": "ephemeral"}
            return [block]
        # Already a list — assume caller set cache_control where needed
        return system

    def _cache_tools(self, tools: list[ToolParam]) -> list[ToolParam]:
        """Mark the last tool with cache_control so the whole tools block is
        cached. Anthropic prompt-caching applies the cache to everything up to
        and including the marked block."""
        if not tools:
            return tools
        out = [dict(t) for t in tools]
        out[-1]["cache_control"] = {"type": "ephemeral"}
        return out  # type: ignore[return-value]


def _coerce_nested_strings(value: Any) -> Any:
    """Recursively look for string fields that contain JSON and decode them.

    Anthropic's tool-use structured output occasionally returns a list field
    as a JSON-encoded string (the entire `[{...}, {...}]` wrapped in quotes).
    This walks the dict and attempts json.loads() on any string that looks
    like a JSON array or object.
    """
    if isinstance(value, dict):
        return {k: _coerce_nested_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_nested_strings(v) for v in value]
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(("[", "{")):
            try:
                return _coerce_nested_strings(json.loads(value))
            except (json.JSONDecodeError, ValueError):
                return value
    return value


_client: AnthropicClient | None = None


def get_client() -> AnthropicClient:
    """Lazy singleton, so tests can override via env vars before first use."""
    global _client
    if _client is None:
        _client = AnthropicClient()
    return _client


def reset_client() -> None:
    global _client
    _client = None
