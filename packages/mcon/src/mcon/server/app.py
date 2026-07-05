"""Small standard-library HTTP server for the initial mcon control plane."""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from mcon.adapters import codex
from mcon.adapters.llm_proxy import convert_provider_event
from mcon.auth import AuthManager, AuthProviderError
from mcon.policy import PolicyError, PolicyManager
from mcon.run import RunService, RunState
from mcon.text import utf8_safe


def run_server(*, plugin_root: Path, host: str, port: int) -> None:
    manager = PolicyManager.load(plugin_root)
    auth_manager = AuthManager.default()
    run_service = RunService(
        root=Path(os.environ.get("MCON_RUN_ROOT", "/mcon/runs")),
        default_workspace=Path(os.environ.get("MCON_WORKSPACE", "/workspace")),
    )
    mcp_contexts: dict[str, dict[str, str]] = {}
    mcp_contexts_lock = threading.RLock()

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
                if path == "/api/policy/explain-network":
                    self._handle_explain_network(payload)
                    return
                if path == "/api/exec":
                    self._handle_exec(payload)
                    return
                if path == "/api/runs":
                    self._handle_create_run(payload)
                    return
                if path.startswith("/api/runs/"):
                    self._handle_run_post(path, payload)
                    return
                if path == "/api/chat/stream":
                    self._handle_chat_stream(payload)
                    return
                if path == "/api/chat/codex/stream":
                    payload.setdefault("provider", "codex")
                    self._handle_chat_stream(payload)
                    return
                if path == "/api/mcp":
                    self._handle_mcp(payload)
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

        def _handle_explain_network(self, payload: dict[str, Any]) -> None:
            subject = _required_string(payload, "subject")
            mode = _required_string(payload, "mode")
            purpose = _required_string(payload, "purpose")
            self._send_json(200, manager.explain_network(subject, mode, purpose=purpose))

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
            network_decision = manager.explain_network(subject, network, purpose="command")
            if not network_decision["final"]["allowed"]:
                self._send_json(
                    403,
                    {
                        "decision": decision,
                        "network_decision": network_decision,
                        "status": "blocked",
                    },
                )
                return
            file_argument_decision = manager.explain_command_file_arguments(subject, argv, cwd=cwd)
            if not file_argument_decision["allowed"]:
                self._send_json(
                    403,
                    {
                        "decision": decision,
                        "file_argument_decision": file_argument_decision,
                        "status": "blocked",
                    },
                )
                return
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
                    network_purpose="command",
                    session_id=session_id,
                    session_root=session_root,
                ),
            }
            try:
                completed = subprocess.run(
                    _executor_command(),
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

        def _handle_create_run(self, payload: dict[str, Any]) -> None:
            subject = _required_string(payload, "subject")
            resolved = manager.resolve_subject(subject)
            workspace_value = _optional_string(payload, "workspace")
            workspace = Path(workspace_value).resolve() if workspace_value else None
            record = run_service.create_run(
                subject=subject,
                workspace=workspace,
                policy_snapshot={
                    "subject": resolved.subject,
                    "policies": list(resolved.policy_names),
                },
            )
            self._send_json(201, record.to_json())

        def _handle_run_post(self, path: str, payload: dict[str, Any]) -> None:
            parts = path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "runs"]:
                self._send_json(404, {"error": "not_found"})
                return
            run_id = parts[2]
            action = parts[3]
            if action == "sync-in":
                record = run_service.sync_in(run_id)
                status = 409 if record.state == RunState.QUARANTINED else 200
                self._send_json(status, record.to_json())
                return
            if action == "diff":
                self._send_json(200, run_service.diff(run_id))
                return
            if action == "discard":
                self._send_json(200, run_service.discard(run_id).to_json())
                return
            self._send_json(404, {"error": "not_found"})

        def _handle_chat_stream(self, payload: dict[str, Any]) -> None:
            provider = _optional_string(payload, "provider") or "codex"
            if provider != "codex":
                raise ValueError(f"unsupported chat provider: {provider}")
            subject = _required_string(payload, "subject")
            resolved = manager.resolve_subject(subject)
            messages = _required_messages(payload)
            chat_id = _chat_id(payload, messages)
            cwd = _workspace_cwd(_optional_string(payload, "cwd"))
            sandbox = _chat_sandbox(payload)
            prompt = _prompt_from_messages(
                messages,
                chat_id=chat_id,
                policy_context=_policy_context(resolved, cwd=cwd, sandbox=sandbox),
            )
            session_id = _agent_scoped_session_id(subject, _optional_string(payload, "session_id") or "default")
            sandbox_spec = manager.sandbox_spec(
                subject,
                workspace=os.environ.get("MCON_WORKSPACE", "/workspace"),
                network="inherit",
                network_purpose="provider",
                file_access="read-only",
                session_id=session_id,
                session_root=os.environ.get("MCON_SESSION_ROOT", "/mcon/session-fs"),
            )
            mcp_token = secrets.token_urlsafe(32)
            with mcp_contexts_lock:
                mcp_contexts[mcp_token] = {
                    "subject": subject,
                    "parent_session_id": session_id,
                    "cwd": str(cwd),
                }

            try:
                process = codex.popen_exec_stream(
                    prompt,
                    workspace=cwd,
                    sandbox_spec=sandbox_spec,
                    mcp_token=mcp_token,
                    mcp_url=f"http://127.0.0.1:{self.server.server_address[1]}/api/mcp",
                )
            except FileNotFoundError as error:
                with mcp_contexts_lock:
                    mcp_contexts.pop(mcp_token, None)
                raise ValueError("Python executor command could not be started") from error
            try:
                self._start_ndjson(200)
                write_event = lambda event: self._write_ndjson({"chat_id": chat_id, **event})
                write_event(
                    {
                        "type": "start",
                        "orchestrator": "mcon",
                        "provider": provider,
                        "subject": subject,
                        "cwd": str(cwd),
                        "sandbox": sandbox,
                        "network": sandbox_spec["network"],
                    }
                )
                _stream_process_as_ndjson(process, write_event, provider=provider)
            finally:
                with mcp_contexts_lock:
                    mcp_contexts.pop(mcp_token, None)
                _terminate_process(process)

        def _handle_mcp(self, payload: dict[str, Any]) -> None:
            authorization = self.headers.get("authorization") or ""
            prefix = "Bearer "
            if not authorization.startswith(prefix):
                self._send_json(401, {"error": "missing_bearer_token"})
                return
            token = authorization[len(prefix) :]
            with mcp_contexts_lock:
                context = dict(mcp_contexts.get(token, {}))
            if not context:
                self._send_json(403, {"error": "invalid_mcp_token"})
                return

            response = _handle_mcp_request(manager, context, payload)
            if response is None:
                self.send_response(202)
                self.send_header("content-length", "0")
                self.end_headers()
                return
            self._send_json(200, response)

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

        def _start_ndjson(self, status: int) -> None:
            self.send_response(status)
            self.send_header("content-type", "application/x-ndjson; charset=utf-8")
            self.end_headers()

        def _write_ndjson(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
            self.wfile.write(body)
            self.wfile.flush()

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


def _required_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    value = payload.get("messages")
    if not isinstance(value, list) or not value:
        raise ValueError("messages must be a non-empty list")
    messages: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"messages[{index}] must be an object")
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant", "system"} or not isinstance(content, str) or not content:
            raise ValueError(f"messages[{index}] must include role and content")
        messages.append({"role": role, "content": utf8_safe(content)})
        chat_id = item.get("chat_id")
        if chat_id is not None:
            if not _valid_chat_id(chat_id):
                raise ValueError(f"messages[{index}].chat_id must look like #01234")
            messages[-1]["chat_id"] = utf8_safe(chat_id)
    return messages


def _prompt_from_messages(
    messages: list[dict[str, str]],
    *,
    chat_id: str | None = None,
    policy_context: str | None = None,
) -> str:
    lines = [
        "You are running inside ModelConsole TUI.",
        "Continue the conversation using the transcript below.",
        "Do not assume tool output that is not present in the transcript.",
        "Respect the ModelConsole policy context exactly; do not claim access beyond it.",
        "The built-in shell is disabled. Use the ModelConsole run_command_session MCP tool for commands.",
        "Each command tool call runs in a separate child session under the caller's policy.",
    ]
    if chat_id:
        lines.extend(
            [
                f"Current chat id: {chat_id}",
                f"Reply to the latest USER message for {chat_id}.",
            ]
        )
    if policy_context:
        lines.extend(["", "ModelConsole policy context:", policy_context])
    lines.extend(["", "Transcript:"])
    for message in messages:
        role = message["role"].upper()
        suffix = f" {message['chat_id']}" if message.get("chat_id") else ""
        lines.append(f"{role}{suffix}: {message['content']}")
    return "\n".join(lines)


def _chat_id(payload: dict[str, Any], messages: list[dict[str, str]]) -> str:
    value = payload.get("chat_id")
    if value is None and messages:
        value = messages[-1].get("chat_id")
    if value is None:
        return f"#{secrets.randbelow(100000):05d}"
    if not _valid_chat_id(value):
        raise ValueError("chat_id must look like #01234")
    return value


def _valid_chat_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"#[0-9A-Za-z_-]{1,32}", value) is not None


def _policy_context(resolved: Any, *, cwd: Path, sandbox: str) -> str:
    lines = [
        f"subject: {resolved.subject}",
        f"resolved_policies: {', '.join(resolved.policy_names) or '(none)'}",
        f"cwd: {cwd}",
        f"codex_cli_sandbox: {sandbox}",
        "command_policy: last matching permission wins; no match means deny; ask means do not execute automatically.",
    ]
    if resolved.commands:
        lines.append("commands:")
        for command in resolved.commands:
            argv = " ".join([command.command, *command.subcommands]).strip()
            details = [f"- {command.fqn}: action={command.action}", f"match={argv!r}"]
            if command.uses:
                details.append(f"uses={','.join(command.uses)}")
            target_branches = command.semantics.get("target_branches")
            if target_branches:
                details.append(f"target_branches={target_branches}")
            lines.append("  " + "; ".join(details))
    else:
        lines.append("commands: none")
    lines.append("file_policy: last matching path permission wins; no match means deny.")
    lines.append("file_actions: deny=no access; read=stat/list/read; write=create only; edit=stat/list/read/create/write.")
    if resolved.files:
        lines.append("files:")
        for file_permission in resolved.files:
            lines.append(
                f"  - {file_permission.fqn}: action={file_permission.action}; paths={', '.join(file_permission.paths)}"
            )
    else:
        lines.append("files: none")
    lines.append("network_policy: no matching permission means deny; disabled network is always allowed.")
    if resolved.network:
        lines.append("network:")
        for network_permission in resolved.network:
            lines.append(
                "  "
                + f"- {network_permission.fqn}: action={network_permission.action}; "
                + f"mode={network_permission.mode}; purposes={','.join(network_permission.purposes)}"
            )
    else:
        lines.append("network: none")
    if resolved.credentials:
        credential_names = ", ".join(f"{key}->{value.env_name}" for key, value in sorted(resolved.credentials.items()))
        lines.append(f"credential_policy_names_only: {credential_names}")
    else:
        lines.append("credential_policy_names_only: none")
    return "\n".join(lines)


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
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }


def _executor_command() -> list[str]:
    return [sys.executable, "-m", "mcon.executor"]


def _agent_scoped_session_id(subject: str, session_id: str) -> str:
    scoped = f"{subject}--{session_id}"
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", scoped)
    if not normalized:
        raise ValueError("session_id must include at least one supported character")
    return normalized[:120]


def _workspace_cwd(value: str | None) -> Path:
    workspace = Path(os.environ.get("MCON_WORKSPACE", "/workspace")).resolve()
    cwd = Path(value).resolve() if value else workspace
    if cwd != workspace and workspace not in cwd.parents:
        raise ValueError(f"cwd must be inside workspace: {cwd}")
    return cwd


def _chat_sandbox(payload: dict[str, Any]) -> str:
    sandbox = _optional_string(payload, "sandbox") or "read-only"
    if sandbox not in {"read-only", "workspace-write"}:
        raise ValueError("sandbox must be read-only or workspace-write")
    if sandbox == "workspace-write":
        raise ValueError("chat workspace-write is disabled until provider tools are enforced by mcon executor")
    return sandbox


def _handle_mcp_request(
    manager: PolicyManager,
    context: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    request_id = payload.get("id")
    method = payload.get("method")
    if not isinstance(method, str):
        return _mcp_error(request_id, -32600, "method must be a string")
    if request_id is None:
        return None
    if method == "initialize":
        params = payload.get("params")
        protocol_version = "2025-06-18"
        if isinstance(params, dict) and isinstance(params.get("protocolVersion"), str):
            protocol_version = params["protocolVersion"]
        return _mcp_result(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "modelconsole", "version": "0.1"},
                "instructions": (
                    "The parent agent has no shell. Use run_command_session for allowed commands. "
                    "Each call creates an isolated child execution session governed by the caller policy."
                ),
            },
        )
    if method == "ping":
        return _mcp_result(request_id, {})
    if method == "tools/list":
        return _mcp_result(request_id, {"tools": [_command_session_tool()]})
    if method == "tools/call":
        params = payload.get("params")
        if not isinstance(params, dict) or params.get("name") != "run_command_session":
            return _mcp_error(request_id, -32602, "unknown tool")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            return _mcp_error(request_id, -32602, "tool arguments must be an object")
        try:
            result = _run_command_session(manager, context, arguments)
        except (PolicyError, ValueError) as error:
            return _mcp_result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(error)}],
                    "isError": True,
                },
            )
        return _mcp_result(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False, sort_keys=True),
                    }
                ],
                "structuredContent": result,
                "isError": result["status"] != "succeeded",
            },
        )
    return _mcp_error(request_id, -32601, f"unsupported method: {method}")


def _command_session_tool() -> dict[str, Any]:
    return {
        "name": "run_command_session",
        "description": (
            "Start a separate isolated child session to run one command. "
            "The command, file, and network policies of the calling agent are enforced."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Command argv without shell parsing.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory inside the workspace.",
                },
                "network": {
                    "type": "string",
                    "enum": ["none", "inherit"],
                    "default": "none",
                },
                "timeout_seconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 120,
                    "default": 60,
                },
            },
            "required": ["argv"],
        },
    }


def _run_command_session(
    manager: PolicyManager,
    context: dict[str, str],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    argv = arguments.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ValueError("argv must be a non-empty list of non-empty strings")
    subject = context["subject"]
    decision = manager.explain_command(subject, argv)
    if decision["final"]["action"] != "allow":
        raise ValueError(f"command blocked: {decision['final']}")

    workspace = os.environ.get("MCON_WORKSPACE", "/workspace")
    requested_cwd = arguments.get("cwd")
    if requested_cwd is not None and not isinstance(requested_cwd, str):
        raise ValueError("cwd must be a string")
    cwd = str(_workspace_cwd(requested_cwd or context["cwd"]))
    network = arguments.get("network", "none")
    if not isinstance(network, str):
        raise ValueError("network must be a string")
    network_decision = manager.explain_network(subject, network, purpose="command")
    if not network_decision["final"]["allowed"]:
        raise ValueError(f"network blocked: {network_decision['final']}")
    file_decision = manager.explain_command_file_arguments(subject, argv, cwd=cwd)
    if not file_decision["allowed"]:
        raise ValueError(f"file access blocked: {file_decision['violations']}")

    timeout = arguments.get("timeout_seconds", 60)
    if not isinstance(timeout, int | float) or timeout <= 0 or timeout > 120:
        raise ValueError("timeout_seconds must be greater than 0 and at most 120")
    child_id = f"{context['parent_session_id']}--tool-{secrets.token_hex(6)}"
    spec = {
        "argv": argv,
        "cwd": cwd,
        "env": {},
        "sandbox": manager.sandbox_spec(
            subject,
            workspace=workspace,
            network=network,
            network_purpose="command",
            session_id=child_id,
            session_root=os.environ.get("MCON_SESSION_ROOT", "/mcon/session-fs"),
        ),
    }
    try:
        completed = subprocess.run(
            _executor_command(),
            input=json.dumps(spec),
            text=True,
            capture_output=True,
            timeout=float(timeout),
            env=_executor_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "session_id": child_id,
            "status": "timeout",
            "returncode": None,
            "stdout": error.stdout or "",
            "stderr": error.stderr or "",
        }
    return {
        "session_id": child_id,
        "status": "succeeded" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _mcp_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _mcp_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _stream_process_as_ndjson(process: subprocess.Popen[str], write_event: Any, *, provider: str = "codex") -> None:
    events: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def reader(kind: str, stream: Any) -> None:
        try:
            for line in stream:
                events.put((kind, line.rstrip("\n")))
        finally:
            events.put((kind, None))

    assert process.stdout is not None
    assert process.stderr is not None
    threads = [
        threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        closed = set()
        while len(closed) < 2:
            kind, line = events.get()
            if line is None:
                closed.add(kind)
                continue
            if kind == "stdout":
                _scrub_process_credentials(process)
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError:
                    write_event({"type": "stdout", "text": line})
                else:
                    for event in convert_provider_event(provider, decoded):
                        write_event(event)
            else:
                write_event({"type": "stderr", "text": line})
        returncode = process.wait()
        write_event({"type": "exit", "returncode": returncode})
    finally:
        _terminate_process(process)


def _terminate_process(process: subprocess.Popen[str]) -> None:
    try:
        process_group = getattr(process, "_mcon_process_group", None)
        if process_group is not None:
            _terminate_process_group(process, process_group)
            return
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    finally:
        runtime_dir = getattr(process, "_mcon_runtime_dir", None)
        if runtime_dir is not None:
            shutil.rmtree(runtime_dir, ignore_errors=True)


def _scrub_process_credentials(process: subprocess.Popen[str]) -> None:
    credential_paths = getattr(process, "_mcon_credential_paths", [])
    for path in credential_paths:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
    process._mcon_credential_paths = []  # type: ignore[attr-defined]


def _terminate_process_group(process: subprocess.Popen[str], process_group: int) -> None:
    if process.poll() is None:
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait()
