"""Small standard-library HTTP server for the initial mcon control plane."""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from mcon.adapters import codex
from mcon.auth import AuthManager, AuthProviderError
from mcon.policy import PolicyError, PolicyManager
from mcon.text import utf8_safe


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
                if path == "/api/chat/stream":
                    self._handle_chat_stream(payload)
                    return
                if path == "/api/chat/codex/stream":
                    payload.setdefault("provider", "codex")
                    self._handle_chat_stream(payload)
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

            try:
                process = codex.popen_exec_stream(prompt, workspace=cwd, sandbox=sandbox)
            except FileNotFoundError as error:
                raise ValueError("codex command not found in PATH") from error
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
                }
            )
            _stream_process_as_ndjson(process, write_event)

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
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")}


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


def _stream_process_as_ndjson(process: subprocess.Popen[str], write_event: Any) -> None:
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
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError:
                    write_event({"type": "stdout", "text": line})
                else:
                    write_event({"type": "codex_event", "event": decoded})
            else:
                write_event({"type": "stderr", "text": line})
        returncode = process.wait()
        write_event({"type": "exit", "returncode": returncode})
    finally:
        _terminate_process(process)


def _terminate_process(process: subprocess.Popen[str]) -> None:
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
