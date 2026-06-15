"""Thin adapter for the Codex CLI."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path


DEFAULT_CODEX_HOME = Path("/mcon/codex-home")
DEFAULT_WORKSPACE = Path("/workspace")


def run_device_login() -> int:
    """Run Codex device-code login in the current terminal."""

    return _run_passthrough(["codex", "login", "--device-auth"])


def run_exec(prompt: str, *, workspace: Path | None = None, json_stream: bool = True) -> int:
    """Run `codex exec` with the saved Codex login."""

    command = ["codex", "exec"]
    if json_stream:
        command.append("--json")
    command.append(prompt)
    return _run_passthrough(command, cwd=workspace or _workspace_path())


def _run_passthrough(command: list[str], *, cwd: Path | None = None) -> int:
    environment = codex_environment(os.environ.items())
    try:
        process = subprocess.Popen(command, cwd=cwd, env=environment)
    except FileNotFoundError:
        print("codex command not found in PATH", file=sys.stderr)
        return 127
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return process.wait()


def codex_environment(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    environment = dict(items)
    environment.setdefault("CODEX_HOME", str(DEFAULT_CODEX_HOME))
    environment.setdefault("MCON_WORKSPACE", str(DEFAULT_WORKSPACE))
    return environment


def workspace_path() -> Path:
    return Path(os.environ.get("MCON_WORKSPACE", str(DEFAULT_WORKSPACE)))


_codex_environment = codex_environment
_workspace_path = workspace_path
