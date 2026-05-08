# 説明: NVIDIA NIM Chat Completions API を Anthropic Messages API 互換アダプターとして扱う。
# 引数: なし。
# 返り値: なし。
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .anthropic import AdapterError, RawAdapterResponse
from .codex import SyntheticStreamResponse, _anthropic_message_to_sse

# NVIDIA NIM/OpenAI 互換 API の標準エンドポイント。
DEFAULT_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com"

# NVIDIA バックエンドの標準モデル。MCON_NVIDIA_MODEL または boot --model で上書きする。
DEFAULT_NVIDIA_MODEL = "nvidia/llama-3.3-nemotron-super-49b-v1.5"

# NVIDIA NIM API キーを読む環境変数名。
ENV_NVIDIA_API_KEY = "NVIDIA_API_KEY"

# NVIDIA NGC 互換キー名。
ENV_NGC_API_KEY = "NGC_API_KEY"

# NVIDIA 互換エンドポイントを差し替える環境変数名。
ENV_NVIDIA_BASE_URL = "MCON_NVIDIA_BASE_URL"

# NVIDIA モデル上書き用環境変数名。
ENV_NVIDIA_MODEL = "MCON_NVIDIA_MODEL"

# 説明: NVIDIA NIM API 呼び出しを Anthropic Messages API 互換アダプターとして扱う。
# 引数: クラス属性と __init__ の引数で状態を表す。
# 返り値: NvidiaNimAdapter インスタンス。
class NvidiaNimAdapter:
    # backend_name: ファクトリーと 実行基盤が参照する NVIDIA バックエンドの正規名。
    backend_name = "nvidia"

    # 説明: 認証情報と標準モデルを初期化する。
    # 引数: credentials は vault や環境変数から取得した認証情報。
    # 引数: default_model はリクエストにモデルがない場合の代替モデル。
    # 返り値: なし。
    def __init__(self, credentials: dict[str, str] | None = None, default_model: str | None = None) -> None:
        self._credentials = credentials or {}
        self._default_model = default_model or DEFAULT_NVIDIA_MODEL

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
            return {"input_tokens": _estimate_input_tokens(request_payload)}, 200
        if upstream_path != "/v1/messages":
            raise AdapterError(501, "not_supported", f"NVIDIA NIM adapter does not implement {upstream_path}")

        chat_payload = _anthropic_message_to_chat_payload(request_payload, self._default_model)
        raw_response = self._request_nvidia_json("POST", "/v1/chat/completions", chat_payload)
        response_payload = json.loads(raw_response.body.decode("utf-8"))
        return _chat_completion_to_anthropic_message(response_payload, request_payload), raw_response.status_code

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
            return {"input_tokens": _estimate_input_tokens(request_payload)}, 200
        raise AdapterError(501, "not_supported", f"NVIDIA NIM adapter does not implement {upstream_path}")

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
        raise AdapterError(501, "not_supported", f"NVIDIA NIM adapter does not implement raw body endpoint {upstream_path}")

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
        raw_response = self._request_nvidia_json("GET", upstream_path)
        response_payload = json.loads(raw_response.body.decode("utf-8"))
        if requested_model:
            return _openai_model_to_anthropic_model(response_payload, requested_model), raw_response.status_code
        return _openai_models_to_anthropic_models(response_payload), raw_response.status_code

    # 説明: NVIDIA NIM JSON API を呼び出し RawAdapterResponse を返す。
    # 引数: method は転送先へ送る HTTP メソッド。
    # 引数: upstream_path は転送先 API パス。
    # 引数: request_payload は Anthropic 互換 JSON ボディ。
    # 返り値: NVIDIA NIM API から返った RawAdapterResponse。
    def _request_nvidia_json(
        self,
        method: str,
        upstream_path: str,
        request_payload: dict[str, Any] | None = None,
    ) -> RawAdapterResponse:
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

    # 説明: 認証ヘッダーを含む 転送先リクエストオブジェクトを構築する。
    # 引数: method は転送先へ送る HTTP メソッド。
    # 引数: upstream_path は転送先 API パス。
    # 引数: body は転送する生リクエストボディ。
    # 返り値: 認証ヘッダー 設定済み urllib request オブジェクト。
    def _build_request(self, method: str, upstream_path: str, body: bytes | None) -> urllib.request.Request:
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

# 説明: Anthropic Messages リクエストを OpenAI chat completions ペイロードに変換する。
# 引数: request_payload は Anthropic 互換 JSON ボディ。
# 引数: default_model はリクエストにモデルがない場合の代替モデル。
# 返り値: OpenAI chat completions API に渡す JSON ペイロード。
def _anthropic_message_to_chat_payload(request_payload: dict[str, Any], default_model: str) -> dict[str, Any]:
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

# 説明: message 配列をバックエンド API の message 配列へ変換する。
# 引数: request_payload は Anthropic 互換 JSON ボディ。
# 返り値: バックエンド API に渡すメッセージ配列。
def _convert_messages(request_payload: dict[str, Any]) -> list[dict[str, Any]]:
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

# 説明: Anthropic content block または文字列を テキストに変換する。
# 引数: content は Anthropic content block、文字列、または任意値。
# 返り値: プレーンテキストに変換した content。
def _content_to_text(content: Any) -> str:
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

# 説明: content に tool_use block が含まれるか判定する。
# 引数: content は Anthropic content block、文字列、または任意値。
# 返り値: tool_use block が含まれる場合は True。
def _contains_tool_use(content: Any) -> bool:
    return isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content)

# 説明: content に tool_result block が含まれるか判定する。
# 引数: content は Anthropic content block、文字列、または任意値。
# 返り値: tool_result block が含まれる場合は True。
def _contains_tool_result(content: Any) -> bool:
    return isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)

# 説明: Anthropic tool_use block から OpenAI tool_calls を作る。
# 引数: content は Anthropic content block、文字列、または任意値。
# 返り値: OpenAI tool_calls 形状の配列。
def _tool_calls_from_content(content: Any) -> list[dict[str, Any]]:
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

# 説明: Anthropic tool_result block から OpenAI tool message を作る。
# 引数: content は Anthropic content block、文字列、または任意値。
# 返り値: OpenAI tool message 形状の配列。
def _tool_results_from_content(content: Any) -> list[dict[str, Any]]:
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

# 説明: Anthropic tool 定義をバックエンド API の tool 定義に変換する。
# 引数: tool は Anthropic tool 定義。
# 返り値: バックエンド API に渡す tool 定義。
def _convert_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
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
        return {"type": "function", "function": {"name": tool_choice.get("name", "")}}
    return tool_choice.get("type", "auto")

# 説明: OpenAI chat completion レスポンスを Anthropic message ペイロードに変換する。
# 引数: response_payload はバックエンドから返った JSON ペイロード。
# 引数: request_payload は Anthropic 互換 JSON ボディ。
# 返り値: Anthropic message 形状の JSON ペイロード。
def _chat_completion_to_anthropic_message(
    response_payload: dict[str, Any],
    request_payload: dict[str, Any],
) -> dict[str, Any]:
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

# 説明: OpenAI chat message を Anthropic content block list に変換する。
# 引数: message は変換対象の Anthropic message。
# 返り値: Anthropic content block の配列。
def _message_to_content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
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

# 説明: OpenAI finish_reason を Anthropic stop_reason に変換する。
# 引数: finish_reason は OpenAI response の終了理由。
# 引数: content は Anthropic content block、文字列、または任意値。
# 返り値: Anthropic stop_reason の文字列。
def _finish_reason_to_stop_reason(finish_reason: Any, content: list[dict[str, Any]]) -> str:
    if any(block.get("type") == "tool_use" for block in content):
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    if finish_reason == "stop":
        return "end_turn"
    return "end_turn"

# 説明: OpenAI model list ペイロードを Anthropic model list ペイロードに変換する。
# 引数: response_payload はバックエンドから返った JSON ペイロード。
# 返り値: Anthropic model list 形状の JSON ペイロード。
def _openai_models_to_anthropic_models(response_payload: dict[str, Any]) -> dict[str, Any]:
    models = []
    seen: set[str] = set()
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
    path = upstream_path.split("?", 1)[0].rstrip("/")
    prefix = "/v1/models/"
    if path.startswith(prefix):
        return path[len(prefix) :]
    return ""

# 説明: リクエストペイロードから概算 input token 数を返す。
# 引数: request_payload は Anthropic 互換 JSON ボディ。
# 返り値: 概算 input token 数。
def _estimate_input_tokens(request_payload: dict[str, Any]) -> int:
    text = "\n".join(message.get("content", "") if isinstance(message.get("content"), str) else _content_to_text(message.get("content")) for message in request_payload.get("messages", []))
    system_text = _content_to_text(request_payload.get("system"))
    if system_text:
        text = system_text + "\n" + text
    return max(1, len(text) // 4)

# 説明: 要求モデルを NVIDIA NIM が扱うモデル ID へ写像する。
# 引数: model は全バックエンド共通のモデル上書き。
# 引数: default_model はリクエストにモデルがない場合の代替モデル。
# 返り値: NVIDIA NIM API に渡すモデル ID。
def _map_model_to_nvidia(model: Any, default_model: str = DEFAULT_NVIDIA_MODEL) -> str:
    model_name = str(model or "")
    if "/" in model_name and not model_name.startswith("claude-"):
        return model_name
    return default_model

# ADAPTER_SPEC: ファクトリーが自動収集する モジュールレベルの登録情報。
ADAPTER_SPEC = {
    "name": "nvidia",  # name: ファクトリーが照合する正規バックエンド名。
    "aliases": ("nim", "nvidia-nim"),  # aliases: 正規名以外で受け付ける別名。
    "adapter_class": NvidiaNimAdapter,  # adapter_class: 生成するアダプタークラス。
    "model_override": "nvidia_model",  # model_override: バックエンド固有モデル上書きのキーワード名。
}
