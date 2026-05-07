"""Minimal terminal frontend for the mcon server API."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from config import RuntimeConfig
from runtime import read_runtime


def run_tui(
    runtime_config: RuntimeConfig,
    *,
    server_url: str | None = None,
    session_id: str | None = None,
    backend: str | None = None,
    model: str | None = None,
    title: str | None = None,
) -> int:
    """Run an interactive terminal client backed by the mcon server.

    Args:
        runtime_config: Loaded runtime configuration for server startup.
        server_url: Existing server URL. ``None`` starts or reuses a local one.
        session_id: Existing session id to attach to.
        backend: Backend name for a newly created session.
        model: Model name for a newly created session.
        title: Session title for a newly created session.

    Returns:
        Process exit code for the TUI session.
    """

    _configure_terminal_input()
    resolved_server_url = server_url or _ensure_server(runtime_config)
    print(f"mcon tui -> {resolved_server_url}", file=sys.stderr)
    current_session = _ensure_session(
        resolved_server_url,
        session_id=session_id,
        backend=backend,
        model=model,
        title=title,
    )
    _print_session(current_session)
    _print_help()

    while True:
        try:
            user_input = input(f"mcon:{current_session['id'][:8]}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user_input:
            continue
        if user_input in {"/quit", "/exit", "quit", "exit"}:
            return 0
        if user_input == "/help":
            _print_help()
            continue
        if user_input == "/sessions":
            sessions = _api_get(resolved_server_url, "/api/sessions").get("sessions", [])
            for session in sessions:
                marker = "*" if session["id"] == current_session["id"] else " "
                print(f"{marker} {session['id']} {session['backend']} {session['model']} {session['title']}")
            continue
        if user_input.startswith("/new"):
            current_session = _api_post(
                resolved_server_url,
                "/api/sessions",
                {"backend": backend, "model": model, "title": user_input.removeprefix("/new").strip() or None},
            )["session"]
            _print_session(current_session)
            continue
        if user_input.startswith("/use "):
            requested_session_id = user_input.removeprefix("/use ").strip()
            current_session = _api_get(resolved_server_url, f"/api/sessions/{requested_session_id}")["session"]
            _print_session(current_session)
            continue
        if user_input == "/transcript":
            transcript = _api_get(resolved_server_url, f"/api/sessions/{current_session['id']}/transcript").get("messages", [])
            for message in transcript:
                print(f"{message['role']}: {message['content']}")
            continue

        try:
            result = _api_post(
                resolved_server_url,
                f"/api/sessions/{current_session['id']}/messages",
                {"content": user_input},
            )
        except RuntimeError as runtime_error:
            print(f"error: {runtime_error}", file=sys.stderr)
            continue
        current_session = result["session"]
        assistant_message = result.get("assistant_message", {})
        print(assistant_message.get("content", ""))


def _ensure_server(runtime_config: RuntimeConfig) -> str:
    """Return a healthy server URL, starting a persistent host server when needed.

    Args:
        runtime_config: Loaded runtime configuration for server startup.

    Returns:
        HTTP base URL for the server API.
    """

    runtime_data = read_runtime(runtime_config.runtime_path)
    runtime_url = runtime_data.get("server_url")
    if runtime_url and _server_is_healthy(str(runtime_url)):
        return str(runtime_url)

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mcon.cli",
            "--data-dir",
            str(runtime_config.data_directory),
            "serve",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("mcon server exited before becoming healthy")
        runtime_data = read_runtime(runtime_config.runtime_path)
        runtime_url = runtime_data.get("server_url")
        if runtime_url and _server_is_healthy(str(runtime_url)):
            return str(runtime_url)
        time.sleep(0.1)
    raise RuntimeError("mcon server did not become healthy within 10 seconds")


def _server_is_healthy(server_url: str) -> bool:
    """Check whether the server health endpoint responds.

    Args:
        server_url: HTTP base URL to probe.

    Returns:
        ``True`` when ``/health`` returns valid JSON, otherwise ``False``.
    """

    try:
        _api_get(server_url, "/health")
        return True
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def _ensure_session(
    server_url: str,
    *,
    session_id: str | None,
    backend: str | None,
    model: str | None,
    title: str | None,
) -> dict[str, Any]:
    """Find or create the current chat session.

    Args:
        server_url: HTTP base URL for the server API.
        session_id: Existing session id requested by the caller.
        backend: Backend name for session selection/creation.
        model: Model name for session selection/creation.
        title: Title for session creation.

    Returns:
        Session metadata returned by the server.
    """

    if session_id:
        return _api_get(server_url, f"/api/sessions/{session_id}")["session"]
    sessions = _api_get(server_url, "/api/sessions").get("sessions", [])
    if backend:
        normalized_backend = backend.strip().lower()
        for session in sessions:
            if str(session.get("backend", "")).strip().lower() != normalized_backend:
                continue
            if model and str(session.get("model", "")) != model:
                continue
            return session
        return _api_post(server_url, "/api/sessions", {"backend": backend, "model": model, "title": title})["session"]
    if sessions:
        return sessions[0]
    return _api_post(server_url, "/api/sessions", {"backend": backend, "model": model, "title": title})["session"]


def _api_get(server_url: str, path: str) -> dict[str, Any]:
    """Call a JSON GET endpoint on the server API.

    Args:
        server_url: HTTP base URL for the server API.
        path: Absolute API path beginning with ``/``.

    Returns:
        Decoded JSON object from the response body.
    """

    with urllib.request.urlopen(f"{server_url.rstrip('/')}{path}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _api_post(server_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Call a JSON POST endpoint on the server API.

    Args:
        server_url: HTTP base URL for the server API.
        path: Absolute API path beginning with ``/``.
        payload: JSON-serializable request payload.

    Returns:
        Decoded JSON object from the response body.

    Raises:
        RuntimeError: When the server returns an HTTP error response.
    """

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{server_url.rstrip('/')}{path}",
        data=body,
        method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=360) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as http_error:
        response_body = http_error.read().decode("utf-8")
        try:
            error_payload = json.loads(response_body)
        except json.JSONDecodeError:
            error_payload = {"message": response_body}
        raise RuntimeError(error_payload.get("message") or error_payload.get("error") or response_body) from http_error


def _print_session(session: dict[str, Any]) -> None:
    """Print the active session summary.

    Args:
        session: Session metadata returned by the server.

    Returns:
        ``None``.
    """

    print(f"session: {session['id']} backend={session['backend']} model={session['model']} title={session['title']}")


def _print_help() -> None:
    """Print available TUI slash commands.

    Args:
        None.

    Returns:
        ``None``.
    """

    print("commands: /new [title], /sessions, /use <session-id>, /transcript, /help, /quit, exit")
    print("exit closes only this TUI client; use `mcon stop` to stop the server", file=sys.stderr)


def _configure_terminal_input() -> None:
    """Enable readline editing when available so special keys do not print escape bytes.

    Args:
        None.

    Returns:
        ``None``.
    """

    if not sys.stdin.isatty():
        return
    try:
        import readline
    except ImportError:
        return
    for binding in (
        "set editing-mode emacs",
        "set enable-keypad on",
        "set input-meta on",
        "set output-meta on",
        "set convert-meta off",
    ):
        try:
            readline.parse_and_bind(binding)
        except ValueError:
            continue
