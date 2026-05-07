# 説明: mcon server API 用の最小ターミナル frontend。

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


# 説明: mcon server API を使う対話型ターミナル client を起動する。
# 引数: runtime_config は server 起動用 runtime 設定。server_url、session_id、backend、model、title は接続先と session 作成条件。
# 返り値: TUI session のプロセス終了コード。
def run_tui(
    runtime_config: RuntimeConfig,
    *,
    server_url: str | None = None,
    session_id: str | None = None,
    backend: str | None = None,
    model: str | None = None,
    title: str | None = None,
) -> int:
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


# 説明: 正常な server URL を返し、必要なら常駐ホスト server を起動する。
# 引数: runtime_config は server 起動用 runtime 設定。
# 返り値: server API の HTTP base URL。
def _ensure_server(runtime_config: RuntimeConfig) -> str:
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
            raise RuntimeError("mcon server が healthy になる前に終了しました")
        runtime_data = read_runtime(runtime_config.runtime_path)
        runtime_url = runtime_data.get("server_url")
        if runtime_url and _server_is_healthy(str(runtime_url)):
            return str(runtime_url)
        time.sleep(0.1)
    raise RuntimeError("mcon server が 10 秒以内に healthy になりませんでした")


# 説明: server health endpoint が応答するか確認する。
# 引数: server_url は確認対象の HTTP base URL。
# 返り値: /health が valid JSON を返せば True、それ以外は False。
def _server_is_healthy(server_url: str) -> bool:
    try:
        _api_get(server_url, "/health")
        return True
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


# 説明: 現在の chat session を探すか作成する。
# 引数: server_url は server API の HTTP base URL。session_id、backend、model、title は session 選択または作成条件。
# 返り値: server が返した session metadata。
def _ensure_session(
    server_url: str,
    *,
    session_id: str | None,
    backend: str | None,
    model: str | None,
    title: str | None,
) -> dict[str, Any]:
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


# 説明: server API の JSON GET endpoint を呼び出す。
# 引数: server_url は server API の HTTP base URL。path は / で始まる API path。
# 返り値: response body から decode した JSON object。
def _api_get(server_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{server_url.rstrip('/')}{path}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


# 説明: server API の JSON POST endpoint を呼び出す。
# 引数: server_url は server API の HTTP base URL。path は / で始まる API path。payload は JSON 化する request body。
# 返り値: response body から decode した JSON object。HTTP error 時は RuntimeError を送出する。
def _api_post(server_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
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


# 説明: active session の概要を表示する。
# 引数: session は server が返した session metadata。
# 返り値: なし。
def _print_session(session: dict[str, Any]) -> None:
    print(f"session: {session['id']} backend={session['backend']} model={session['model']} title={session['title']}")


# 説明: TUI slash command の一覧を表示する。
# 引数: なし。
# 返り値: なし。
def _print_help() -> None:
    print("commands: /new [title], /sessions, /use <session-id>, /transcript, /help, /quit, exit")
    print("exit closes only this TUI client; use `mcon stop` to stop the server", file=sys.stderr)


# 説明: 利用可能な場合に readline editing を有効化し、特殊 key が escape bytes として表示されるのを避ける。
# 引数: なし。
# 返り値: なし。
def _configure_terminal_input() -> None:
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
