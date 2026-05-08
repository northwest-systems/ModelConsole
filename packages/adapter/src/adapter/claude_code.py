# 説明: Claude Code CLI を Anthropic Messages API 互換アダプターとして扱う。
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

# 説明: Claude Code CLI 実行を Anthropic Messages API 互換アダプターとして扱う。
# 引数: クラス属性と __init__ の引数で状態を表す。
# 返り値: ClaudeCodeAdapter インスタンス。
class ClaudeCodeAdapter:
    # backend_name: ファクトリーと 実行基盤が参照する Claude Code バックエンドの正規名。
    backend_name = "claude-code"

    # 説明: 認証情報と標準モデルを初期化する。
    # 引数: credentials は vault や環境変数から取得した認証情報。
    # 引数: default_model はリクエストにモデルがない場合の代替モデル。
    # 返り値: なし。
    def __init__(self, credentials: dict[str, str] | None = None, default_model: str | None = None) -> None:
        self._credentials = credentials or {}
        self._default_model = default_model or DEFAULT_CLAUDE_CODE_MODEL

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
            raise AdapterError(501, "not_supported", f"Claude Code adapter does not implement {upstream_path}")
        if request_payload.get("tools"):
            raise AdapterError(501, "not_supported", "Claude Code adapter does not support Anthropic tool use yet")

        started_at = time.time()
        prompt = _prompt_from_anthropic_request(request_payload)
        output = self._run_claude_prompt(prompt, request_payload)
        return _claude_text_to_anthropic_message(output, request_payload, started_at), 200

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
            if requested_model:
                return _anthropic_model(requested_model), 200
            return _claude_models(os.environ.get(ENV_CLAUDE_CODE_MODEL) or self._default_model), 200
        if upstream_path == "/v1/messages/count_tokens" and request_payload is not None:
            return {"input_tokens": len(_prompt_from_anthropic_request(request_payload).split())}, 200
        raise AdapterError(501, "not_supported", f"Claude Code adapter does not implement {upstream_path}")

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
        raise AdapterError(501, "not_supported", f"Claude Code adapter does not implement raw body endpoint {upstream_path}")

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

    # 説明: Claude Code CLI を 1 プロンプトで実行して標準出力を返す。
    # 引数: prompt は CLI に渡すテキストプロンプト。
    # 引数: request_payload は Anthropic 互換 JSON ボディ。
    # 返り値: Claude Code CLI の標準出力テキスト。
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

# 説明: Claude Code CLI の テキスト出力を Anthropic message ペイロードに包む。
# 引数: output は CLI または バックエンド から得たテキスト。
# 引数: request_payload は Anthropic 互換 JSON ボディ。
# 引数: started_at は message id 生成用の開始時刻。
# 返り値: Anthropic message ペイロード。
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

# 説明: Claude Code 用の Anthropic 形状 model list を作る。
# 引数: model_id はレスポンスに載せるモデル ID。
# 返り値: Anthropic 形状の model list ペイロード。
def _claude_models(model_id: str | None = None) -> dict[str, Any]:
    resolved_model_id = model_id or os.environ.get(ENV_CLAUDE_CODE_MODEL, DEFAULT_CLAUDE_CODE_MODEL)
    models = [_anthropic_model(resolved_model_id)]
    return {
        "data": models,
        "has_more": False,
        "first_id": models[0]["id"],
        "last_id": models[-1]["id"],
    }

# 説明: 1 件の Anthropic 形状 model item ペイロードを作る。
# 引数: model_id はレスポンスに載せるモデル ID。
# 返り値: Anthropic 形状の model item ペイロード。
def _anthropic_model(model_id: str) -> dict[str, Any]:
    return {"id": model_id, "type": "model", "display_name": model_id, "created_at": None}

# 説明: /v1/models/{id} path からモデル ID を取り出す。
# 引数: upstream_path は転送先 API パス。
# 返り値: デコード済みモデル ID。パスが不一致なら空文字列。
def _requested_model_from_path(upstream_path: str) -> str:
    import urllib.parse

    path = urllib.parse.urlsplit(upstream_path).path
    prefix = "/v1/models/"
    if not path.startswith(prefix):
        return ""
    return urllib.parse.unquote(path[len(prefix) :])

# ADAPTER_SPEC: ファクトリーが自動収集する モジュールレベルの登録情報。
ADAPTER_SPEC = {
    "name": "claude-code",  # name: ファクトリーが照合する正規バックエンド名。
    "aliases": ("claude",),  # aliases: 正規名以外で受け付ける別名。
    "adapter_class": ClaudeCodeAdapter,  # adapter_class: 生成するアダプタークラス。
    "model_override": "claude_code_model",  # model_override: バックエンド固有モデル上書きのキーワード名。
}
