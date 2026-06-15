"""Small standard-library HTTP server for the initial mcon control plane."""

from __future__ import annotations

import json
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from mcon.auth import AuthManager, AuthProviderError
from mcon.policy import PolicyError, PolicyManager


def run_server(*, plugin_root: Path, host: str, port: int) -> None:
    manager = PolicyManager.load(plugin_root)
    auth_manager = AuthManager.default()

    class Handler(BaseHTTPRequestHandler):
        server_version = "mcon/0.1"

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                self._send_json(200, {"service": "mcon", "status": "ok"})
                return
            if path == "/api/auth/providers":
                self._send_json(200, auth_manager.list_providers())
                return
            if path.startswith("/api/auth/"):
                self._handle_auth_get(path)
                return
            self._send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            try:
                payload = self._read_json()
                path = urlparse(self.path).path
                if path == "/api/policy/explain-command":
                    self._handle_explain_command(payload)
                    return
                if path == "/api/policy/explain-file":
                    self._handle_explain_file(payload)
                    return
                if path == "/api/exec":
                    self._handle_exec(payload)
                    return
                if path.startswith("/api/auth/"):
                    self._handle_auth_post(path)
                    return
                self._send_json(404, {"error": "not_found"})
            except PolicyError as error:
                self._send_json(400, {"error": "policy_error", "message": str(error)})
            except AuthProviderError as error:
                self._send_json(400, {"error": "auth_provider_error", "message": str(error)})
            except ValueError as error:
                self._send_json(400, {"error": "invalid_request", "message": str(error)})

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _handle_explain_command(self, payload: dict[str, Any]) -> None:
            subject = _required_string(payload, "subject")
            argv = _required_string_list(payload, "argv")
            self._send_json(200, manager.explain_command(subject, argv))

        def _handle_explain_file(self, payload: dict[str, Any]) -> None:
            subject = _required_string(payload, "subject")
            operation = _required_string(payload, "operation")
            path = _required_string(payload, "path")
            self._send_json(200, manager.explain_file(subject, operation, path))

        def _handle_exec(self, payload: dict[str, Any]) -> None:
            subject = _required_string(payload, "subject")
            argv = _required_string_list(payload, "argv")
            decision = manager.explain_command(subject, argv)
            action = decision["final"]["action"]
            if action != "allow":
                self._send_json(403, {"decision": decision, "status": "blocked"})
                return

            workspace = _optional_string(payload, "workspace") or os.environ.get("MCON_WORKSPACE", "/workspace")
            cwd = _optional_string(payload, "cwd") or workspace
            network = _optional_string(payload, "network") or "none"
            session_id = _agent_scoped_session_id(subject, _optional_string(payload, "session_id") or "default")
            session_root = os.environ.get("MCON_SESSION_ROOT", "/mcon/session-fs")
            spec = {
                "argv": argv,
                "cwd": cwd,
                "env": _optional_env(payload),
                "sandbox": manager.sandbox_spec(
                    subject,
                    workspace=workspace,
                    network=network,
                    session_id=session_id,
                    session_root=session_root,
                ),
            }
            try:
                completed = subprocess.run(
                    ["mcon-executor"],
                    input=json.dumps(spec),
                    text=True,
                    capture_output=True,
                    timeout=_optional_timeout(payload),
                    env=_executor_environment(),
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                self._send_json(
                    504,
                    {
                        "decision": decision,
                        "status": "timeout",
                        "stdout": error.stdout or "",
                        "stderr": error.stderr or "",
                    },
                )
                return
            self._send_json(
                200,
                {
                    "decision": decision,
                    "returncode": completed.returncode,
                    "session_id": session_id,
                    "status": "succeeded" if completed.returncode == 0 else "failed",
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
            )

        def _handle_auth_get(self, path: str) -> None:
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "auth"] and parts[3] == "status":
                self._send_json(200, auth_manager.status(parts[2]))
                return
            if len(parts) == 5 and parts[:2] == ["api", "auth"] and parts[3] == "device-login":
                self._send_json(200, auth_manager.get_device_login(parts[2], parts[4]))
                return
            self._send_json(404, {"error": "not_found"})

        def _handle_auth_post(self, path: str) -> None:
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "auth"] and parts[3] == "device-login":
                self._send_json(202, auth_manager.start_device_login(parts[2]))
                return
            self._send_json(404, {"error": "not_found"})

        def _read_json(self) -> dict[str, Any]:
            content_length = int(self.headers.get("content-length") or "0")
            if content_length <= 0:
                return {}
            raw = self.rfile.read(content_length)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer((host, port), Handler)
    actual_host, actual_port = httpd.server_address
    print(f"mcon server listening on http://{actual_host}:{actual_port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _required_string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings")
    return value


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_env(payload: dict[str, Any]) -> dict[str, str]:
    value = payload.get("env")
    if value is None:
        return {}
    if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise ValueError("env must be an object of string keys and string values")
    return dict(value)


def _optional_timeout(payload: dict[str, Any]) -> float:
    value = payload.get("timeout_seconds", 30)
    if not isinstance(value, int | float) or value <= 0:
        raise ValueError("timeout_seconds must be a positive number")
    return float(value)


def _executor_environment() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")}


def _agent_scoped_session_id(subject: str, session_id: str) -> str:
    scoped = f"{subject}--{session_id}"
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", scoped)
    if not normalized:
        raise ValueError("session_id must include at least one supported character")
    return normalized[:120]
