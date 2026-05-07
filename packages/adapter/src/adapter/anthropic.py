"""Anthropic upstream adapter."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

# 標準の Anthropic API endpoint。MCON_ANTHROPIC_BASE_URL で上書きできる。
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"

# Anthropic API key を読む環境変数名。
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"

# Anthropic compatible endpoint を差し替える環境変数名。
ENV_ANTHROPIC_BASE_URL = "MCON_ANTHROPIC_BASE_URL"


class AdapterError(Exception):
    """User-facing adapter error."""

    def __init__(self, status_code: int, error_type: str, message: str) -> None:
        """Create an adapter error that can be returned by the server.

        Args:
            status_code: HTTP status code to return to the caller.
            error_type: Stable machine-readable error type.
            message: Human-readable error message.

        Returns:
            ``None``.
        """

        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class RawAdapterResponse:
    """Raw upstream response body plus selected response metadata."""

    status_code: int
    headers: dict[str, str]
    body: bytes


class AnthropicAdapter:
    """Forward Anthropic-compatible requests to Anthropic upstream."""

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        """Initialize the Anthropic adapter.

        Args:
            credentials: Optional credential values loaded from the vault.

        Returns:
            ``None``.
        """

        self._credentials = credentials or {}

    def forward_json(
        self,
        request_headers: Any,
        request_payload: dict[str, Any],
        upstream_path: str = "/v1/messages",
    ) -> tuple[dict[str, Any], int]:
        """Forward an Anthropic-compatible JSON request.

        Args:
            request_headers: Incoming request headers.
            request_payload: Anthropic-compatible JSON payload.
            upstream_path: Anthropic upstream path.

        Returns:
            Tuple of decoded response payload and HTTP status code.
        """

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

    def request_json(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        request_payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        """Forward one JSON request to Anthropic and decode the JSON response.

        Args:
            method: HTTP method.
            request_headers: Incoming request headers.
            upstream_path: Anthropic upstream path.
            request_payload: Optional JSON payload.

        Returns:
            Tuple of decoded response payload and HTTP status code.
        """

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

    def request_raw(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        request_payload: dict[str, Any] | None = None,
    ) -> RawAdapterResponse:
        """Forward one request to Anthropic and return raw bytes.

        Args:
            method: HTTP method.
            request_headers: Incoming request headers.
            upstream_path: Anthropic upstream path.
            request_payload: Optional JSON payload.

        Returns:
            Raw response metadata and body bytes.
        """

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

    def request_raw_body(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        body: bytes | None,
    ) -> RawAdapterResponse:
        """Forward a request with an already encoded body.

        Args:
            method: HTTP method.
            request_headers: Incoming request headers.
            upstream_path: Anthropic upstream path.
            body: Already encoded request body.

        Returns:
            Raw response metadata and body bytes.
        """

        content_type = request_headers.get("content-type", "application/octet-stream")
        upstream_request = self._build_request(method, request_headers, upstream_path, body, content_type=content_type)
        return self._open_raw(upstream_request)

    def _open_raw(self, upstream_request: urllib.request.Request) -> RawAdapterResponse:
        """Open an upstream request and normalize transport errors.

        Args:
            upstream_request: Fully constructed urllib request.

        Returns:
            Raw response metadata and body bytes.
        """

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

    def open_stream(
        self,
        request_headers: Any,
        request_payload: dict[str, Any],
        upstream_path: str = "/v1/messages",
    ) -> Any:
        """Open a streaming upstream request.

        Args:
            request_headers: Incoming request headers.
            request_payload: Anthropic-compatible JSON payload.
            upstream_path: Anthropic upstream path.

        Returns:
            urllib response object.
        """

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

    def _build_request(
        self,
        method: str,
        request_headers: Any,
        upstream_path: str,
        body: bytes | None,
        content_type: str,
    ) -> urllib.request.Request:
        """Build an Anthropic upstream request with auth headers.

        Args:
            method: HTTP method.
            request_headers: Incoming request headers.
            upstream_path: Anthropic upstream path.
            body: Encoded request body.
            content_type: Request content type.

        Returns:
            Fully constructed urllib request.
        """

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
