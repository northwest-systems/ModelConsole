# 説明: このモジュールの処理。
# 引数: なし。
# 返り値: なし。
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

from .anthropic import AdapterError, RawAdapterResponse
from .codex import SyntheticStreamResponse, _anthropic_message_to_sse

# Claude Code CLI バックエンドの標準モデル。MCON_CLAUDE_CODE_MODEL または boot --model で上書きする。
DEFAULT_CLAUDE_CODE_MODEL = "claude-sonnet-4-6"

# Claude Code CLI が読む API キー環境変数名。
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"

# Claude Code CLI へ渡す モデル上書き用環境変数名。
ENV_CLAUDE_CODE_MODEL = "MCON_CLAUDE_CODE_MODEL"

# 説明: このクラスの処理を提供する。
# 引数: 定義された引数を使用する。
# 返り値: クラスのインスタンス。
class ClaudeCodeAdapter:
    backend_name = "claude-code"

    # 説明: この関数の処理を行う。
    # 引数: 定義された引数を使用する。
    # 返り値: なし。
    def __init__(self, credentials: dict[str, str] | None = None, default_model: str | None = None) -> None:
        self._credentials = credentials or {}
        self._default_model = default_model or DEFAULT_CLAUDE_CODE_MODEL

    # 説明: この関数の処理を行う。
    # 引数: 定義された引数を使用する。
    # 返り値: 型注釈に従う値を返す。
    def forward_json(
        self,
        request_headers: Any,
        request_payload: dict[str, Any],
        upstream_path: str = "/v1/messages",
    ) -> tuple[dict[str, Any], int]:
        if upstream_path == "/v1/messages/count_tokens":
            return {"input_tokens": len(_prompt_from_anthropic_request(request_payload).split())}, 200
        if upstream_path != "/v1/messages":
            raise AdapterError(501, "not_supported", f"Claude Code adapter does not implement {upstream_path}")
        if request_payload.get("tools"):
            raise AdapterError(501, "not_supported", "Claude Code adapter does not support Anthropic tool use yet")

        started_at = time.time()
        prompt = _prompt_from_anthropic_request(request_payload)
        output = self._run_claude_prompt(prompt, request_payload)
        return _claude_text_to_anthropic_message(output, request_payload, started_at), 200

    # 説明: この関数の処理を行う。
    # 引数: 定義された引数を使用する。
    # 返り値: 型注釈に従う値を返す。
    def request_json(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        request_payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        if upstream_path.startswith("/v1/models"):
            requested_model = _requested_model_from_path(upstream_path)
            if requested_model:
                return _anthropic_model(requested_model), 200
            return _claude_models(os.environ.get(ENV_CLAUDE_CODE_MODEL) or self._default_model), 200
        if upstream_path == "/v1/messages/count_tokens" and request_payload is not None:
            return {"input_tokens": len(_prompt_from_anthropic_request(request_payload).split())}, 200
        raise AdapterError(501, "not_supported", f"Claude Code adapter does not implement {upstream_path}")

    # 説明: この関数の処理を行う。
    # 引数: 定義された引数を使用する。
    # 返り値: 型注釈に従う値を返す。
    def request_raw(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        request_payload: dict[str, Any] | None = None,
    ) -> RawAdapterResponse:
        response_payload, status_code = self.request_json(method, request_headers, upstream_path, request_payload)
        return RawAdapterResponse(
            status_code=status_code,
            headers={"content-type": "application/json"},
            body=json.dumps(response_payload, separators=(",", ":")).encode("utf-8"),
        )

    # 説明: この関数の処理を行う。
    # 引数: 定義された引数を使用する。
    # 返り値: 型注釈に従う値を返す。
    def request_raw_body(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        body: bytes | None,
    ) -> RawAdapterResponse:
        raise AdapterError(501, "not_supported", f"Claude Code adapter does not implement raw body endpoint {upstream_path}")

    # 説明: この関数の処理を行う。
    # 引数: 定義された引数を使用する。
    # 返り値: 型注釈に従う値を返す。
    def open_stream(
        self,
        request_headers: Any,
        request_payload: dict[str, Any],
        upstream_path: str = "/v1/messages",
    ) -> SyntheticStreamResponse:
        payload_without_stream = dict(request_payload)
        payload_without_stream["stream"] = False
        response_payload, status_code = self.forward_json(request_headers, payload_without_stream, upstream_path)
        return SyntheticStreamResponse(
            status=status_code,
            headers={"content-type": "text/event-stream"},
            body=_anthropic_message_to_sse(response_payload),
        )

    # 説明: この関数の処理を行う。
    # 引数: 定義された引数を使用する。
    # 返り値: 型注釈に従う値を返す。
    def _run_claude_prompt(self, prompt: str, request_payload: dict[str, Any]) -> str:
        model = os.environ.get(ENV_CLAUDE_CODE_MODEL) or self._default_model
        command = ["claude", "-p", prompt, "--model", model, "--output-format", "text"]
        environment = dict(os.environ)
        credential_value = self._credentials.get(ENV_ANTHROPIC_API_KEY)
        if credential_value:
            environment[ENV_ANTHROPIC_API_KEY] = credential_value
        completed_process = subprocess.run(command, env=environment, text=True, capture_output=True, check=False, timeout=300)
        if completed_process.returncode != 0:
            message = completed_process.stderr.strip() or completed_process.stdout.strip() or "claude command failed"
            raise AdapterError(502, "upstream_error", message)
        return completed_process.stdout.strip()

# 説明: この関数の処理を行う。
# 引数: 定義された引数を使用する。
# 返り値: 型注釈に従う値を返す。
def _prompt_from_anthropic_request(request_payload: dict[str, Any]) -> str:
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

# 説明: この関数の処理を行う。
# 引数: 定義された引数を使用する。
# 返り値: 型注釈に従う値を返す。
def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                text_parts.append(json.dumps(block.get("content", ""), ensure_ascii=False))
        return "\n".join(part for part in text_parts if part)
    return str(content)

# 説明: この関数の処理を行う。
# 引数: 定義された引数を使用する。
# 返り値: 型注釈に従う値を返す。
def _claude_text_to_anthropic_message(output: str, request_payload: dict[str, Any], started_at: float) -> dict[str, Any]:
    input_tokens = len(_prompt_from_anthropic_request(request_payload).split())
    output_tokens = len(output.split())
    return {
        "id": f"msg_claude_code_{int(started_at * 1000)}",
        "type": "message",
        "role": "assistant",
        "model": request_payload.get("model", os.environ.get(ENV_CLAUDE_CODE_MODEL, DEFAULT_CLAUDE_CODE_MODEL)),
        "content": [{"type": "text", "text": output}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }

# 説明: この関数の処理を行う。
# 引数: 定義された引数を使用する。
# 返り値: 型注釈に従う値を返す。
def _claude_models(model_id: str | None = None) -> dict[str, Any]:
    resolved_model_id = model_id or os.environ.get(ENV_CLAUDE_CODE_MODEL, DEFAULT_CLAUDE_CODE_MODEL)
    models = [_anthropic_model(resolved_model_id)]
    return {
        "data": models,
        "has_more": False,
        "first_id": models[0]["id"],
        "last_id": models[-1]["id"],
    }

# 説明: この関数の処理を行う。
# 引数: 定義された引数を使用する。
# 返り値: 型注釈に従う値を返す。
def _anthropic_model(model_id: str) -> dict[str, Any]:
    return {"id": model_id, "type": "model", "display_name": model_id, "created_at": None}

# 説明: この関数の処理を行う。
# 引数: 定義された引数を使用する。
# 返り値: 型注釈に従う値を返す。
def _requested_model_from_path(upstream_path: str) -> str:
    import urllib.parse

    path = urllib.parse.urlsplit(upstream_path).path
    prefix = "/v1/models/"
    if not path.startswith(prefix):
        return ""
    return urllib.parse.unquote(path[len(prefix) :])

ADAPTER_SPEC = {
    "name": "claude-code",
    "aliases": ("claude",),
    "adapter_class": ClaudeCodeAdapter,
    "model_override": "claude_code_model",
}
