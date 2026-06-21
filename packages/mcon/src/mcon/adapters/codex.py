"""Thin adapter for the Codex CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

from mcon.text import utf8_safe


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


def popen_exec_stream(
    prompt: str,
    *,
    workspace: Path,
    sandbox_spec: dict[str, object],
) -> subprocess.Popen[str]:
    """Start Codex inside the mcon executor namespace."""

    runtime_home = _prepare_codex_home()
    command = [
        "codex",
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--disable",
        "multi_agent",
        "--ephemeral",
        "--ignore-user-config",
        "--cd",
        str(workspace),
        "-",
    ]
    sandbox = dict(sandbox_spec)
    sandbox["runtime_files"] = _codex_runtime_files(runtime_home)
    spec = {
        "argv": command,
        "cwd": str(workspace),
        "env": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
            "CODEX_HOME": str(runtime_home),
            "MCON_WORKSPACE": str(workspace),
        },
        "stdin": utf8_safe(prompt),
        "sandbox": sandbox,
    }
    try:
        process = subprocess.Popen(
            ["mcon-executor"],
            cwd=workspace,
            env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except BaseException:
        shutil.rmtree(runtime_home, ignore_errors=True)
        raise
    process._mcon_process_group = process.pid  # type: ignore[attr-defined]
    process._mcon_runtime_dir = str(runtime_home)  # type: ignore[attr-defined]
    process._mcon_credential_paths = [str(runtime_home / "auth.json")]  # type: ignore[attr-defined]
    assert process.stdin is not None
    try:
        process.stdin.write(json.dumps(spec))
        process.stdin.close()
    except BrokenPipeError:
        pass
    return process


def _prepare_codex_home() -> Path:
    runtime_root = Path("/mcon/provider-runtime")
    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime_home = Path(tempfile.mkdtemp(prefix="codex-", dir=runtime_root))
    try:
        for name in ("auth.json", "installation_id"):
            source = DEFAULT_CODEX_HOME / name
            if source.is_file():
                shutil.copy2(source, runtime_home / name)
    except BaseException:
        shutil.rmtree(runtime_home, ignore_errors=True)
        raise
    return runtime_home


def _codex_runtime_files(runtime_home: Path) -> list[dict[str, str]]:
    paths = [
        DEFAULT_CODEX_HOME / "packages",
        Path("/etc/ssl/certs"),
        Path("/etc/resolv.conf"),
        Path("/etc/hosts"),
        Path("/etc/nsswitch.conf"),
        Path("/etc/gai.conf"),
    ]
    result = [{"action": "edit", "path": str(runtime_home)}]
    result.extend({"action": "read", "path": str(path)} for path in paths if path.exists())
    return result


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
