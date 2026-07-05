"""Python executor for ModelConsole sandbox specs."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any


@dataclass
class FileRule:
    action: str
    path: str


@dataclass
class SandboxSpec:
    enabled: bool = False
    workspace: str = ""
    network: str = ""
    session_id: str = ""
    session_root: str = ""
    files: list[FileRule] = field(default_factory=list)
    runtime_files: list[FileRule] = field(default_factory=list)


@dataclass
class ExecSpec:
    argv: list[str]
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    stdin: str = ""
    sandbox: SandboxSpec = field(default_factory=SandboxSpec)


@dataclass
class CommandSpec:
    argv: list[str]
    cwd: str | None
    env: dict[str, str]


def main() -> int:
    try:
        run(sys.stdin, sys.stdout, sys.stderr)
    except Exception as error:
        print(f"mcon.executor: {error}", file=sys.stderr)
        return 1
    return 0


def run(stdin: IO[str], stdout: IO[str], stderr: IO[str]) -> None:
    spec = parse_exec_spec(json.load(stdin))
    if not spec.argv or not spec.argv[0]:
        raise ValueError("argv must contain a command")
    command = build_command(spec)
    completed = subprocess.run(
        command.argv,
        input=spec.stdin if spec.stdin else None,
        text=True,
        cwd=command.cwd,
        env=command.env,
        stdout=stdout,
        stderr=stderr,
        check=False,
    )
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, command.argv)
    if spec.sandbox.enabled:
        apply_session_writes(spec)


def parse_exec_spec(raw: dict[str, Any]) -> ExecSpec:
    if not isinstance(raw, dict):
        raise ValueError("exec spec must be a JSON object")
    sandbox = raw.get("sandbox") or {}
    if not isinstance(sandbox, dict):
        raise ValueError("sandbox must be an object")
    argv = raw.get("argv") or []
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise ValueError("argv must be a list of strings")
    env = raw.get("env") or {}
    if not isinstance(env, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()):
        raise ValueError("env must be an object of string keys and string values")
    stdin = raw.get("stdin") or ""
    if not isinstance(stdin, str):
        raise ValueError("stdin must be a string")
    cwd = raw.get("cwd") or ""
    if not isinstance(cwd, str):
        raise ValueError("cwd must be a string")
    for key in ("workspace", "network", "session_id", "session_root"):
        value = sandbox.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"sandbox.{key} must be a string")
    return ExecSpec(
        argv=list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=stdin,
        sandbox=SandboxSpec(
            enabled=bool(sandbox.get("enabled")),
            workspace=sandbox.get("workspace") or "",
            network=sandbox.get("network") or "",
            session_id=sandbox.get("session_id") or "",
            session_root=sandbox.get("session_root") or "",
            files=parse_file_rules(sandbox.get("files") or [], "files"),
            runtime_files=parse_file_rules(sandbox.get("runtime_files") or [], "runtime_files"),
        ),
    )


def parse_file_rules(raw: Any, name: str) -> list[FileRule]:
    if not isinstance(raw, list):
        raise ValueError(f"sandbox.{name} must be a list")
    rules: list[FileRule] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"sandbox.{name}[{index}] must be an object")
        action = item.get("action")
        path = item.get("path")
        if not isinstance(action, str) or not isinstance(path, str):
            raise ValueError(f"sandbox.{name}[{index}] must include string action and path")
        rules.append(FileRule(action, path))
    return rules


def build_command(spec: ExecSpec) -> CommandSpec:
    if not spec.argv or not spec.argv[0]:
        raise ValueError("argv must contain a command")
    env = build_env(spec.env)
    if not spec.sandbox.enabled:
        return CommandSpec(argv=spec.argv, cwd=spec.cwd or None, env=env)

    workspace = clean_absolute_path(spec.sandbox.workspace or "/workspace")
    cwd = clean_absolute_path(spec.cwd or workspace)
    if not path_within(workspace, cwd):
        raise ValueError(f"cwd must be inside workspace: {cwd}")
    return CommandSpec(argv=["bwrap", *build_bubblewrap_args(spec, workspace, cwd)], cwd=None, env=env)


def build_env(env: dict[str, str]) -> dict[str, str]:
    result = dict(env)
    result.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    return result


def build_bubblewrap_args(spec: ExecSpec, workspace: str, cwd: str) -> list[str]:
    network_mode = spec.sandbox.network or "none"
    if network_mode not in {"none", "inherit"}:
        raise ValueError(f"unsupported sandbox network mode: {network_mode}")
    session_root = clean_absolute_path(spec.sandbox.session_root or "/mcon/session-fs")

    args = [
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/etc",
        "--dir",
        "/etc/ssl",
        "--dir",
        "/mcon",
    ]
    args.extend(existing_read_only_binds(["/usr", "/bin", "/lib", "/lib64"]))
    args.extend(["--dir", workspace])
    if network_mode == "none":
        args.append("--unshare-net")

    for rule in sorted(spec.sandbox.files, key=lambda item: len(item.path)):
        args.extend(build_file_rule_args(rule, workspace, session_root, spec.sandbox.session_id))
    for rule in spec.sandbox.runtime_files:
        args.extend(build_runtime_file_rule_args(rule, workspace))
    args.extend(["--chdir", cwd, "--", *spec.argv])
    return args


def existing_read_only_binds(paths: list[str]) -> list[str]:
    args: list[str] = []
    for path in paths:
        if Path(path).exists():
            args.extend(["--ro-bind", path, path])
    return args


def build_runtime_file_rule_args(rule: FileRule, workspace: str) -> list[str]:
    path = clean_absolute_path(rule.path)
    if path_within(workspace, path):
        raise ValueError(f"runtime file rule must be outside workspace: {path}")
    if not allowed_runtime_path(path):
        raise ValueError(f"runtime file rule path is not allowlisted: {path}")
    if rule.action == "read":
        return ["--ro-bind-try", path, path]
    if rule.action == "edit":
        if not path_within("/mcon/provider-runtime", path):
            raise ValueError(f"writable runtime file rule must be inside provider runtime: {path}")
        return ["--bind-try", path, path]
    raise ValueError(f"unsupported runtime file rule action: {rule.action}")


def allowed_runtime_path(path: str) -> bool:
    if path_within("/mcon/codex-home/packages", path):
        return True
    if path_within("/mcon/provider-runtime", path):
        return True
    return path in {"/etc/ssl/certs", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/gai.conf"}


def build_file_rule_args(rule: FileRule, workspace: str, session_root: str, session_id: str) -> list[str]:
    path = clean_absolute_path(rule.path)
    if not path_within(workspace, path):
        raise ValueError(f"file rule path must be inside workspace: {path}")
    if rule.action == "read":
        return ["--ro-bind-try", path, path]
    if rule.action == "edit":
        return ["--bind-try", path, path]
    if rule.action == "write":
        source = session_write_source(session_root, session_id, workspace, path)
        Path(source).mkdir(parents=True, exist_ok=True, mode=0o700)
        return ["--bind", source, path]
    if rule.action == "deny":
        if not Path(path).exists():
            return []
        return ["--tmpfs", path]
    raise ValueError(f"unsupported file rule action: {rule.action}")


def apply_session_writes(spec: ExecSpec) -> None:
    workspace = clean_absolute_path(spec.sandbox.workspace or "/workspace")
    session_root = clean_absolute_path(spec.sandbox.session_root or "/mcon/session-fs")
    write_rules = [rule for rule in spec.sandbox.files if rule.action == "write"]
    if not write_rules:
        return
    validate_session_id(spec.sandbox.session_id)
    lock_path = apply_lock_path(session_root, workspace)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_file(lock_path):
        session_manifest = load_manifest(session_manifest_path(session_root, spec.sandbox.session_id), "session")
        workspace_manifest = load_manifest(workspace_manifest_path(session_root, workspace), "workspace")
        for rule in write_rules:
            destination = clean_absolute_path(rule.path)
            if not path_within(workspace, destination):
                raise ValueError(f"write rule path must be inside workspace: {destination}")
            source = session_write_source(session_root, spec.sandbox.session_id, workspace, destination)
            if not Path(source).exists():
                continue
            apply_session_tree(Path(source), Path(destination), Path(workspace), session_manifest, workspace_manifest)
        save_manifest(session_manifest_path(session_root, spec.sandbox.session_id), session_manifest)
        save_manifest(workspace_manifest_path(session_root, workspace), workspace_manifest)


@contextlib.contextmanager
def lock_file(path: Path) -> Any:
    import fcntl

    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def apply_session_tree(
    source_root: Path,
    destination_root: Path,
    workspace_root: Path,
    session_manifest: set[str],
    workspace_manifest: set[str],
) -> None:
    for source_path in sorted(source_root.rglob("*")):
        relative = source_path.relative_to(source_root)
        if not valid_relative_path(relative):
            raise ValueError(f"session path escaped source root: {relative}")
        if source_path.is_symlink():
            raise ValueError(f"session output symlinks are not allowed: {relative}")
        destination_path = destination_root / relative
        if not path_within(destination_root, destination_path):
            raise ValueError(f"session output escaped destination root: {destination_path}")
        workspace_relative = destination_path.relative_to(workspace_root)
        if not valid_relative_path(workspace_relative):
            raise ValueError(f"session output escaped workspace root: {destination_path}")
        key = workspace_relative.as_posix()
        if source_path.is_dir():
            apply_session_directory(destination_root, destination_path, key, session_manifest, workspace_manifest)
        elif source_path.is_file():
            apply_session_file(
                destination_root,
                source_path,
                destination_path,
                key,
                source_path.stat().st_mode & 0o777,
                session_manifest,
                workspace_manifest,
            )
        else:
            raise ValueError(f"unsupported session output file type: {relative}")


def apply_session_directory(
    destination_root: Path,
    destination_path: Path,
    key: str,
    session_manifest: set[str],
    workspace_manifest: set[str],
) -> None:
    reject_symlink_parent(destination_root, destination_path)
    if destination_path.exists() or destination_path.is_symlink():
        if destination_path.is_symlink():
            raise ValueError(f"refusing to write through host symlink: {destination_path}")
        if not destination_path.is_dir():
            raise ValueError(f"refusing to replace existing host file with directory: {destination_path}")
        return
    destination_path.mkdir(parents=True, mode=0o700, exist_ok=True)
    session_manifest.add(key)
    workspace_manifest.add(key)


def apply_session_file(
    destination_root: Path,
    source_path: Path,
    destination_path: Path,
    key: str,
    mode: int,
    session_manifest: set[str],
    workspace_manifest: set[str],
) -> None:
    reject_symlink_parent(destination_root, destination_path)
    if destination_path.exists() or destination_path.is_symlink():
        if destination_path.is_symlink():
            raise ValueError(f"refusing to write through host symlink: {destination_path}")
        if destination_path.is_dir():
            raise ValueError(f"refusing to replace existing host directory with file: {destination_path}")
        if key not in session_manifest and key not in workspace_manifest:
            raise ValueError(f"refusing to overwrite host path not created by mcon: {destination_path}")
    else:
        destination_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        session_manifest.add(key)
        workspace_manifest.add(key)
    temporary = destination_path.with_name(f".{destination_path.name}.tmp.{os.getpid()}")
    shutil.copyfile(source_path, temporary)
    os.chmod(temporary, mode)
    os.replace(temporary, destination_path)


def reject_symlink_parent(root: Path, path: Path) -> None:
    root = Path(os.path.abspath(os.path.normpath(root)))
    if root.is_symlink():
        raise ValueError(f"refusing to write below host symlink: {root}")
    parent = path.parent
    try:
        relative = parent.relative_to(root)
    except ValueError as error:
        raise ValueError(f"destination parent escaped root: {parent}") from error
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists():
            return
        if current.is_symlink():
            raise ValueError(f"refusing to write below host symlink: {current}")


def load_manifest(path: Path, kind: str) -> set[str]:
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"decode {kind} manifest: {error}") from error
    created = raw.get("created")
    if not isinstance(created, list):
        raise ValueError(f"decode {kind} manifest: created must be a list")
    result: set[str] = set()
    for item in created:
        if not isinstance(item, str) or not valid_relative_path(Path(item)):
            raise ValueError(f"invalid {kind} manifest path: {item}")
        result.add(item)
    return result


def save_manifest(path: Path, created: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps({"created": sorted(created)}, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def session_manifest_path(session_root: str, session_id: str) -> Path:
    return Path(session_root) / session_id / "manifest.json"


def workspace_manifest_path(session_root: str, workspace: str) -> Path:
    digest = hashlib.sha256(str(Path(workspace)).encode("utf-8")).hexdigest()
    return Path(session_root) / "_workspaces" / digest / "manifest.json"


def apply_lock_path(session_root: str, workspace: str) -> Path:
    digest = hashlib.sha256(str(Path(workspace)).encode("utf-8")).hexdigest()
    return Path(session_root) / "_locks" / f"{digest}.lock"


def clean_absolute_path(path: str) -> str:
    if not path:
        raise ValueError("path must not be empty")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError(f"path must be absolute: {path}")
    return os.path.abspath(os.path.normpath(path))


def session_write_source(session_root: str, session_id: str, workspace: str, path: str) -> str:
    validate_session_id(session_id)
    workspace_path = Path(workspace)
    path_obj = Path(path)
    try:
        relative = path_obj.relative_to(workspace_path)
    except ValueError as error:
        raise ValueError(f"write path must be inside workspace: {path}") from error
    if str(relative) == ".":
        relative = Path("_workspace")
    if not valid_relative_path(relative):
        raise ValueError(f"write path must be inside workspace: {path}")
    source = Path(session_root) / session_id / "fs" / relative
    if not path_within(Path(session_root) / session_id, source):
        raise ValueError(f"resolved session path escaped session root: {source}")
    return str(source)


def valid_relative_path(path: Path) -> bool:
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts


def validate_session_id(session_id: str) -> None:
    if not session_id:
        raise ValueError("sandbox.session_id is required when write permissions are used")
    for char in session_id:
        if char.isalnum() or char in {"_", "-"}:
            continue
        raise ValueError(f"sandbox.session_id contains unsupported character: {char!r}")


def path_within(root: str | Path, path: str | Path) -> bool:
    root_path = Path(os.path.abspath(os.path.normpath(root)))
    path_obj = Path(os.path.abspath(os.path.normpath(path)))
    return path_obj == root_path or root_path in path_obj.parents


if __name__ == "__main__":
    raise SystemExit(main())
