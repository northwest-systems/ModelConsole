"""LLM provider stream conversion.

The proxy contract follows Claude Code's print/SDK stream shape: top-level
messages such as assistant/system/result, and assistant content blocks such as
text/tool_use/tool_result. Provider-specific events are preserved as raw
``llm_event`` records, while common UI-visible data is emitted as small
canonical events.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def convert_provider_event(provider: str, raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one provider JSON event into ModelConsole LLM proxy events."""

    events: list[dict[str, Any]] = [
        {
            "type": "llm_event",
            "provider": provider,
            "message": raw,
        }
    ]
    events.extend(_convert_claude_code_event(provider, raw))
    if provider == "codex":
        events.extend(_convert_codex_event(provider, raw))
    return events


def _convert_claude_code_event(provider: str, raw: dict[str, Any]) -> Iterable[dict[str, Any]]:
    event_type = raw.get("type")
    if event_type == "assistant":
        message = raw.get("message") if isinstance(raw.get("message"), dict) else raw
        yield from _assistant_content_events(provider, message.get("content"))
        return
    if event_type == "result":
        yield {
            "type": "llm_result",
            "provider": provider,
            "subtype": raw.get("subtype"),
            "result": raw.get("result"),
            "usage": raw.get("usage"),
            "session_id": raw.get("session_id"),
            "duration_ms": raw.get("duration_ms"),
            "total_cost_usd": raw.get("total_cost_usd"),
        }
        return
    if event_type == "system":
        yield {
            "type": "llm_system",
            "provider": provider,
            "subtype": raw.get("subtype"),
            "session_id": raw.get("session_id"),
        }
        return
    if event_type == "user":
        yield {
            "type": "llm_user",
            "provider": provider,
            "message": raw.get("message", raw.get("content")),
        }
        return
    if event_type == "stream_event" and isinstance(raw.get("event"), dict):
        yield from _claude_partial_events(provider, raw["event"])


def _assistant_content_events(provider: str, content: Any) -> Iterable[dict[str, Any]]:
    if isinstance(content, str):
        yield _assistant_delta(provider, content)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            yield _assistant_delta(provider, block["text"])
        elif block_type == "tool_use":
            yield {
                "type": "llm_tool_call",
                "provider": provider,
                "id": block.get("id"),
                "name": block.get("name"),
                "input": block.get("input"),
            }
        elif block_type == "tool_result":
            yield {
                "type": "llm_tool_result",
                "provider": provider,
                "tool_use_id": block.get("tool_use_id"),
                "content": block.get("content"),
                "is_error": block.get("is_error"),
            }


def _claude_partial_events(provider: str, event: dict[str, Any]) -> Iterable[dict[str, Any]]:
    event_type = event.get("type")
    if event_type == "content_block_delta":
        delta = event.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
            yield _assistant_delta(provider, delta["text"])
    yield {
        "type": "llm_stream_event",
        "provider": provider,
        "event": event,
    }


def _convert_codex_event(provider: str, raw: dict[str, Any]) -> Iterable[dict[str, Any]]:
    if isinstance(raw.get("delta"), str):
        yield _assistant_delta(provider, raw["delta"])
        return
    event_type = raw.get("type")
    if event_type in {"agent_message", "assistant_message", "message"}:
        text = _first_string(raw, ("message", "content", "text"))
        if text is not None:
            yield _assistant_delta(provider, text)
        return
    item = raw.get("item")
    if isinstance(item, dict) and item.get("type") in {"agent_message", "assistant_message", "message"}:
        text = _first_string(item, ("text", "message", "content"))
        if text is not None:
            yield _assistant_delta(provider, text)


def _assistant_delta(provider: str, text: str) -> dict[str, Any]:
    return {
        "type": "assistant_delta",
        "provider": provider,
        "delta": text,
    }


def _first_string(mapping: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str):
            return value
    return None
