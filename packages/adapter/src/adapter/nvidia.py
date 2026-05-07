"""NVIDIA NIM backend adapter for the mcon system runtime."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .anthropic import AdapterError, RawAdapterResponse
from .codex import SyntheticStreamResponse, _anthropic_message_to_sse

# NVIDIA NIM/OpenAI-compatible API の標準 endpoint。
DEFAULT_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com"

# NVIDIA backend の標準 model。MCON_NVIDIA_MODEL または boot --model で上書きする。
DEFAULT_NVIDIA_MODEL = "nvidia/llama-3.3-nemotron-super-49b-v1.5"

# Claude Code compatibility 用に models API へ見せる仮 model 名。
CLAUDE_PROXY_MODEL = "claude-sonnet-4-6"

# NVIDIA NIM API key を読む環境変数名。
ENV_NVIDIA_API_KEY = "NVIDIA_API_KEY"

# NVIDIA NGC 互換 key 名。
ENV_NGC_API_KEY = "NGC_API_KEY"

# NVIDIA compatible endpoint を差し替える環境変数名。
ENV_NVIDIA_BASE_URL = "MCON_NVIDIA_BASE_URL"

# NVIDIA model override の環境変数名。
ENV_NVIDIA_MODEL = "MCON_NVIDIA_MODEL"


class NvidiaNimAdapter:
    """Translate Anthropic Messages API calls to NVIDIA NIM Chat Completions."""

    backend_name = "nvidia"

    def __init__(self, credentials: dict[str, str] | None = None, default_model: str | None = None) -> None:
        """Initialize the NVIDIA NIM adapter.

        Args:
            credentials: Optional credential values loaded from the vault.
            default_model: Default NVIDIA model id.

        Returns:
            ``None``.
        """

        self._credentials = credentials or {}
        self._default_model = default_model or DEFAULT_NVIDIA_MODEL

    def forward_json(
        self,
        request_headers: Any,
        request_payload: dict[str, Any],
        upstream_path: str = "/v1/messages",
    ) -> tuple[dict[str, Any], int]:
        """Forward an Anthropic Messages request to NVIDIA NIM.

        Args:
            request_headers: Incoming request headers.
            request_payload: Anthropic-compatible JSON payload.
            upstream_path: Anthropic-compatible upstream path.

        Returns:
            Tuple of Anthropic-compatible response payload and HTTP status code.
        """

        if upstream_path == "/v1/messages/count_tokens":
            return {"input_tokens": _estimate_input_tokens(request_payload)}, 200
        if upstream_path != "/v1/messages":
            raise AdapterError(501, "not_supported", f"NVIDIA NIM adapter does not implement {upstream_path}")

        chat_payload = _anthropic_message_to_chat_payload(request_payload, self._default_model)
        raw_response = self._request_nvidia_json("POST", "/v1/chat/completions", chat_payload)
        response_payload = json.loads(raw_response.body.decode("utf-8"))
        return _chat_completion_to_anthropic_message(response_payload, request_payload), raw_response.status_code

    def request_json(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        request_payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        """Handle model and token-count endpoints for NVIDIA NIM.

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
            return {"input_tokens": _estimate_input_tokens(request_payload)}, 200
        raise AdapterError(501, "not_supported", f"NVIDIA NIM adapter does not implement {upstream_path}")

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
        """Reject raw body requests because this adapter only maps JSON calls.

        Args:
            method: HTTP method.
            request_headers: Incoming request headers.
            upstream_path: Requested upstream path.
            body: Raw request body.

        Returns:
            Never returns; raises ``AdapterError``.
        """

        raise AdapterError(501, "not_supported", f"NVIDIA NIM adapter does not implement raw body endpoint {upstream_path}")

    def open_stream(
        self,
        request_headers: Any,
        request_payload: dict[str, Any],
        upstream_path: str = "/v1/messages",
    ) -> SyntheticStreamResponse:
        """Convert a non-streaming NVIDIA response into synthetic SSE.

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
        """Fetch model metadata from NVIDIA NIM and normalize it.

        Args:
            method: HTTP method.
            upstream_path: Model endpoint path.

        Returns:
            Tuple of model payload and HTTP status code.
        """

        if method != "GET":
            raise AdapterError(405, "method_not_allowed", "Models API only supports GET")
        requested_model = _requested_model_from_path(upstream_path)
        if requested_model and requested_model.startswith("claude-"):
            return _anthropic_model(requested_model), 200
        raw_response = self._request_nvidia_json("GET", upstream_path)
        response_payload = json.loads(raw_response.body.decode("utf-8"))
        if requested_model:
            return _openai_model_to_anthropic_model(response_payload, requested_model), raw_response.status_code
        return _openai_models_to_anthropic_models(response_payload), raw_response.status_code

    def _request_nvidia_json(
        self,
        method: str,
        upstream_path: str,
        request_payload: dict[str, Any] | None = None,
    ) -> RawAdapterResponse:
        """Call the NVIDIA NIM JSON API.

        Args:
            method: HTTP method.
            upstream_path: NVIDIA API path.
            request_payload: Optional JSON payload.

        Returns:
            Raw response metadata and body bytes.
        """

        body = None
        if request_payload is not None:
            body = json.dumps(request_payload).encode("utf-8")
        upstream_request = self._build_request(method, upstream_path, body)
        try:
            with urllib.request.urlopen(upstream_request, timeout=120) as response:
                return RawAdapterResponse(
                    status_code=int(response.status),
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read(),
                )
        except urllib.error.HTTPError as http_error:
            response_body = http_error.read().decode("utf-8")
            try:
                response_payload = json.loads(response_body)
                error_payload = response_payload.get("error", {})
                message = str(error_payload.get("message", response_body))
            except json.JSONDecodeError:
                message = response_body
            raise AdapterError(int(http_error.code), "upstream_error", message) from http_error
        except urllib.error.URLError as url_error:
            raise AdapterError(502, "upstream_unavailable", str(url_error.reason)) from url_error
        except TimeoutError as timeout_error:
            raise AdapterError(504, "upstream_timeout", "NVIDIA NIM upstream request timed out") from timeout_error

    def _build_request(self, method: str, upstream_path: str, body: bytes | None) -> urllib.request.Request:
        """Build a NVIDIA NIM upstream request with auth headers.

        Args:
            method: HTTP method.
            upstream_path: NVIDIA API path.
            body: Encoded request body.

        Returns:
            Fully constructed urllib request.
        """

        api_key = (
            self._credentials.get(ENV_NVIDIA_API_KEY)
            or self._credentials.get(ENV_NGC_API_KEY)
            or os.environ.get(ENV_NVIDIA_API_KEY)
            or os.environ.get(ENV_NGC_API_KEY)
        )
        if not api_key:
            raise AdapterError(503, "configuration_error", f"{ENV_NVIDIA_API_KEY} is not set")

        base_url = (os.environ.get(ENV_NVIDIA_BASE_URL) or DEFAULT_NVIDIA_BASE_URL).rstrip("/")
        upstream_url = f"{base_url}{upstream_path}"
        return urllib.request.Request(
            upstream_url,
            data=body,
            method=method,
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "authorization": f"Bearer {api_key}",
            },
        )


def _anthropic_message_to_chat_payload(request_payload: dict[str, Any], default_model: str) -> dict[str, Any]:
    """Convert an Anthropic request to OpenAI chat completions payload.

    Args:
        request_payload: Anthropic-compatible JSON payload.
        default_model: Fallback NVIDIA model id.

    Returns:
        OpenAI-compatible chat completions payload.
    """

    chat_payload: dict[str, Any] = {
        "model": os.environ.get(ENV_NVIDIA_MODEL) or _map_model_to_nvidia(request_payload.get("model"), default_model),
        "messages": _convert_messages(request_payload),
        "stream": False,
    }
    if "max_tokens" in request_payload:
        chat_payload["max_tokens"] = request_payload["max_tokens"]
    if "temperature" in request_payload:
        chat_payload["temperature"] = request_payload["temperature"]
    if "top_p" in request_payload:
        chat_payload["top_p"] = request_payload["top_p"]
    if request_payload.get("stop_sequences"):
        chat_payload["stop"] = request_payload["stop_sequences"]
    if request_payload.get("tools"):
        chat_payload["tools"] = [{"type": "function", "function": _convert_tool(tool)} for tool in request_payload["tools"]]
    if request_payload.get("tool_choice"):
        chat_payload["tool_choice"] = _convert_tool_choice(request_payload["tool_choice"])
    return chat_payload


def _convert_messages(request_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert Anthropic messages to OpenAI chat messages.

    Args:
        request_payload: Anthropic-compatible JSON payload.

    Returns:
        OpenAI-compatible chat message list.
    """

    messages: list[dict[str, Any]] = []
    system_text = _content_to_text(request_payload.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})
    for message in request_payload.get("messages", []):
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if _contains_tool_use(content):
            messages.append({"role": "assistant", "content": _content_to_text(content), "tool_calls": _tool_calls_from_content(content)})
        elif _contains_tool_result(content):
            messages.extend(_tool_results_from_content(content))
        else:
            messages.append({"role": role, "content": _content_to_text(content)})
    return messages


def _content_to_text(content: Any) -> str:
    """Convert Anthropic content blocks to plain text.

    Args:
        content: String or list of Anthropic content blocks.

    Returns:
        Plain text representation.
    """

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                parts.append(str(block.get("text", "")))
            elif block_type == "tool_result":
                parts.append(json.dumps(block.get("content", ""), ensure_ascii=False))
        return "\n".join(part for part in parts if part)
    return str(content)


def _contains_tool_use(content: Any) -> bool:
    """Check whether Anthropic content contains tool_use blocks.

    Args:
        content: Anthropic content value.

    Returns:
        ``True`` when a tool_use block is present.
    """

    return isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content)


def _contains_tool_result(content: Any) -> bool:
    """Check whether Anthropic content contains tool_result blocks.

    Args:
        content: Anthropic content value.

    Returns:
        ``True`` when a tool_result block is present.
    """

    return isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)


def _tool_calls_from_content(content: Any) -> list[dict[str, Any]]:
    """Convert Anthropic tool_use blocks to OpenAI tool_calls.

    Args:
        content: Anthropic content value.

    Returns:
        OpenAI-compatible tool_call list.
    """

    if not isinstance(content, list):
        return []
    tool_calls = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                }
            )
    return tool_calls


def _tool_results_from_content(content: Any) -> list[dict[str, Any]]:
    """Convert Anthropic tool_result blocks to OpenAI tool messages.

    Args:
        content: Anthropic content value.

    Returns:
        OpenAI-compatible tool result messages.
    """

    if not isinstance(content, list):
        return []
    messages = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": _content_to_text(block.get("content", "")),
                }
            )
    return messages


def _convert_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic tool schema to OpenAI function schema.

    Args:
        tool: Anthropic tool definition.

    Returns:
        OpenAI-compatible function schema.
    """

    return {
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
    }


def _convert_tool_choice(tool_choice: Any) -> Any:
    """Convert Anthropic tool_choice to OpenAI chat shape.

    Args:
        tool_choice: Anthropic tool choice value.

    Returns:
        OpenAI-compatible tool choice value.
    """

    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, dict):
        return "auto"
    if tool_choice.get("type") == "tool":
        return {"type": "function", "function": {"name": tool_choice.get("name", "")}}
    return tool_choice.get("type", "auto")


def _chat_completion_to_anthropic_message(
    response_payload: dict[str, Any],
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    """Convert OpenAI chat completion response to Anthropic message.

    Args:
        response_payload: OpenAI-compatible chat completion payload.
        request_payload: Original Anthropic-compatible request payload.

    Returns:
        Anthropic-compatible message payload.
    """

    choice = (response_payload.get("choices") or [{}])[0]
    message = choice.get("message", {})
    content = _message_to_content_blocks(message)
    usage = response_payload.get("usage", {})
    return {
        "id": f"msg_{response_payload.get('id', 'nvidia')}",
        "type": "message",
        "role": "assistant",
        "model": response_payload.get("model", request_payload.get("model", DEFAULT_NVIDIA_MODEL)),
        "content": content,
        "stop_reason": _finish_reason_to_stop_reason(choice.get("finish_reason"), content),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _message_to_content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one OpenAI chat message to Anthropic content blocks.

    Args:
        message: OpenAI-compatible chat message.

    Returns:
        Anthropic content block list.
    """

    content: list[dict[str, Any]] = []
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": text})
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function", {})
        content.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id", "toolu_nvidia"),
                "name": function.get("name", ""),
                "input": _parse_arguments(function.get("arguments", "{}")),
            }
        )
    if not content:
        content.append({"type": "text", "text": ""})
    return content


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


def _finish_reason_to_stop_reason(finish_reason: Any, content: list[dict[str, Any]]) -> str:
    """Map OpenAI finish_reason to Anthropic stop_reason.

    Args:
        finish_reason: OpenAI finish_reason value.
        content: Converted Anthropic content blocks.

    Returns:
        Anthropic stop reason.
    """

    if any(block.get("type") == "tool_use" for block in content):
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    if finish_reason == "stop":
        return "end_turn"
    return "end_turn"


def _openai_models_to_anthropic_models(response_payload: dict[str, Any]) -> dict[str, Any]:
    """Convert OpenAI model list to Anthropic models payload.

    Args:
        response_payload: OpenAI-compatible models payload.

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
    return {
        "data": models,
        "has_more": False,
        "first_id": models[0]["id"] if models else None,
        "last_id": models[-1]["id"] if models else None,
    }


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

    path = upstream_path.split("?", 1)[0].rstrip("/")
    prefix = "/v1/models/"
    if path.startswith(prefix):
        return path[len(prefix) :]
    return ""


def _estimate_input_tokens(request_payload: dict[str, Any]) -> int:
    """Estimate input token count for a request.

    Args:
        request_payload: Anthropic-compatible JSON payload.

    Returns:
        Estimated input token count.
    """

    text = "\n".join(message.get("content", "") if isinstance(message.get("content"), str) else _content_to_text(message.get("content")) for message in request_payload.get("messages", []))
    system_text = _content_to_text(request_payload.get("system"))
    if system_text:
        text = system_text + "\n" + text
    return max(1, len(text) // 4)


def _map_model_to_nvidia(model: Any, default_model: str = DEFAULT_NVIDIA_MODEL) -> str:
    """Map requested model names to a NVIDIA-supported model id.

    Args:
        model: Requested model value.
        default_model: Fallback NVIDIA model id.

    Returns:
        Selected NVIDIA model id.
    """

    model_name = str(model or "")
    if "/" in model_name and not model_name.startswith("claude-"):
        return model_name
    return default_model
