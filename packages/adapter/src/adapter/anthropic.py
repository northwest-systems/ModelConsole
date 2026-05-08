# 説明: Anthropic API へリクエストを転送するアダプターを提供する。
# 引数: なし。
# 返り値: なし。
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

# 標準の Anthropic API エンドポイント。MCON_ANTHROPIC_BASE_URL で上書きできる。
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"

# Anthropic API キーを読む環境変数名。
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"

# Anthropic 互換エンドポイントを差し替える環境変数名。
ENV_ANTHROPIC_BASE_URL = "MCON_ANTHROPIC_BASE_URL"

# 説明: HTTP ステータス と エラー種別 を持つアダプターエラーを表す。
# 引数: クラス属性と __init__ の引数で状態を表す。
# 返り値: AdapterError インスタンス。
class AdapterError(Exception):
    # 説明: 認証情報と標準モデルを初期化する。
    # 引数: status_code は HTTP レスポンスステータスコード。
    # 引数: error_type は機械判定用のエラー種別。
    # 引数: message は変換対象の Anthropic message。
    # 返り値: なし。
    def __init__(self, status_code: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.message = message

# 説明: 転送先のステータス、ヘッダー、ボディを保持する。
# 引数: クラス属性と __init__ の引数で状態を表す。
# 返り値: RawAdapterResponse インスタンス。
@dataclass(frozen=True)
class RawAdapterResponse:
    status_code: int  # status_code: 転送先が返した HTTP ステータスコード。
    headers: dict[str, str]  # ヘッダー: 小文字 key に正規化したレスポンスヘッダー。
    body: bytes  # body: 転送先レスポンスボディの生 bytes。

# 説明: Anthropic API へ Anthropic 互換リクエストを転送する。
# 引数: クラス属性と __init__ の引数で状態を表す。
# 返り値: AnthropicAdapter インスタンス。
class AnthropicAdapter:
    # 説明: 認証情報と標準モデルを初期化する。
    # 引数: credentials は vault や環境変数から取得した認証情報。
    # 返り値: なし。
    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        self._credentials = credentials or {}

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
        try:
            with self.open_stream(request_headers, request_payload, upstream_path) as response:
                response_body = response.read().decode("utf-8")
                return json.loads(response_body), int(response.status)
        except urllib.error.HTTPError as http_error:
            response_body = http_error.read().decode("utf-8")
            try:
                return json.loads(response_body), int(http_error.code)
            except json.JSONDecodeError as decode_error:
                raise AdapterError(int(http_error.code), "upstream_error", response_body) from decode_error
        except urllib.error.URLError as url_error:
            raise AdapterError(502, "upstream_unavailable", str(url_error.reason)) from url_error
        except TimeoutError as timeout_error:
            raise AdapterError(504, "upstream_timeout", "Anthropic upstream request timed out") from timeout_error

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
        try:
            raw_response = self.request_raw(method, request_headers, upstream_path, request_payload)
            return json.loads(raw_response.body.decode("utf-8")), raw_response.status_code
        except urllib.error.HTTPError as http_error:
            response_body = http_error.read().decode("utf-8")
            try:
                return json.loads(response_body), int(http_error.code)
            except json.JSONDecodeError as decode_error:
                raise AdapterError(int(http_error.code), "upstream_error", response_body) from decode_error
        except json.JSONDecodeError as decode_error:
            raise AdapterError(502, "upstream_error", "Anthropic upstream returned non-JSON response") from decode_error

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
        body = None
        if request_payload is not None:
            body = json.dumps(request_payload).encode("utf-8")
        upstream_request = self._build_request(
            method,
            request_headers,
            upstream_path,
            body,
            content_type="application/json",
        )
        return self._open_raw(upstream_request)

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
        content_type = request_headers.get("content-type", "application/octet-stream")
        upstream_request = self._build_request(method, request_headers, upstream_path, body, content_type=content_type)
        return self._open_raw(upstream_request)

    # 説明: 構築済み upstream request を送信し、生レスポンスを返す。
    # 引数: upstream_request は送信前の urllib request オブジェクト。
    # 返り値: ステータス、ヘッダー、ボディを含む RawAdapterResponse。
    def _open_raw(self, upstream_request: urllib.request.Request) -> RawAdapterResponse:
        try:
            with urllib.request.urlopen(upstream_request, timeout=120) as response:
                return RawAdapterResponse(
                    status_code=int(response.status),
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read(),
                )
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as url_error:
            raise AdapterError(502, "upstream_unavailable", str(url_error.reason)) from url_error
        except TimeoutError as timeout_error:
            raise AdapterError(504, "upstream_timeout", "Anthropic upstream request timed out") from timeout_error

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
    ) -> Any:
        body = json.dumps(request_payload).encode("utf-8")
        upstream_request = self._build_request(
            "POST",
            request_headers,
            upstream_path,
            body,
            content_type="application/json",
        )
        try:
            return urllib.request.urlopen(upstream_request, timeout=120)
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as url_error:
            raise AdapterError(502, "upstream_unavailable", str(url_error.reason)) from url_error
        except TimeoutError as timeout_error:
            raise AdapterError(504, "upstream_timeout", "Anthropic upstream request timed out") from timeout_error

    # 説明: 認証ヘッダーを含む 転送先リクエストオブジェクトを構築する。
    # 引数: method は転送先へ送る HTTP メソッド。
    # 引数: request_headers は呼び出し元から受け取った HTTP ヘッダー。
    # 引数: upstream_path は転送先 API パス。
    # 引数: body は転送する生リクエストボディ。
    # 引数: content_type は転送先へ渡す Content-Type。
    # 返り値: 認証ヘッダー 設定済み urllib request オブジェクト。
    def _build_request(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        body: bytes | None,
        content_type: str,
    ) -> urllib.request.Request:
        api_key = self._credentials.get(ENV_ANTHROPIC_API_KEY) or os.environ.get(ENV_ANTHROPIC_API_KEY)
        if not api_key:
            raise AdapterError(503, "configuration_error", f"{ENV_ANTHROPIC_API_KEY} is not set")

        base_url = os.environ.get(ENV_ANTHROPIC_BASE_URL, DEFAULT_ANTHROPIC_BASE_URL).rstrip("/")
        upstream_url = f"{base_url}{upstream_path}"
        anthropic_version = request_headers.get("anthropic-version", "2023-06-01")
        upstream_request = urllib.request.Request(
            upstream_url,
            data=body,
            method=method,
            headers={
                "content-type": content_type,
                "accept": "application/json",
                "x-api-key": api_key,
                "anthropic-version": anthropic_version,
            },
        )
        beta_header = request_headers.get("anthropic-beta")
        if beta_header:
            upstream_request.add_header("anthropic-beta", beta_header)
        return upstream_request

# ADAPTER_SPEC: ファクトリーが自動収集する モジュールレベルの登録情報。
ADAPTER_SPEC = {
    "name": "anthropic",  # name: ファクトリーが照合する正規バックエンド名。
    "aliases": (),  # aliases: 正規名以外で受け付ける別名。
    "adapter_class": AnthropicAdapter,  # adapter_class: 生成するアダプタークラス。
    "model_override": None,  # model_override: バックエンド固有モデル上書きのキーワード名。
}
