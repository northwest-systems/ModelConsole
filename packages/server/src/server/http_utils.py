"""HTTP helpers for stdlib-based services."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """HTTP request body を JSON object として読む。

    Args:
        handler: request body と headers を持つ stdlib HTTP handler。

    Returns:
        decode した JSON object。body が空なら空 dict。
    """

    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length <= 0:
        return {}
    body = handler.rfile.read(content_length)
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


def send_json(handler: BaseHTTPRequestHandler, status_code: int, payload: dict[str, Any]) -> None:
    """JSON response を送信する。

    Args:
        handler: response を書き込む stdlib HTTP handler。
        status_code: HTTP status code。
        payload: JSON に serialize する dict。

    Returns:
        なし。
    """

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_text(handler: BaseHTTPRequestHandler, status_code: int, text: str) -> None:
    """plain text response を送信する。

    Args:
        handler: response を書き込む stdlib HTTP handler。
        status_code: HTTP status code。
        text: response body の text。

    Returns:
        なし。
    """

    body = text.encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
