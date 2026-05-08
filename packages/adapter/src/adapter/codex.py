# 説明: Codex CLI と OpenAI Responses API 互換応答を Anthropic Messages API 形状へ変換する。
# 引数: なし。
# 返り値: なし。
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

# Codex CLI の標準モデル。MCON_CODEX_MODEL または boot --model で上書きする。
DEFAULT_CODEX_MODEL = "gpt-5.3-codex"

# Claude Code 互換用に モデル API へ見せる仮モデル名。
CLAUDE_PROXY_MODEL = "claude-sonnet-4-6"

# Codex/OpenAI 認証に使う API キー環境変数名。
ENV_OPENAI_API_KEY = "OPENAI_API_KEY"

# Codex CLI へ渡す モデル上書き用環境変数名。
ENV_CODEX_MODEL = "MCON_CODEX_MODEL"

# 説明: 非ストリーミング応答を SSE stream として読ませる軽量 response を表す。
# 引数: クラス属性と __init__ の引数で状態を表す。
# 返り値: SyntheticStreamResponse インスタンス。
@dataclass
class SyntheticStreamResponse:
    status: int  # status: 合成レスポンスとして返す HTTP ステータス。
    headers: dict[str, str]  # ヘッダー: 合成レスポンスヘッダー。
    body: bytes  # body: read で返すレスポンスボディ bytes。
    _offset: int = 0  # _offset: 次に読み出す byte 位置。

    # 説明: with 文で SyntheticStreamResponse 自身を返す。
    # 引数: なし。
    # 返り値: SyntheticStreamResponse 自身。
    def __enter__(self) -> "SyntheticStreamResponse":
        return self

    # 説明: with 文終了時の後処理として何もしない。
    # 引数: exc_type は with 文終了時の 例外型。
    # 引数: exc_value は with 文終了時の 例外値。
    # 引数: traceback は with 文終了時の トレースバック。
    # 返り値: なし。
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    # 説明: SyntheticStreamResponse のボディから指定 byte 数を読む。
    # 引数: size は読み取る最大 byte 数。
    # 返り値: 読み取ったボディ bytes。
    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self.body):
            return b""
        if size is None or size < 0:
            size = len(self.body) - self._offset
        chunk = self.body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

# 説明: Codex CLI 実行と Responses API 変換を Anthropic Messages API 互換アダプターとして扱う。
# 引数: クラス属性と __init__ の引数で状態を表す。
# 返り値: CodexAdapter インスタンス。
class CodexAdapter:
    # backend_name: ファクトリーと 実行基盤が参照する Codex バックエンドの正規名。
    backend_name = "codex"

    # 説明: 認証情報と標準モデルを初期化する。
    # 引数: credentials は vault や環境変数から取得した認証情報。
    # 引数: default_model はリクエストにモデルがない場合の代替モデル。
    # 返り値: なし。
    def __init__(self, credentials: dict[str, str] | None = None, default_model: str | None = None) -> None:
        self._credentials = credentials or {}
        self._default_model = default_model or DEFAULT_CODEX_MODEL

    # 説明: Anthropic Messages API 互換 JSON リクエストをバックエンドへ転送する。
    # 引数: request_headers は呼び出し元から受け取った HTTP ヘッダー。
    # 引数: request_payload は Anthropic 互換 JSON ボディ。
    # 引数: upstream_path は転送先 API パス。
    # 返り値: レスポンスペイロードと HTTP ステータスコードの組。
    def forward_json(
        self,
        request_headers: Any,
        request_payload: dict[str, Any],
        upstream_path: str = "/v1/messages",
    ) -> tuple[dict[str, Any], int]:
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

    # 説明: message 以外の JSON エンドポイントを処理する。
    # 引数: method は転送先へ送る HTTP メソッド。
    # 引数: request_headers は呼び出し元から受け取った HTTP ヘッダー。
    # 引数: upstream_path は転送先 API パス。
    # 引数: request_payload は Anthropic 互換 JSON ボディ。
    # 返り値: レスポンスペイロードと HTTP ステータスコードの組。
    def request_json(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        request_payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        if upstream_path.startswith("/v1/models"):
            return self._request_models(method, upstream_path)
        if upstream_path == "/v1/messages/count_tokens" and request_payload is not None:
            return self._request_input_tokens(request_payload)
        raise AdapterError(501, "not_supported", f"Codex adapter does not implement {upstream_path}")

    # 説明: JSON エンドポイントの応答を RawAdapterResponse として返す。
    # 引数: method は転送先へ送る HTTP メソッド。
    # 引数: request_headers は呼び出し元から受け取った HTTP ヘッダー。
    # 引数: upstream_path は転送先 API パス。
    # 引数: request_payload は Anthropic 互換 JSON ボディ。
    # 返り値: ステータス、ヘッダー、ボディを含む RawAdapterResponse。
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

    # 説明: 生ボディリクエストを処理するか、未対応エンドポイントとして拒否する。
    # 引数: method は転送先へ送る HTTP メソッド。
    # 引数: request_headers は呼び出し元から受け取った HTTP ヘッダー。
    # 引数: upstream_path は転送先 API パス。
    # 引数: body は転送する生リクエストボディ。
    # 返り値: ステータス、ヘッダー、ボディを含む RawAdapterResponse。
    def request_raw_body(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        body: bytes | None,
    ) -> RawAdapterResponse:
        raise AdapterError(501, "not_supported", f"Codex adapter does not implement raw body endpoint {upstream_path}")

    # 説明: ストリームリクエストを開くか、非ストリーミング応答から SSE レスポンスを作る。
    # 引数: request_headers は呼び出し元から受け取った HTTP ヘッダー。
    # 引数: request_payload は Anthropic 互換 JSON ボディ。
    # 引数: upstream_path は転送先 API パス。
    # 返り値: ストリームとして read できるレスポンスオブジェクト。
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

    # 説明: バックエンド model エンドポイントの応答を取得または生成する。
    # 引数: method は転送先へ送る HTTP メソッド。
    # 引数: upstream_path は転送先 API パス。
    # 返り値: モデル一覧または単一モデルのペイロードと HTTP ステータスコードの組。
    def _request_models(self, method: str, upstream_path: str) -> tuple[dict[str, Any], int]:
        if method != "GET":
            raise AdapterError(405, "method_not_allowed", "Models API only supports GET")
        requested_model = _requested_model_from_path(upstream_path)
        if requested_model:
            if requested_model.startswith("claude-"):
                return _anthropic_model(requested_model), 200
            return _anthropic_model(os.environ.get(ENV_CODEX_MODEL) or self._default_model), 200
        return _codex_models(os.environ.get(ENV_CODEX_MODEL) or self._default_model), 200

    # 説明: リクエストペイロード の概算 input token 数 を返す。
    # 引数: request_payload は Anthropic 互換 JSON ボディ。
    # 返り値: 概算 input token 数のペイロードと HTTP ステータスコードの組。
    def _request_input_tokens(self, request_payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        return {"input_tokens": len(_prompt_from_anthropic_request(request_payload).split())}, 200

    # 説明: Codex CLI を 1 プロンプトで実行して標準出力を返す。
    # 引数: prompt は CLI に渡すテキストプロンプト。
    # 引数: request_payload は Anthropic 互換 JSON ボディ。
    # 返り値: Codex CLI の標準出力テキスト。
    def _run_codex_prompt(self, prompt: str, request_payload: dict[str, Any]) -> str:
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

# 説明: Anthropic Messages リクエストを OpenAI Responses API ペイロードに変換する。
# 引数: request_payload は Anthropic 互換 JSON ボディ。
# 引数: default_model はリクエストにモデルがない場合の代替モデル。
# 返り値: OpenAI Responses API に渡す JSON ペイロード。
def _anthropic_message_to_response_payload(
    request_payload: dict[str, Any],
    default_model: str = DEFAULT_CODEX_MODEL,
) -> dict[str, Any]:
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

# 説明: message 配列をバックエンド API の message 配列へ変換する。
# 引数: messages は変換対象の message 配列。
# 返り値: バックエンド API に渡すメッセージ配列。
def _convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    input_items: list[dict[str, Any]] = []
    for message in messages:
        input_items.extend(_convert_message(message))
    return input_items

# 説明: 1 件の Anthropic message を Responses API input item に変換する。
# 引数: message は変換対象の Anthropic message。
# 返り値: Responses API input item の配列。
def _convert_message(message: dict[str, Any]) -> list[dict[str, Any]]:
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

# 説明: role に応じた Responses API text block type を返す。
# 引数: role は message の 送信者 role。
# 返り値: Responses API の text block 種別。
def _text_type_for_role(role: str) -> str:
    if role == "assistant":
        return "output_text"
    return "input_text"

# 説明: Anthropic tool_result block を テキストに直列化する。
# 引数: block は変換対象の content block。
# 返り値: tool_result の内容を直列化したテキスト。
def _tool_result_to_text(block: dict[str, Any]) -> str:
    content = block.get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)

# 説明: Anthropic tool 定義をバックエンド API の tool 定義に変換する。
# 引数: tool は Anthropic tool 定義。
# 返り値: バックエンド API に渡す tool 定義。
def _convert_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
    }

# 説明: Anthropic tool_choice をバックエンド API の指定形式に変換する。
# 引数: tool_choice は Anthropic tool_choice 指定。
# 返り値: バックエンド API に渡す tool_choice 指定。
def _convert_tool_choice(tool_choice: Any) -> Any:
    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, dict):
        return "auto"
    if tool_choice.get("type") == "tool":
        return {"type": "function", "name": tool_choice.get("name")}
    return tool_choice.get("type", "auto")

# 説明: Anthropic system content を Responses API instructions に変換する。
# 引数: system は Anthropic system content。
# 返り値: Responses API に渡す instructions 文字列。system が空なら None。
def _system_to_instructions(system: Any) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = [block.get("text", "") for block in system if block.get("type") == "text"]
        return "\n\n".join(part for part in parts if part)
    return str(system)

# 説明: Anthropic Messages リクエストから CLI 用プロンプトを作る。
# 引数: request_payload は Anthropic 互換 JSON ボディ。
# 返り値: CLI に渡すプロンプトテキスト。
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

# 説明: Anthropic content block または文字列を テキストに変換する。
# 引数: content は Anthropic content block、文字列、または任意値。
# 返り値: プレーンテキストに変換した content。
def _content_to_text(content: Any) -> str:
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

# 説明: Codex CLI の テキスト出力を Anthropic message ペイロードに包む。
# 引数: output は CLI または バックエンド から得たテキスト。
# 引数: request_payload は Anthropic 互換 JSON ボディ。
# 引数: started_at は message id 生成用の開始時刻。
# 返り値: Anthropic message ペイロード。
def _codex_text_to_anthropic_message(output: str, request_payload: dict[str, Any], started_at: float) -> dict[str, Any]:
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

# 説明: Codex 用の Anthropic 形状 model list を作る。
# 引数: model_id はレスポンスに載せるモデル ID。
# 返り値: Anthropic 形状の model list ペイロード。
def _codex_models(model_id: str | None = None) -> dict[str, Any]:
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

# 説明: OpenAI Responses API ペイロードを Anthropic message ペイロードに変換する。
# 引数: response_payload はバックエンドから返った JSON ペイロード。
# 引数: request_payload は Anthropic 互換 JSON ボディ。
# 返り値: Anthropic message 形状の JSON ペイロード。
def _openai_response_to_anthropic_message(
    response_payload: dict[str, Any],
    request_payload: dict[str, Any],
) -> dict[str, Any]:
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

# 説明: OpenAI output item list を Anthropic content block list に変換する。
# 引数: output_items は OpenAI Responses API の output 配列。
# 返り値: Anthropic content block の配列。
def _convert_openai_output(output_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

# 説明: text block がない OpenAI output から 代替テキストを作る。
# 引数: output_items は OpenAI Responses API の output 配列。
# 返り値: 代替用のテキスト。
def response_text_fallback(output_items: list[dict[str, Any]]) -> str:
    for output_item in output_items:
        if output_item.get("type") == "message":
            return json.dumps(output_item.get("content", []), ensure_ascii=False)
    return ""

# 説明: tool call arguments を dict に parse する。
# 引数: arguments は tool call arguments の生値。
# 返り値: dict へ正規化した tool call arguments。
def _parse_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(str(arguments))
    except json.JSONDecodeError:
        return {"value": arguments}
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}

# 説明: OpenAI レスポンスステータスと content から Anthropic stop_reason を決める。
# 引数: response_payload はバックエンドから返った JSON ペイロード。
# 引数: content は Anthropic content block、文字列、または任意値。
# 返り値: Anthropic stop_reason の文字列。
def _stop_reason(response_payload: dict[str, Any], content: list[dict[str, Any]]) -> str:
    if any(block.get("type") == "tool_use" for block in content):
        return "tool_use"
    if response_payload.get("status") == "incomplete":
        return "max_tokens"
    return "end_turn"

# 説明: Anthropic message ペイロードを synthetic SSE bytes に変換する。
# 引数: message_payload は処理に使う値。
# 返り値: Anthropic SSE として返す bytes。
def _anthropic_message_to_sse(message_payload: dict[str, Any]) -> bytes:
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

# 説明: 1 件の SSE イベント bytes を作る。
# 引数: event_name は SSE イベント名。
# 引数: payload は SSE data に入れる JSON ペイロード。
# 返り値: SSE 1 イベント分の bytes。
def _sse_event(event_name: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode(
        "utf-8"
    )

# 説明: OpenAI model list ペイロードを Anthropic model list ペイロードに変換する。
# 引数: response_payload はバックエンドから返った JSON ペイロード。
# 返り値: Anthropic model list 形状の JSON ペイロード。
def _openai_models_to_anthropic_models(response_payload: dict[str, Any]) -> dict[str, Any]:
    models = [_anthropic_model(CLAUDE_PROXY_MODEL)]
    seen = {CLAUDE_PROXY_MODEL}
    for model in response_payload.get("data", []):
        model_id = model.get("id", "")
        if model_id and model_id not in seen:
            models.append(_anthropic_model(model_id))
            seen.add(model_id)
    return {"data": models, "has_more": False, "first_id": models[0]["id"] if models else None, "last_id": models[-1]["id"] if models else None}

# 説明: OpenAI model ペイロードを Anthropic model item ペイロードに変換する。
# 引数: response_payload はバックエンドから返った JSON ペイロード。
# 引数: fallback_model_id はペイロードにモデル ID がない場合の 代替値。
# 返り値: Anthropic model item 形状の JSON ペイロード。
def _openai_model_to_anthropic_model(response_payload: dict[str, Any], fallback_model_id: str) -> dict[str, Any]:
    model_id = str(response_payload.get("id") or fallback_model_id)
    return _anthropic_model(model_id)

# 説明: 1 件の Anthropic 形状 model item ペイロードを作る。
# 引数: model_id はレスポンスに載せるモデル ID。
# 返り値: Anthropic 形状の model item ペイロード。
def _anthropic_model(model_id: str) -> dict[str, Any]:
    return {"id": model_id, "type": "model", "display_name": model_id, "created_at": None}

# 説明: /v1/models/{id} path からモデル ID を取り出す。
# 引数: upstream_path は転送先 API パス。
# 返り値: デコード済みモデル ID。パスが不一致なら空文字列。
def _requested_model_from_path(upstream_path: str) -> str:
    path = urllib.parse.urlsplit(upstream_path).path
    prefix = "/v1/models/"
    if not path.startswith(prefix):
        return ""
    return urllib.parse.unquote(path[len(prefix) :])

# 説明: Anthropic リクエストから概算 input token 数ペイロードを作る。
# 引数: request_payload は Anthropic 互換 JSON ボディ。
# 返り値: 概算 input token 数の JSON ペイロード。
def _estimate_token_count(request_payload: dict[str, Any]) -> dict[str, int]:
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

# 説明: 要求モデルを Codex が扱うモデル ID へ写像する。
# 引数: model は全バックエンド共通のモデル上書き。
# 引数: default_model はリクエストにモデルがない場合の代替モデル。
# 返り値: Codex CLI に渡すモデル ID。
def _map_model_to_codex(model: Any, default_model: str = DEFAULT_CODEX_MODEL) -> str:
    model_name = str(model or "")
    if model_name.startswith("gpt-") or model_name.startswith("o"):
        return model_name
    return default_model

# ADAPTER_SPEC: ファクトリーが自動収集する モジュールレベルの登録情報。
ADAPTER_SPEC = {
    "name": "codex",  # name: ファクトリーが照合する正規バックエンド名。
    "aliases": (),  # aliases: 正規名以外で受け付ける別名。
    "adapter_class": CodexAdapter,  # adapter_class: 生成するアダプタークラス。
    "model_override": "codex_model",  # model_override: バックエンド固有モデル上書きのキーワード名。
}
