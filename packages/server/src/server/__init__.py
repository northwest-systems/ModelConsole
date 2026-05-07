"""Server API for mcon frontends."""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import adapter

from audit import AuditLogger
from config import RuntimeConfig
from config.constants import AUDIT_SERVICE_NAME, SERVER_SERVICE_NAME, VAULT_SERVICE_NAME
from runtime import read_runtime, write_runtime
from system import SystemRuntime

from .http_utils import read_json_body, send_json, send_text


def run_server(runtime_config: RuntimeConfig) -> ThreadingHTTPServer:
    """Start the server API and return the server object.

    Args:
        runtime_config: Loaded runtime configuration with server bind settings.

    Returns:
        Configured ``ThreadingHTTPServer``. The caller decides when to serve.
    """

    audit_logger = AuditLogger(runtime_config.audit_root)
    system_runtime = SystemRuntime(runtime_config)

    class ServerHandler(BaseHTTPRequestHandler):
        server_version = "server/0.1"

        def do_GET(self) -> None:
            """Route HTTP GET requests.

            Args:
                None.

            Returns:
                ``None``. The method writes the HTTP response directly.
            """

            if self.path == "/health":
                send_json(self, 200, {"service": SERVER_SERVICE_NAME, "status": "ok"})
                return
            if self.path == "/api/status":
                send_json(self, 200, build_status(runtime_config))
                return
            if self.path == "/api/sessions":
                send_json(self, 200, {"sessions": system_runtime.list_sessions()})
                return
            if self.path.startswith("/api/sessions/"):
                self._handle_get_session(system_runtime)
                return
            if self.path == "/":
                send_text(self, 200, "mcon phase 1 server\n")
                return
            send_json(self, 404, {"error": "not_found"})

        def do_POST(self) -> None:
            """Route HTTP POST requests.

            Args:
                None.

            Returns:
                ``None``. The method writes the HTTP response directly.
            """

            if self.path == "/api/sessions":
                self._handle_create_session(system_runtime)
                return
            if self.path.startswith("/api/sessions/") and self.path.endswith("/messages"):
                self._handle_post_session_message(system_runtime)
                return
            send_json(self, 404, {"error": "not_found"})

        def log_message(self, format: str, *args: Any) -> None:
            """Suppress default access logging.

            Args:
                format: BaseHTTPRequestHandler format string.
                *args: Values for the format string.

            Returns:
                ``None``.
            """

            return

        def _handle_get_session(self, system_runtime: SystemRuntime) -> None:
            """Handle session metadata and transcript reads.

            Args:
                system_runtime: System layer that owns session state.

            Returns:
                ``None``. The method writes the HTTP response directly.
            """

            parts = self.path.strip("/").split("/")
            if len(parts) == 3:
                try:
                    send_json(self, 200, {"session": system_runtime.get_session(parts[2])})
                except KeyError:
                    send_json(self, 404, {"error": "session_not_found"})
                return
            if len(parts) == 4 and parts[3] == "transcript":
                try:
                    send_json(self, 200, {"messages": system_runtime.read_transcript(parts[2])})
                except KeyError:
                    send_json(self, 404, {"error": "session_not_found"})
                return
            send_json(self, 404, {"error": "not_found"})

        def _handle_create_session(self, system_runtime: SystemRuntime) -> None:
            """Handle session creation requests.

            Args:
                system_runtime: System layer that owns session state.

            Returns:
                ``None``. The method writes the HTTP response directly.
            """

            payload = read_json_body(self)
            try:
                session = system_runtime.create_session(
                    title=_optional_string(payload.get("title")),
                    backend=_optional_string(payload.get("backend")),
                    model=_optional_string(payload.get("model")),
                )
            except adapter.AdapterError as adapter_error:
                send_json(self, adapter_error.status_code, {"error": adapter_error.error_type, "message": adapter_error.message})
                return
            send_json(self, 201, {"session": session})

        def _handle_post_session_message(self, system_runtime: SystemRuntime) -> None:
            """Append a user message and return the assistant response.

            Args:
                system_runtime: System layer that owns session state.

            Returns:
                ``None``. The method writes the HTTP response directly.
            """

            parts = self.path.strip("/").split("/")
            if len(parts) != 4:
                send_json(self, 404, {"error": "not_found"})
                return
            payload = read_json_body(self)
            content = _optional_string(payload.get("content"))
            if content is None:
                send_json(self, 400, {"error": "invalid_request", "message": "content is required"})
                return
            try:
                result = system_runtime.append_user_message(parts[2], content)
            except KeyError:
                send_json(self, 404, {"error": "session_not_found"})
                return
            except ValueError as value_error:
                send_json(self, 400, {"error": "invalid_request", "message": str(value_error)})
                return
            except adapter.AdapterError as adapter_error:
                send_json(self, adapter_error.status_code, {"error": adapter_error.error_type, "message": adapter_error.message})
                return
            except Exception as unexpected_error:
                send_json(self, 500, {"error": "system_error", "message": str(unexpected_error)})
                return
            send_json(self, 200, result)

    http_server = ThreadingHTTPServer(
        (runtime_config.server.host, runtime_config.server.port),
        ServerHandler,
    )
    actual_host, actual_port = http_server.server_address
    write_runtime(
        runtime_config.runtime_path,
        {
            "server_host": str(actual_host),
            "server_pid": os.getpid(),
            "server_port": int(actual_port),
            "server_url": f"http://{actual_host}:{actual_port}",
        },
    )
    audit_logger.write_event("service_event", {"service": SERVER_SERVICE_NAME, "event": "started", "port": actual_port})
    return http_server


def build_status(runtime_config: RuntimeConfig) -> dict[str, Any]:
    """Build the runtime status payload.

    Args:
        runtime_config: Loaded runtime configuration.

    Returns:
        JSON-serializable status dictionary for CLI and API callers.
    """

    runtime_data = read_runtime(runtime_config.runtime_path)
    services = {
        SERVER_SERVICE_NAME: {"status": "ok"},
        AUDIT_SERVICE_NAME: {
            "status": "ok" if runtime_config.audit_root.exists() else "fail",
            "path": str(runtime_config.audit_root),
        },
        VAULT_SERVICE_NAME: {
            "status": "ok" if runtime_config.vault_root.exists() else "fail",
            "path": str(runtime_config.vault_root),
        },
    }

    return {
        "mode": "server-system",
        "runtime": runtime_data,
        "services": services,
    }


def _optional_string(value: object) -> str | None:
    """Normalize optional string fields from JSON payloads.

    Args:
        value: Raw JSON field value.

    Returns:
        Trimmed string, or ``None`` when the value is absent/blank.
    """

    if value is None:
        return None
    text = str(value).strip()
    return text or None
