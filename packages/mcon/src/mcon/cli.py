"""Command line interface for mcon."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .adapters import codex
from .policy import PolicyManager
from .server import run_server
from .tui import run_tui


DEFAULT_PLUGIN_ROOT = Path("configs/plugins/mcon")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcon")
    parser.add_argument("--plugin-root", type=Path, default=DEFAULT_PLUGIN_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="start the mcon server")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    explain_parser = subparsers.add_parser("explain", help="explain a command policy decision")
    explain_parser.add_argument("subject")
    explain_parser.add_argument("argv", nargs=argparse.REMAINDER)

    file_parser = subparsers.add_parser("check-file", help="check a file permission decision")
    file_parser.add_argument("subject")
    file_parser.add_argument("operation", choices=["stat", "list", "read", "create", "write", "delete", "rename"])
    file_parser.add_argument("path")

    tui_parser = subparsers.add_parser("tui", help="start a minimal terminal UI")
    tui_parser.add_argument("--server", default="http://127.0.0.1:8765")

    subparsers.add_parser("codex-login", help="sign in to Codex with device-code authentication")

    codex_exec_parser = subparsers.add_parser("codex-exec", help="run Codex CLI through the saved device login")
    codex_exec_parser.add_argument("prompt")
    codex_exec_parser.add_argument("--workspace", type=Path, default=None)
    codex_exec_parser.add_argument("--no-json", action="store_true", help="disable Codex JSONL output")

    args = parser.parse_args(argv)
    plugin_root = args.plugin_root.resolve()

    if args.command == "serve":
        run_server(plugin_root=plugin_root, host=args.host, port=args.port)
        return 0
    if args.command == "explain":
        command_argv = _normalize_remainder(args.argv)
        manager = PolicyManager.load(plugin_root)
        print(json.dumps(manager.explain_command(args.subject, command_argv), ensure_ascii=False, indent=2))
        return 0
    if args.command == "check-file":
        manager = PolicyManager.load(plugin_root)
        print(json.dumps(manager.explain_file(args.subject, args.operation, args.path), ensure_ascii=False, indent=2))
        return 0
    if args.command == "tui":
        return run_tui(args.server)
    if args.command == "codex-login":
        return codex.run_device_login()
    if args.command == "codex-exec":
        return codex.run_exec(args.prompt, workspace=args.workspace, json_stream=not args.no_json)
    return 1


def _normalize_remainder(argv: list[str]) -> list[str]:
    if argv and argv[0] == "--":
        return argv[1:]
    return argv
