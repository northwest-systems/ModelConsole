# 説明: GitHub Copilot CLI を Anthropic Messages API 互換アダプターとして扱う。
# 引数: なし。
# 返り値: なし。
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
from typing import Any

from .anthropic import AdapterError, RawAdapterResponse
from .codex import SyntheticStreamResponse, _anthropic_message_to_sse

# GitHub Copilot CLI バックエンドの標準モデル。
DEFAULT_COPILOT_MODEL = "gpt-5.3-codex"

# Copilot CLI へ渡す モデル上書き用環境変数名。
ENV_COPILOT_MODEL = "MCON_COPILOT_MODEL"

# Copilot 認証で優先して読む トークン環境変数名。
ENV_COPILOT_GITHUB_TOKEN = "COPILOT_GITHUB_TOKEN"

# GitHub ツール互換の トークン環境変数名。
ENV_GH_TOKEN = "GH_TOKEN"
ENV_GITHUB_TOKEN = "GITHUB_TOKEN"

# 説明: GitHub Copilot CLI 実行を Anthropic Messages API 互換アダプターとして扱う。
# 引数: クラス属性と __init__ の引数で状態を表す。
# 返り値: CopilotAdapter インスタンス。
class CopilotAdapter:
    # backend_name: ファクトリーと 実行基盤が参照する Copilot バックエンドの正規名。
    backend_name = "copilot"

    # 説明: 認証情報と標準モデルを初期化する。
    # 引数: credentials は vault や環境変数から取得した認証情報。
    # 引数: default_model はリクエストにモデルがない場合の代替モデル。
    # 返り値: なし。
    def __init__(self, credentials: dict[str, str] | None = None, default_model: str | None = None) -> None:
        self._credentials = credentials or {}
        self._default_model = default_model or DEFAULT_COPILOT_MODEL

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
            return {"input_tokens": len(_prompt_from_anthropic_request(request_payload).split())}, 200
        if upstream_path != "/v1/messages":
            raise AdapterError(501, "not_supported", f"Copilot adapter does not implement {upstream_path}")
        if request_payload.get("tools"):
            raise AdapterError(501, "not_supported", "Copilot adapter does not support Anthropic tool use yet")

        started_at = time.time()
        prompt = _prompt_from_anthropic_request(request_payload)
        output = self._run_copilot_prompt(prompt, request_payload)
        response_payload = _copilot_text_to_anthropic_message(output, request_payload, started_at)
        return response_payload, 200

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
            requested_model = _requested_model_from_path(upstream_path)
            resolved_model_id = os.environ.get(ENV_COPILOT_MODEL) or self._default_model
            if requested_model:
                if requested_model != resolved_model_id:
                    raise AdapterError(404, "not_found", f"Copilot backend model is not available: {requested_model}")
                return _copilot_model(resolved_model_id), 200
            return _copilot_models(resolved_model_id), 200
        if upstream_path == "/v1/messages/count_tokens" and request_payload is not None:
            return {"input_tokens": len(_prompt_from_anthropic_request(request_payload).split())}, 200
        raise AdapterError(501, "not_supported", f"Copilot adapter does not implement {upstream_path}")

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
        raise AdapterError(501, "not_supported", f"Copilot adapter does not implement raw body endpoint {upstream_path}")

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

    # 説明: GitHub Copilot CLI を 1 プロンプトで実行して標準出力を返す。
    # 引数: prompt は CLI に渡すテキストプロンプト。
    # 引数: request_payload は Anthropic 互換 JSON ボディ。
    # 返り値: Copilot CLI の標準出力テキスト。
    def _run_copilot_prompt(self, prompt: str, request_payload: dict[str, Any]) -> str:
        model = os.environ.get(ENV_COPILOT_MODEL) or self._default_model
        command = ["copilot", "-s", "-p", prompt, "--model", model, "--stream", "off", "--output-format", "text", "--no-color"]
        environment = dict(os.environ)
        for variable_name in (ENV_COPILOT_GITHUB_TOKEN, ENV_GH_TOKEN, ENV_GITHUB_TOKEN):
            credential_value = self._credentials.get(variable_name)
            if credential_value:
                environment[variable_name] = credential_value
        completed_process = subprocess.run(command, env=environment, text=True, capture_output=True, check=False)
        if completed_process.returncode != 0:
            message = completed_process.stderr.strip() or completed_process.stdout.strip() or "copilot command failed"
            raise AdapterError(502, "upstream_error", message)
        return completed_process.stdout.strip()

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
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                text_parts.append(json.dumps(block.get("content", ""), ensure_ascii=False))
        return "\n".join(part for part in text_parts if part)
    return str(content)

# 説明: Copilot CLI の テキスト出力を Anthropic message ペイロードに包む。
# 引数: output は CLI または バックエンド から得たテキスト。
# 引数: request_payload は Anthropic 互換 JSON ボディ。
# 引数: started_at は message id 生成用の開始時刻。
# 返り値: Anthropic message ペイロード。
def _copilot_text_to_anthropic_message(output: str, request_payload: dict[str, Any], started_at: float) -> dict[str, Any]:
    input_tokens = len(_prompt_from_anthropic_request(request_payload).split())
    output_tokens = len(output.split())
    return {
        "id": f"msg_copilot_{int(started_at * 1000)}",
        "type": "message",
        "role": "assistant",
        "model": request_payload.get("model", os.environ.get(ENV_COPILOT_MODEL, DEFAULT_COPILOT_MODEL)),
        "content": [{"type": "text", "text": output}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }

# 説明: Copilot 用の Anthropic 形状 model list を作る。
# 引数: model_id はレスポンスに載せるモデル ID。
# 返り値: Anthropic 形状の model list ペイロード。
def _copilot_models(model_id: str | None = None) -> dict[str, Any]:
    resolved_model_id = model_id or os.environ.get(ENV_COPILOT_MODEL, DEFAULT_COPILOT_MODEL)
    models = [_copilot_model(resolved_model_id)]
    return {
        "data": models,
        "has_more": False,
        "first_id": models[0]["id"],
        "last_id": models[-1]["id"],
    }

# 説明: _copilot_model を実行する。
# 引数: model_id はレスポンスに載せるモデル ID。
# 返り値: Anthropic model item 形状の JSON ペイロード。
def _copilot_model(model_id: str) -> dict[str, Any]:
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

# ADAPTER_SPEC: ファクトリーが自動収集する モジュールレベルの登録情報。
ADAPTER_SPEC = {
    "name": "copilot",  # name: ファクトリーが照合する正規バックエンド名。
    "aliases": (),  # aliases: 正規名以外で受け付ける別名。
    "adapter_class": CopilotAdapter,  # adapter_class: 生成するアダプタークラス。
    "model_override": "copilot_model",  # model_override: バックエンド固有モデル上書きのキーワード名。
}
