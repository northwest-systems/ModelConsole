"""Provider-independent middleware between system runtime and backend adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


# middleware が何も指定されていない時に使う標準 mode。request/response を横流しする。
MIDDLEWARE_BYPASS = "bypass"


@dataclass(frozen=True)
class MiddlewareRequest:
    """Request object passed from system runtime to middleware.

    Args:
        session_id: Session id being processed.
        backend: Selected backend name.
        model: Selected model name.
        payload: Anthropic-like request payload for the adapter.
        headers: Request headers for the adapter.
        upstream_path: Adapter upstream path.

    Returns:
        Immutable request container.
    """

    session_id: str
    backend: str
    model: str
    payload: dict[str, Any]
    headers: dict[str, str]
    upstream_path: str = "/v1/messages"


@dataclass(frozen=True)
class MiddlewareResponse:
    """Response object returned from middleware to system runtime.

    Args:
        payload: Adapter response payload.
        status_code: Adapter HTTP-style status code.
        context_mode: Context handling mode reported to audit logs.

    Returns:
        Immutable response container.
    """

    payload: dict[str, Any]
    status_code: int
    context_mode: str = MIDDLEWARE_BYPASS


class BackendAdapter(Protocol):
    """Minimal adapter protocol required by middleware.

    Args:
        None.

    Returns:
        Protocol definition only.
    """

    def forward_json(
        self,
        request_headers: Any,
        request_payload: dict[str, Any],
        upstream_path: str = "/v1/messages",
    ) -> tuple[dict[str, Any], int]:
        """Forward a JSON request to a backend.

        Args:
            request_headers: Headers passed to the backend adapter.
            request_payload: Anthropic-like request payload.
            upstream_path: Adapter upstream path.

        Returns:
            Tuple of response payload and status code.
        """


class BaseMiddleware:
    """Base middleware contract for request/response processing.

    Args:
        None.

    Returns:
        Middleware base class.
    """

    context_mode = MIDDLEWARE_BYPASS

    def forward(self, request: MiddlewareRequest, backend_adapter: BackendAdapter) -> MiddlewareResponse:
        """Process a request and call the backend adapter.

        Args:
            request: Request container from the system runtime.
            backend_adapter: Selected backend adapter.

        Returns:
            Middleware response container.
        """

        response_payload, status_code = backend_adapter.forward_json(
            request.headers,
            request.payload,
            request.upstream_path,
        )
        return MiddlewareResponse(response_payload, status_code, context_mode=self.context_mode)


class BypassMiddleware(BaseMiddleware):
    """Middleware that passes requests and responses through unchanged.

    Args:
        None.

    Returns:
        Pass-through middleware instance.
    """

    context_mode = MIDDLEWARE_BYPASS


def build_middleware(mode: str | None = None) -> BaseMiddleware:
    """Build middleware for the system-to-adapter path.

    Args:
        mode: Middleware mode name. ``None`` or ``bypass`` selects pass-through.

    Returns:
        Middleware instance.

    Raises:
        ValueError: When an unsupported middleware mode is requested.
    """

    normalized_mode = (mode or MIDDLEWARE_BYPASS).strip().lower()
    if normalized_mode == MIDDLEWARE_BYPASS:
        return BypassMiddleware()
    raise ValueError(f"Unsupported middleware mode: {mode}")


__all__ = [
    "BackendAdapter",
    "BaseMiddleware",
    "BypassMiddleware",
    "MIDDLEWARE_BYPASS",
    "MiddlewareRequest",
    "MiddlewareResponse",
    "build_middleware",
]
