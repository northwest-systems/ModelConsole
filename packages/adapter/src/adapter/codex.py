"""Codex CLI backend adapter for the mcon system runtime."""

from __future__ import annotations

import json
import os
import urllib.parse
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from .anthropic import AdapterError, RawAdapterResponse

# Codex CLI の標準 model。MCON_CODEX_MODEL または boot --model で上書きする。
DEFAULT_CODEX_MODEL = "gpt-5.3-codex"

# Claude Code compatibility 用に models API へ見せる仮 model 名。
CLAUDE_PROXY_MODEL = "claude-sonnet-4-6"

# Codex/OpenAI 認証に使う API key 環境変数名。
ENV_OPENAI_API_KEY = "OPENAI_API_KEY"

# Codex CLI へ渡す model override の環境変数名。
ENV_CODEX_MODEL = "MCON_CODEX_MODEL"


@dataclass
class SyntheticStreamResponse:
    """Small file-like response used when translating non-streaming output to SSE."""

    status: int
    headers: dict[str, str]
    body: bytes
    _offset: int = 0

    def __enter__(self) -> "SyntheticStreamResponse":
        """Enter the synthetic response context.

        Args:
            None.

        Returns:
            This response object.
        """

        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """Exit the synthetic response context.

        Args:
            exc_type: Exception type from the context, if any.
            exc_value: Exception value from the context, if any.
            traceback: Exception traceback from the context, if any.

        Returns:
            ``None``.
        """

        return None

    def read(self, size: int = -1) -> bytes:
        """Read bytes from the synthetic response body.

        Args:
            size: Maximum number of bytes to read, or ``-1`` for all remaining.

        Returns:
            Body bytes.
        """

        if self._offset >= len(self.body):
            return b""
        if size is None or size < 0:
            size = len(self.body) - self._offset
        chunk = self.body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class CodexAdapter:
    """Translate Anthropic Messages API calls to Codex CLI invocations."""

    backend_name = "codex"

    def __init__(self, credentials: dict[str, str] | None = None, default_model: str | None = None) -> None:
        """Initialize the Codex adapter.

        Args:
            credentials: Optional credential values loaded from the vault.
            default_model: Default Codex model id.

        Returns:
            ``None``.
        """

        self._credentials = credentials or {}
        self._default_model = default_model or DEFAULT_CODEX_MODEL

    def forward_json(
        self,
        request_headers: Any,
        request_payload: dict[str, Any],
        upstream_path: str = "/v1/messages",
    ) -> tuple[dict[str, Any], int]:
        """Forward an Anthropic Messages request to Codex CLI.

        Args:
            request_headers: Incoming request headers.
            request_payload: Anthropic-compatible JSON payload.
            upstream_path: Anthropic-compatible upstream path.

        Returns:
            Tuple of Anthropic-compatible response payload and HTTP status code.
        """

        if upstream_path == "/v1/messages/count_tokens":
            return self._request_input_tokens(request_payload)
        if upstream_path != "/v1/messages":
            raise AdapterError(501, "not_supported", f"Codex adapter does not implement {upstream_path}")
        if request_payload.get("tools"):
            raise AdapterError(501, "not_supported", "Codex CLI adapter does not support Anthropic tool use yet")

        started_at = time.time()
        prompt = _prompt_from_anthropic_request(request_payload)
        output = self._run_codex_prompt(prompt, request_payload)
        return _codex_text_to_anthropic_message(output, request_payload, started_at), 200

    def request_json(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        request_payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        """Handle non-message JSON endpoints implemented by this adapter.

        Args:
            method: HTTP method.
            request_headers: Incoming request headers.
            upstream_path: Anthropic-compatible upstream path.
            request_payload: Optional JSON payload.

        Returns:
            Tuple of response payload and HTTP status code.
        """

        if upstream_path.startswith("/v1/models"):
            return self._request_models(method, upstream_path)
        if upstream_path == "/v1/messages/count_tokens" and request_payload is not None:
            return self._request_input_tokens(request_payload)
        raise AdapterError(501, "not_supported", f"Codex adapter does not implement {upstream_path}")

    def request_raw(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        request_payload: dict[str, Any] | None = None,
    ) -> RawAdapterResponse:
        """Return a raw JSON response for compatible endpoints.

        Args:
            method: HTTP method.
            request_headers: Incoming request headers.
            upstream_path: Anthropic-compatible upstream path.
            request_payload: Optional JSON payload.

        Returns:
            Raw response metadata and body bytes.
        """

        response_payload, status_code = self.request_json(method, request_headers, upstream_path, request_payload)
        return RawAdapterResponse(
            status_code=status_code,
            headers={"content-type": "application/json"},
            body=json.dumps(response_payload, separators=(",", ":")).encode("utf-8"),
        )

    def request_raw_body(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        body: bytes | None,
    ) -> RawAdapterResponse:
        """Reject raw body requests because Codex CLI only supports prompt execution here.

        Args:
            method: HTTP method.
            request_headers: Incoming request headers.
            upstream_path: Requested upstream path.
            body: Raw request body.

        Returns:
            Never returns; raises ``AdapterError``.
        """

        raise AdapterError(501, "not_supported", f"Codex adapter does not implement raw body endpoint {upstream_path}")

    def open_stream(
        self,
        request_headers: Any,
        request_payload: dict[str, Any],
        upstream_path: str = "/v1/messages",
    ) -> SyntheticStreamResponse:
        """Convert a non-streaming Codex response into synthetic SSE.

        Args:
            request_headers: Incoming request headers.
            request_payload: Anthropic-compatible JSON payload.
            upstream_path: Anthropic-compatible upstream path.

        Returns:
            File-like synthetic SSE response.
        """

        payload_without_stream = dict(request_payload)
        payload_without_stream["stream"] = False
        response_payload, status_code = self.forward_json(request_headers, payload_without_stream, upstream_path)
        return SyntheticStreamResponse(
            status=status_code,
            headers={"content-type": "text/event-stream"},
            body=_anthropic_message_to_sse(response_payload),
        )

    def _request_models(self, method: str, upstream_path: str) -> tuple[dict[str, Any], int]:
        """Return Anthropic-shaped model metadata.

        Args:
            method: HTTP method.
            upstream_path: Model endpoint path.

        Returns:
            Tuple of model payload and HTTP status code.
        """

        if method != "GET":
            raise AdapterError(405, "method_not_allowed", "Models API only supports GET")
        requested_model = _requested_model_from_path(upstream_path)
        if requested_model:
            if requested_model.startswith("claude-"):
                return _anthropic_model(requested_model), 200
            return _anthropic_model(os.environ.get(ENV_CODEX_MODEL) or self._default_model), 200
        return _codex_models(os.environ.get(ENV_CODEX_MODEL) or self._default_model), 200

    def _request_input_tokens(self, request_payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """Estimate input token count for a prompt.

        Args:
            request_payload: Anthropic-compatible JSON payload.

        Returns:
            Tuple of token-count payload and HTTP status code.
        """

        return {"input_tokens": len(_prompt_from_anthropic_request(request_payload).split())}, 200

    def _run_codex_prompt(self, prompt: str, request_payload: dict[str, Any]) -> str:
        """Run ``codex exec`` for one prompt.

        Args:
            prompt: Prompt text to pass to Codex CLI.
            request_payload: Original request payload, reserved for future options.

        Returns:
            Final assistant text from Codex CLI.
        """

        model = os.environ.get(ENV_CODEX_MODEL) or self._default_model
        with tempfile.NamedTemporaryFile("r", encoding="utf-8", delete=True) as output_file:
            command = [
                "codex",
                "exec",
                "--model",
                model,
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--output-last-message",
                output_file.name,
                prompt,
            ]
            environment = dict(os.environ)
            credential_value = self._credentials.get(ENV_OPENAI_API_KEY)
            if credential_value:
                environment[ENV_OPENAI_API_KEY] = credential_value
            completed_process = subprocess.run(command, env=environment, text=True, capture_output=True, check=False, timeout=300)
            if completed_process.returncode != 0:
                message = completed_process.stderr.strip() or completed_process.stdout.strip() or "codex command failed"
                raise AdapterError(502, "upstream_error", message)
            output_file.seek(0)
            output = output_file.read().strip()
        return output or completed_process.stdout.strip()


def _anthropic_message_to_response_payload(
    request_payload: dict[str, Any],
    default_model: str = DEFAULT_CODEX_MODEL,
) -> dict[str, Any]:
    """Map the Anthropic Messages request shape into Responses API input."""

    output_model = os.environ.get(ENV_CODEX_MODEL) or _map_model_to_codex(request_payload.get("model"), default_model)
    response_payload: dict[str, Any] = {
        "model": output_model,
        "input": _convert_messages(request_payload.get("messages", [])),
    }

    instructions = _system_to_instructions(request_payload.get("system"))
    if instructions:
        response_payload["instructions"] = instructions
    if "max_tokens" in request_payload:
        response_payload["max_output_tokens"] = request_payload["max_tokens"]
    if "temperature" in request_payload:
        response_payload["temperature"] = request_payload["temperature"]
    if "top_p" in request_payload:
        response_payload["top_p"] = request_payload["top_p"]
    if request_payload.get("tools"):
        response_payload["tools"] = [_convert_tool(tool) for tool in request_payload["tools"]]
    if request_payload.get("tool_choice"):
        response_payload["tool_choice"] = _convert_tool_choice(request_payload["tool_choice"])
    return response_payload


def _convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic messages to Responses API input items.

    Args:
        messages: Anthropic message list.

    Returns:
        Responses API input item list.
    """

    input_items: list[dict[str, Any]] = []
    for message in messages:
        input_items.extend(_convert_message(message))
    return input_items


def _convert_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one Anthropic message to Responses API items.

    Args:
        message: Anthropic message object.

    Returns:
        Responses API input items for the message.
    """

    role = message.get("role", "user")
    content = message.get("content", "")
    if isinstance(content, str):
        return [{"role": role, "content": [{"type": _text_type_for_role(role), "text": content}]}]

    message_blocks: list[dict[str, Any]] = []
    input_items: list[dict[str, Any]] = []
    for block in content:
        block_type = block.get("type")
        if block_type == "text":
            message_blocks.append({"type": _text_type_for_role(role), "text": block.get("text", "")})
        elif block_type == "tool_result":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": block.get("tool_use_id", ""),
                    "output": _tool_result_to_text(block),
                }
            )
        elif block_type == "tool_use":
            input_items.append(
                {
                    "type": "function_call",
                    "call_id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                }
            )
    if message_blocks:
        input_items.insert(0, {"role": role, "content": message_blocks})
    if not input_items:
        input_items.append({"role": role, "content": [{"type": _text_type_for_role(role), "text": ""}]})
    return input_items


def _text_type_for_role(role: str) -> str:
    """Choose Responses API text block type for a role.

    Args:
        role: Anthropic message role.

    Returns:
        Responses API text block type.
    """

    if role == "assistant":
        return "output_text"
    return "input_text"


def _tool_result_to_text(block: dict[str, Any]) -> str:
    """Serialize an Anthropic tool result block.

    Args:
        block: Anthropic tool_result content block.

    Returns:
        Text payload for function_call_output.
    """

    content = block.get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _convert_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic tool schema to Responses API function schema.

    Args:
        tool: Anthropic tool definition.

    Returns:
        Responses API function tool definition.
    """

    return {
        "type": "function",
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
    }


def _convert_tool_choice(tool_choice: Any) -> Any:
    """Convert Anthropic tool_choice to Responses API shape.

    Args:
        tool_choice: Anthropic tool choice value.

    Returns:
        Responses API tool choice value.
    """

    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, dict):
        return "auto"
    if tool_choice.get("type") == "tool":
        return {"type": "function", "name": tool_choice.get("name")}
    return tool_choice.get("type", "auto")


def _system_to_instructions(system: Any) -> str | None:
    """Convert Anthropic system content to Responses API instructions.

    Args:
        system: Anthropic system field.

    Returns:
        Instruction text, or ``None``.
    """

    if system is None:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = [block.get("text", "") for block in system if block.get("type") == "text"]
        return "\n\n".join(part for part in parts if part)
    return str(system)


def _prompt_from_anthropic_request(request_payload: dict[str, Any]) -> str:
    """Build plain prompt text from an Anthropic request.

    Args:
        request_payload: Anthropic-compatible JSON payload.

    Returns:
        Prompt text for Codex CLI.
    """

    parts: list[str] = []
    system = request_payload.get("system")
    if system:
        parts.append("System:\n" + _content_to_text(system))
    for message in request_payload.get("messages", []):
        role = str(message.get("role", "user"))
        text = _content_to_text(message.get("content", ""))
        if text:
            parts.append(f"{role.title()}:\n{text}")
    return "\n\n".join(parts).strip()


def _content_to_text(content: Any) -> str:
    """Convert Anthropic content blocks to plain text.

    Args:
        content: String or list of Anthropic content blocks.

    Returns:
        Plain text representation.
    """

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                text_parts.append(json.dumps(block.get("content", ""), ensure_ascii=False))
        return "\n".join(part for part in text_parts if part)
    return str(content)


def _codex_text_to_anthropic_message(output: str, request_payload: dict[str, Any], started_at: float) -> dict[str, Any]:
    """Wrap Codex CLI text as an Anthropic message.

    Args:
        output: Codex CLI output text.
        request_payload: Original Anthropic-compatible request payload.
        started_at: Request start timestamp.

    Returns:
        Anthropic-compatible message payload.
    """

    input_tokens = len(_prompt_from_anthropic_request(request_payload).split())
    output_tokens = len(output.split())
    return {
        "id": f"msg_codex_{int(started_at * 1000)}",
        "type": "message",
        "role": "assistant",
        "model": request_payload.get("model", os.environ.get(ENV_CODEX_MODEL, DEFAULT_CODEX_MODEL)),
        "content": [{"type": "text", "text": output}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _codex_models(model_id: str | None = None) -> dict[str, Any]:
    """Build Anthropic-shaped Codex model list.

    Args:
        model_id: Optional selected model id.

    Returns:
        Anthropic-compatible models payload.
    """

    resolved_model_id = model_id or os.environ.get(ENV_CODEX_MODEL, DEFAULT_CODEX_MODEL)
    models = [_anthropic_model(CLAUDE_PROXY_MODEL)]
    if resolved_model_id != CLAUDE_PROXY_MODEL:
        models.append(_anthropic_model(resolved_model_id))
    return {
        "data": models,
        "has_more": False,
        "first_id": models[0]["id"],
        "last_id": models[-1]["id"],
    }


def _openai_response_to_anthropic_message(
    response_payload: dict[str, Any],
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    """Convert an OpenAI Responses payload to an Anthropic message.

    Args:
        response_payload: OpenAI Responses API payload.
        request_payload: Original Anthropic-compatible request payload.

    Returns:
        Anthropic-compatible message payload.
    """

    content = _convert_openai_output(response_payload.get("output", []))
    usage = response_payload.get("usage", {})
    return {
        "id": f"msg_{response_payload.get('id', 'codex')}",
        "type": "message",
        "role": "assistant",
        "model": request_payload.get("model", os.environ.get(ENV_CODEX_MODEL, DEFAULT_CODEX_MODEL)),
        "content": content,
        "stop_reason": _stop_reason(response_payload, content),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        },
    }


def _convert_openai_output(output_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI output items to Anthropic content blocks.

    Args:
        output_items: Responses API output item list.

    Returns:
        Anthropic content block list.
    """

    content: list[dict[str, Any]] = []
    for output_item in output_items:
        item_type = output_item.get("type")
        if item_type == "message":
            for block in output_item.get("content", []):
                text = block.get("text")
                if text:
                    content.append({"type": "text", "text": text})
        elif item_type == "function_call":
            content.append(
                {
                    "type": "tool_use",
                    "id": output_item.get("call_id") or output_item.get("id", "toolu_codex"),
                    "name": output_item.get("name", ""),
                    "input": _parse_arguments(output_item.get("arguments", "{}")),
                }
            )
    if not content:
        content.append({"type": "text", "text": response_text_fallback(output_items)})
    return content


def response_text_fallback(output_items: list[dict[str, Any]]) -> str:
    """Create fallback text when no text block is present.

    Args:
        output_items: Responses API output item list.

    Returns:
        JSON text fallback or an empty string.
    """

    for output_item in output_items:
        if output_item.get("type") == "message":
            return json.dumps(output_item.get("content", []), ensure_ascii=False)
    return ""


def _parse_arguments(arguments: Any) -> dict[str, Any]:
    """Parse tool call arguments into a dictionary.

    Args:
        arguments: JSON string, dictionary, or arbitrary value.

    Returns:
        Dictionary suitable for Anthropic tool_use input.
    """

    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(str(arguments))
    except json.JSONDecodeError:
        return {"value": arguments}
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


def _stop_reason(response_payload: dict[str, Any], content: list[dict[str, Any]]) -> str:
    """Map OpenAI response status/content to Anthropic stop_reason.

    Args:
        response_payload: OpenAI Responses API payload.
        content: Converted Anthropic content blocks.

    Returns:
        Anthropic stop reason.
    """

    if any(block.get("type") == "tool_use" for block in content):
        return "tool_use"
    if response_payload.get("status") == "incomplete":
        return "max_tokens"
    return "end_turn"


def _anthropic_message_to_sse(message_payload: dict[str, Any]) -> bytes:
    """Encode an Anthropic message as synthetic SSE events.

    Args:
        message_payload: Anthropic-compatible message payload.

    Returns:
        UTF-8 encoded SSE event stream.
    """

    events: list[tuple[str, dict[str, Any]]] = []
    message_start = dict(message_payload)
    message_start["content"] = []
    events.append(("message_start", {"type": "message_start", "message": message_start}))
    for index, block in enumerate(message_payload.get("content", [])):
        if block.get("type") == "text":
            events.append(("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}))
            events.append(("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": block.get("text", "")}}))
            events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
        elif block.get("type") == "tool_use":
            content_block = {key: value for key, value in block.items() if key != "input"}
            content_block["input"] = {}
            events.append(("content_block_start", {"type": "content_block_start", "index": index, "content_block": content_block}))
            events.append(("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block.get("input", {}), ensure_ascii=False)}}))
            events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    events.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": message_payload.get("stop_reason"), "stop_sequence": None}, "usage": message_payload.get("usage", {})}))
    events.append(("message_stop", {"type": "message_stop"}))
    return b"".join(_sse_event(event_name, event_payload) for event_name, event_payload in events)


def _sse_event(event_name: str, payload: dict[str, Any]) -> bytes:
    """Encode one SSE event.

    Args:
        event_name: SSE event name.
        payload: JSON-serializable event payload.

    Returns:
        UTF-8 encoded SSE event bytes.
    """

    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode(
        "utf-8"
    )


def _openai_models_to_anthropic_models(response_payload: dict[str, Any]) -> dict[str, Any]:
    """Convert OpenAI model list to Anthropic models payload.

    Args:
        response_payload: OpenAI models payload.

    Returns:
        Anthropic-compatible models payload.
    """

    models = [_anthropic_model(CLAUDE_PROXY_MODEL)]
    seen = {CLAUDE_PROXY_MODEL}
    for model in response_payload.get("data", []):
        model_id = model.get("id", "")
        if model_id and model_id not in seen:
            models.append(_anthropic_model(model_id))
            seen.add(model_id)
    return {"data": models, "has_more": False, "first_id": models[0]["id"] if models else None, "last_id": models[-1]["id"] if models else None}


def _openai_model_to_anthropic_model(response_payload: dict[str, Any], fallback_model_id: str) -> dict[str, Any]:
    """Convert one OpenAI model payload to an Anthropic model item.

    Args:
        response_payload: OpenAI model payload.
        fallback_model_id: Model id to use when payload has no id.

    Returns:
        Anthropic-compatible model item.
    """

    model_id = str(response_payload.get("id") or fallback_model_id)
    return _anthropic_model(model_id)


def _anthropic_model(model_id: str) -> dict[str, Any]:
    """Build one Anthropic-shaped model item.

    Args:
        model_id: Model identifier.

    Returns:
        Anthropic-compatible model item.
    """

    return {"id": model_id, "type": "model", "display_name": model_id, "created_at": None}


def _requested_model_from_path(upstream_path: str) -> str:
    """Extract a model id from a models endpoint path.

    Args:
        upstream_path: Requested upstream path.

    Returns:
        Decoded model id, or an empty string.
    """

    path = urllib.parse.urlsplit(upstream_path).path
    prefix = "/v1/models/"
    if not path.startswith(prefix):
        return ""
    return urllib.parse.unquote(path[len(prefix) :])


def _estimate_token_count(request_payload: dict[str, Any]) -> dict[str, int]:
    """Estimate token count from text length.

    Args:
        request_payload: Anthropic-compatible JSON payload.

    Returns:
        Token-count payload.
    """

    text_parts: list[str] = []
    system = _system_to_instructions(request_payload.get("system"))
    if system:
        text_parts.append(system)
    for message in request_payload.get("messages", []):
        content = message.get("content", "")
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
    estimated_tokens = max(1, sum(len(part) for part in text_parts) // 4)
    return {"input_tokens": estimated_tokens}


def _map_model_to_codex(model: Any, default_model: str = DEFAULT_CODEX_MODEL) -> str:
    """Map requested model names to a Codex-supported model id.

    Args:
        model: Requested model value.
        default_model: Fallback Codex model id.

    Returns:
        Selected Codex model id.
    """

    model_name = str(model or "")
    if model_name.startswith("gpt-") or model_name.startswith("o"):
        return model_name
    return default_model


ADAPTER_SPEC = {
    "name": "codex",
    "aliases": (),
    "adapter_class": CodexAdapter,
    "model_override": "codex_model",
}
