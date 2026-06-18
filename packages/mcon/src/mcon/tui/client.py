"""Minimal stdin/stdout TUI for continuous mcon sessions."""

from __future__ import annotations

import codecs
import json
import os
import secrets
import select
import shlex
import sys
import termios
import threading
import tty
import unicodedata
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

from mcon.text import utf8_safe


@dataclass
class TuiState:
    server_url: str
    provider: str = "codex"
    subject: str = "mcon.agent.coder"
    cwd: str = "/workspace"
    session_id: str = field(default_factory=lambda: f"tui-{uuid.uuid4().hex[:8]}")
    sandbox: str = "read-only"
    messages: list[dict[str, str]] = field(default_factory=list)
    active_chat_ids: set[str] = field(default_factory=set)
    pending_chats: list[dict[str, str]] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)
    output_lock: threading.RLock = field(default_factory=threading.RLock)
    input_prompt: str = "you> "
    input_buffer: list[str] = field(default_factory=list)
    input_cursor: int = 0
    input_active: bool = False
    input_suspended: int = 0


def run_tui(server_url: str) -> int:
    state = TuiState(server_url=server_url.rstrip("/"))
    print(f"mcon tui -> {state.server_url}")
    _print_help()
    while True:
        try:
            line = utf8_safe(_read_line(state)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in {"/quit", "/exit", "quit", "exit"}:
            return 0
        if line.startswith("/"):
            _handle_command(state, line)
            continue
        if line.startswith("!"):
            _explain_command(state, line[1:].strip())
            continue
        _chat(state, line)


def _print_help() -> None:
    print("chat: type a message and press enter")
    print("commands: ! <argv...>, /exec <argv...>, /provider <name>, /subject <name>, /cwd <path>, /session <id>, /sandbox <mode>, /clear, /status, /quit")


def _read_line(state: TuiState) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input(state.input_prompt)
    with state.output_lock:
        state.input_buffer = []
        state.input_cursor = 0
        state.input_active = True
        _redraw_input_locked(state)
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        tty.setraw(fd)
        while True:
            data = os.read(fd, 1)
            if not data:
                raise EOFError
            if data in {b"\r", b"\n"}:
                with state.output_lock:
                    line = "".join(state.input_buffer)
                    state.input_active = False
                    state.input_buffer = []
                    state.input_cursor = 0
                    sys.stdout.write("\r\033[2K")
                    sys.stdout.flush()
                return line
            if data == b"\x03":
                raise KeyboardInterrupt
            if data == b"\x04":
                with state.output_lock:
                    if not state.input_buffer:
                        state.input_active = False
                        raise EOFError
                    _delete_at_cursor(state)
                    _redraw_input_locked(state)
                continue
            if data == b"\x15":
                with state.output_lock:
                    state.input_buffer.clear()
                    state.input_cursor = 0
                    _redraw_input_locked(state)
                continue
            if data == b"\x17":
                with state.output_lock:
                    _delete_previous_word(state)
                    _redraw_input_locked(state)
                continue
            if data in {b"\x7f", b"\b"}:
                with state.output_lock:
                    _backspace(state)
                    _redraw_input_locked(state)
                continue
            if data == b"\x01":
                with state.output_lock:
                    state.input_cursor = 0
                    _redraw_input_locked(state)
                continue
            if data == b"\x05":
                with state.output_lock:
                    state.input_cursor = len(state.input_buffer)
                    _redraw_input_locked(state)
                continue
            if data == b"\x1b":
                _handle_escape_sequence(state, fd)
                decoder.reset()
                continue
            text = decoder.decode(data)
            if not text:
                continue
            with state.output_lock:
                for character in text:
                    if character.isprintable():
                        state.input_buffer.insert(state.input_cursor, character)
                        state.input_cursor += 1
                _redraw_input_locked(state)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        with state.output_lock:
            state.input_active = False


def _handle_escape_sequence(state: TuiState, fd: int) -> None:
    sequence = bytearray(b"\x1b")
    while len(sequence) < 8:
        ready, _, _ = select.select([fd], [], [], 0.02)
        if not ready:
            break
        sequence.extend(os.read(fd, 1))
        if sequence[-1:] in {b"~", b"A", b"B", b"C", b"D", b"F", b"H"}:
            break
    value = bytes(sequence)
    with state.output_lock:
        if value in {b"\x1b[D", b"\x1bOD"}:
            state.input_cursor = max(0, state.input_cursor - 1)
        elif value in {b"\x1b[C", b"\x1bOC"}:
            state.input_cursor = min(len(state.input_buffer), state.input_cursor + 1)
        elif value in {b"\x1b[H", b"\x1bOH", b"\x1b[1~", b"\x1b[7~"}:
            state.input_cursor = 0
        elif value in {b"\x1b[F", b"\x1bOF", b"\x1b[4~", b"\x1b[8~"}:
            state.input_cursor = len(state.input_buffer)
        elif value == b"\x1b[3~":
            _delete_at_cursor(state)
        _redraw_input_locked(state)


def _backspace(state: TuiState) -> None:
    if state.input_cursor <= 0:
        return
    del state.input_buffer[state.input_cursor - 1]
    state.input_cursor -= 1


def _delete_at_cursor(state: TuiState) -> None:
    if state.input_cursor < len(state.input_buffer):
        del state.input_buffer[state.input_cursor]


def _delete_previous_word(state: TuiState) -> None:
    cursor = state.input_cursor
    while cursor > 0 and state.input_buffer[cursor - 1].isspace():
        cursor -= 1
    while cursor > 0 and not state.input_buffer[cursor - 1].isspace():
        cursor -= 1
    del state.input_buffer[cursor : state.input_cursor]
    state.input_cursor = cursor


def _redraw_input_locked(state: TuiState) -> None:
    if not state.input_active or state.input_suspended:
        return
    text = "".join(state.input_buffer)
    right = "".join(state.input_buffer[state.input_cursor :])
    sys.stdout.write("\r\033[2K")
    sys.stdout.write(f"{state.input_prompt}{text}")
    remaining_width = _display_width(right)
    if remaining_width:
        sys.stdout.write(f"\033[{remaining_width}D")
    sys.stdout.flush()


def _display_width(value: str) -> int:
    width = 0
    for character in value:
        if unicodedata.combining(character):
            continue
        if unicodedata.category(character)[0] == "C":
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
    return width


def _handle_command(state: TuiState, line: str) -> None:
    parts = shlex.split(line)
    command = parts[0]
    args = parts[1:]
    session_setting_commands = {"/clear", "/subject", "/provider", "/cwd", "/session", "/sandbox"}
    if command in session_setting_commands and _session_busy(state):
        print("cannot change session settings while chat messages are active or pending")
        return
    if command == "/help":
        _print_help()
    elif command == "/status":
        with state.lock:
            active = ",".join(sorted(state.active_chat_ids)) or "none"
            message_count = len(state.messages)
            pending_count = len(state.pending_chats)
        print(f"provider={state.provider} subject={state.subject} cwd={state.cwd} session={state.session_id} sandbox={state.sandbox} messages={message_count} active={active} pending={pending_count}")
    elif command == "/clear":
        state.messages.clear()
        print("history cleared")
    elif command == "/subject" and len(args) == 1:
        state.subject = args[0]
        print(f"subject={state.subject}")
    elif command == "/provider" and len(args) == 1:
        state.provider = args[0]
        print(f"provider={state.provider}")
    elif command == "/cwd" and len(args) == 1:
        state.cwd = args[0]
        print(f"cwd={state.cwd}")
    elif command == "/session" and len(args) == 1:
        state.session_id = args[0]
        state.messages.clear()
        print(f"session={state.session_id}")
    elif command == "/sandbox" and args == ["read-only"]:
        state.sandbox = "read-only"
        print(f"sandbox={state.sandbox}")
    elif command == "/sandbox" and args == ["workspace-write"]:
        print("workspace-write is disabled until provider tools are enforced by mcon executor")
    elif command == "/exec" and args:
        _exec_command(state, args)
    else:
        print("unknown command; use /help")


def _session_busy(state: TuiState) -> bool:
    with state.lock:
        return bool(state.active_chat_ids or state.pending_chats)


def _chat(state: TuiState, text: str) -> None:
    text = utf8_safe(text)
    chat_id = _new_chat_id()
    user_message = {"role": "user", "content": text, "chat_id": chat_id}
    with state.lock:
        should_start = not state.active_chat_ids
        if should_start:
            state.messages.append(user_message)
            state.active_chat_ids.add(chat_id)
        else:
            state.pending_chats.append(user_message)
    _print_line(state, f"queued {chat_id}")
    if should_start:
        _start_chat_worker(state, user_message)


def _start_chat_worker(state: TuiState, user_message: dict[str, str]) -> None:
    chat_id = user_message["chat_id"]
    with state.lock:
        messages = list(state.messages)
        payload = {
            "chat_id": chat_id,
            "provider": state.provider,
            "subject": state.subject,
            "cwd": state.cwd,
            "session_id": state.session_id,
            "sandbox": state.sandbox,
            "messages": messages,
        }
    thread = threading.Thread(target=_chat_worker, args=(state, chat_id, user_message, payload), daemon=True)
    thread.start()


def _chat_worker(state: TuiState, chat_id: str, user_message: dict[str, str], payload: dict[str, object]) -> None:
    assistant_parts: list[str] = []
    _print_line(state, f"assistant {chat_id}> waiting")
    started_output = False
    try:
        for event in _post_stream(state.server_url, "/api/chat/stream", payload):
            output = _event_text(event)
            if output:
                assistant_parts.append(output)
                if not started_output:
                    _print_prefix(state, f"assistant {chat_id}> ")
                    started_output = True
                _print_text(state, output)
            elif event.get("type") == "stderr":
                _print_line(state, f"[{chat_id} stderr] {event.get('text', '')}")
            elif event.get("type") == "exit" and event.get("returncode") != 0:
                _print_line(state, f"[{chat_id} exit {event.get('returncode')}]")
    except RuntimeError as error:
        if started_output:
            _finish_output_stream(state)
        with state.lock:
            if user_message in state.messages:
                state.messages.remove(user_message)
            state.active_chat_ids.discard(chat_id)
            next_message = _pop_next_chat_locked(state)
        _print_line(state, f"{chat_id} error: {error}")
        if next_message:
            _start_chat_worker(state, next_message)
        return
    if started_output:
        _finish_output_stream(state)
    assistant = "".join(assistant_parts).strip()
    with state.lock:
        if assistant:
            state.messages.append({"role": "assistant", "content": assistant, "chat_id": chat_id})
        state.active_chat_ids.discard(chat_id)
        next_message = _pop_next_chat_locked(state)
    if next_message:
        _start_chat_worker(state, next_message)


def _pop_next_chat_locked(state: TuiState) -> dict[str, str] | None:
    if not state.pending_chats:
        return None
    next_message = state.pending_chats.pop(0)
    state.messages.append(next_message)
    state.active_chat_ids.add(next_message["chat_id"])
    return next_message


def _new_chat_id() -> str:
    return f"#{secrets.randbelow(100000):05d}"


def _print_line(state: TuiState, text: str) -> None:
    with state.output_lock:
        if state.input_active:
            sys.stdout.write("\r\033[2K")
        print(text, flush=True)
        _redraw_input_locked(state)


def _print_prefix(state: TuiState, text: str) -> None:
    with state.output_lock:
        if state.input_active:
            sys.stdout.write("\r\033[2K")
            state.input_suspended += 1
        elif not sys.stdin.isatty():
            print()
        print(text, end="", flush=True)


def _print_text(state: TuiState, text: str) -> None:
    with state.output_lock:
        print(text, end="", flush=True)


def _finish_output_stream(state: TuiState) -> None:
    with state.output_lock:
        print("", flush=True)
        if state.input_suspended:
            state.input_suspended -= 1
        _redraw_input_locked(state)


def _explain_command(state: TuiState, command_line: str) -> None:
    if not command_line:
        return
    argv = shlex.split(command_line)
    try:
        response = _post(
            state.server_url,
            "/api/policy/explain-command",
            {"subject": state.subject, "argv": argv},
        )
    except RuntimeError as error:
        print(f"error: {error}")
        return
    final = response["final"]
    print(f"{final['action']}: {final.get('permission', final.get('reason', ''))}")


def _exec_command(state: TuiState, argv: list[str]) -> None:
    try:
        response = _post(
            state.server_url,
            "/api/exec",
            {
                "subject": state.subject,
                "argv": argv,
                "cwd": state.cwd,
                "session_id": state.session_id,
                "timeout_seconds": 60,
            },
        )
    except RuntimeError as error:
        print(f"error: {error}")
        return
    print(f"status={response.get('status')} returncode={response.get('returncode')}")
    stdout = response.get("stdout")
    stderr = response.get("stderr")
    if stdout:
        print(str(stdout), end="" if str(stdout).endswith("\n") else "\n")
    if stderr:
        print(str(stderr), end="" if str(stderr).endswith("\n") else "\n")


def _post(server_url: str, path: str, payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        f"{server_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        raise RuntimeError(body) from error
    except (urllib.error.URLError, OSError, json.JSONDecodeError, UnicodeError) as error:
        raise RuntimeError(str(error)) from error
    if not isinstance(decoded, dict):
        raise RuntimeError("server returned non-object JSON")
    return decoded


def _post_stream(server_url: str, path: str, payload: dict[str, object]) -> Iterator[dict[str, Any]]:
    request = urllib.request.Request(
        f"{server_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=None) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                decoded = json.loads(line)
                if isinstance(decoded, dict):
                    yield decoded
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        raise RuntimeError(body) from error
    except (urllib.error.URLError, OSError, json.JSONDecodeError, UnicodeError) as error:
        raise RuntimeError(str(error)) from error


def _event_text(event: dict[str, Any]) -> str | None:
    if event.get("type") == "stdout":
        text = event.get("text")
        return text if isinstance(text, str) else None
    if event.get("type") != "codex_event":
        return None
    inner = event.get("event")
    if not isinstance(inner, dict):
        return None
    event_type = inner.get("type")
    if isinstance(inner.get("delta"), str):
        return inner["delta"]
    if event_type in {"agent_message", "assistant_message", "message"}:
        for key in ("message", "content", "text"):
            value = inner.get(key)
            if isinstance(value, str):
                return value
    item = inner.get("item")
    if isinstance(item, dict) and item.get("type") in {"agent_message", "assistant_message", "message"}:
        text = item.get("text")
        return text if isinstance(text, str) else None
    return None
