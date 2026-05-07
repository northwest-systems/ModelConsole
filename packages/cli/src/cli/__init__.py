"""mcon command line interface."""

from __future__ import annotations

import argparse
import json
import os
import getpass
import signal
import subprocess
import sys
import time
from pathlib import Path

from auth import (
    CLI_AUTH_FILE_NAME,
    login_claude_with_api_key,
    login_claude_with_oauth,
    login_codex_with_api_key,
    login_codex_with_oauth,
    login_copilot_with_api_key,
    login_copilot_with_oauth,
    login_nvidia_with_api_key,
)

from config import initialize_data_directory, load_config
from config.backends import (
    get_auth_label,
    get_backend_specs,
    get_container_environment_names,
    get_default_model,
    get_model_candidates,
    normalize_backend_name,
)
from config.constants import EXIT_FAIL, EXIT_OK, EXIT_WARN
from config.paths import get_data_directory
from doctor import run_doctor
from runtime import read_runtime, write_runtime
from server import build_status, run_server


LOGIN_TARGETS = ("claude", "codex", "copilot", "nvidia")


def main(argv: list[str] | None = None) -> int:
    """Parse command line arguments and dispatch to the selected subcommand.

    Args:
        argv: Command line arguments without the program name. ``None`` reads
            from ``sys.argv``.

    Returns:
        Process exit code for the selected command.
    """

    parser = argparse.ArgumentParser(prog="mcon")
    parser.add_argument("--data-dir", type=Path, default=None, help="data directory (default: $MCON_DATA_DIR or /data)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="initialize mcon data files")
    subparsers.add_parser("serve", help="run the mcon server API")
    subparsers.add_parser("stop", help="stop the host mcon server API")
    subparsers.add_parser("status", help="show runtime status")
    subparsers.add_parser("doctor", help="run diagnostics")
    tui_parser = subparsers.add_parser("tui", help="run the mcon terminal frontend through the server API")
    tui_parser.add_argument("--server", default=None, help="mcon server URL (default: runtime.json or persistent host server)")
    tui_parser.add_argument("--session", default=None, help="existing session id to open")
    tui_parser.add_argument("--backend", default=None, help="backend for a new session")
    tui_parser.add_argument("--model", default=None, help="model for a new session")
    tui_parser.add_argument("--title", default=None, help="title for a new session")
    code_parser = subparsers.add_parser("code", help="open a known mcon file in $EDITOR")
    code_parser.add_argument("key_or_path")

    login_parser = subparsers.add_parser("login", help="prepare provider credentials")
    login_parser.add_argument("target", choices=LOGIN_TARGETS)
    login_parser.add_argument("--method", choices=["api-key", "oauth"], required=True)
    login_parser.add_argument("--api-key-env", default=None, help="read API key from this environment variable")
    login_parser.add_argument("--device-auth", action="store_true", help="use device auth where supported (default for codex OAuth)")
    login_parser.add_argument("--setup-token", action="store_true", help="use Claude setup-token OAuth flow")
    login_parser.add_argument("--print-command", action="store_true", help="print OAuth command instead of running it")

    backend_parser = subparsers.add_parser("backend", help=argparse.SUPPRESS)
    backend_subparsers = backend_parser.add_subparsers(dest="backend_command", required=True)
    backend_list_parser = backend_subparsers.add_parser("list")
    backend_list_parser.add_argument("--plain", action="store_true")
    backend_normalize_parser = backend_subparsers.add_parser("normalize")
    backend_normalize_parser.add_argument("backend")
    backend_default_model_parser = backend_subparsers.add_parser("default-model")
    backend_default_model_parser.add_argument("backend")
    backend_models_parser = backend_subparsers.add_parser("model-candidates")
    backend_models_parser.add_argument("backend")
    backend_auth_label_parser = backend_subparsers.add_parser("auth-label")
    backend_auth_label_parser.add_argument("backend")
    backend_subparsers.add_parser("container-env")

    subparsers.add_parser("list", help=argparse.SUPPRESS).add_argument("topic", choices=["paths"])
    subparsers.add_parser("list-paths", help="list known mcon paths")

    args = parser.parse_args(argv)
    data_directory = args.data_dir.resolve() if args.data_dir else get_data_directory()

    if args.command == "init":
        return _cmd_init(data_directory)
    if args.command == "serve":
        return _cmd_serve(data_directory)
    if args.command == "stop":
        return _cmd_stop(data_directory)
    if args.command == "status":
        return _cmd_status(data_directory)
    if args.command == "doctor":
        return _cmd_doctor(data_directory)
    if args.command == "tui":
        return _cmd_tui(data_directory, args)
    if args.command == "code":
        return _cmd_code(data_directory, args.key_or_path)
    if args.command == "login":
        return _cmd_login(data_directory, args)
    if args.command == "backend":
        return _cmd_backend(data_directory, args)
    if args.command == "list-paths" or (args.command == "list" and args.topic == "paths"):
        return _cmd_list_paths(data_directory)
    parser.error("unknown command")
    return EXIT_FAIL


def _cmd_init(data_directory: Path) -> int:
    """Initialize the data directory and print created/known file paths.

    Args:
        data_directory: Root directory for config, runtime, vault, and audit.

    Returns:
        ``EXIT_OK`` after initialization completes.
    """

    paths = initialize_data_directory(data_directory)
    print(f"initialized: {data_directory}", file=sys.stderr)
    for path in paths:
        print(path)
    return EXIT_OK


def _cmd_serve(data_directory: Path) -> int:
    """Start the HTTP server in the foreground.

    Args:
        data_directory: Root directory used to load runtime configuration.

    Returns:
        ``EXIT_OK`` when interrupted cleanly.
    """

    initialize_data_directory(data_directory)
    runtime_config = load_config(data_directory)
    server = run_server(runtime_config)

    print(f"mcon server: http://{server.server_address[0]}:{server.server_address[1]}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        return EXIT_OK


def _cmd_stop(data_directory: Path) -> int:
    """Stop the persistent host server recorded in runtime.json.

    Args:
        data_directory: Root directory used to load runtime state.

    Returns:
        ``EXIT_OK`` when no server is running or shutdown succeeds, otherwise
        ``EXIT_FAIL``.
    """

    runtime_config = load_config(data_directory)
    runtime_data = read_runtime(runtime_config.runtime_path)
    server_pid = runtime_data.get("server_pid")
    if not isinstance(server_pid, int):
        print("mcon server is not running", file=sys.stderr)
        return EXIT_OK
    if not _pid_is_running(server_pid):
        write_runtime(
            runtime_config.runtime_path,
            {"server_pid": None, "server_url": None, "server_host": None, "server_port": None},
        )
        print("mcon server is not running", file=sys.stderr)
        return EXIT_OK

    os.kill(server_pid, signal.SIGTERM)
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _pid_is_running(server_pid):
            write_runtime(
                runtime_config.runtime_path,
                {"server_pid": None, "server_url": None, "server_host": None, "server_port": None},
            )
            print(f"stopped mcon server: pid={server_pid}", file=sys.stderr)
            return EXIT_OK
        time.sleep(0.1)
    print(f"failed to stop mcon server: pid={server_pid}", file=sys.stderr)
    return EXIT_FAIL


def _cmd_status(data_directory: Path) -> int:
    """Print JSON status for runtime services.

    Args:
        data_directory: Root directory used to load runtime configuration.

    Returns:
        ``EXIT_FAIL`` when any service check fails, otherwise ``EXIT_OK``.
    """

    runtime_config = load_config(data_directory)
    status = build_status(runtime_config)
    print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
    failed_services = [
        service_name
        for service_name, service_status in status["services"].items()
        if service_status.get("status") == "fail"
    ]
    return EXIT_FAIL if failed_services else EXIT_OK


def _cmd_doctor(data_directory: Path) -> int:
    """Run human-readable diagnostics.

    Args:
        data_directory: Root directory used to load runtime configuration.

    Returns:
        Highest diagnostic exit code: ok, warn, or fail.
    """

    runtime_config = load_config(data_directory)
    checks = run_doctor(runtime_config)
    highest_exit_code = EXIT_OK
    for check in checks:
        print(f"[{check.status:<4}] {check.name:<16} {check.message}")
        if check.status == "FAIL":
            highest_exit_code = EXIT_FAIL
        elif check.status == "WARN" and highest_exit_code == EXIT_OK:
            highest_exit_code = EXIT_WARN
    return highest_exit_code


def _cmd_tui(data_directory: Path, args: argparse.Namespace) -> int:
    """Launch the terminal frontend through the server API.

    Args:
        data_directory: Root directory used to load runtime configuration.
        args: Parsed argparse namespace for TUI options.

    Returns:
        TUI process exit code.
    """

    from tui import run_tui

    initialize_data_directory(data_directory)
    runtime_config = load_config(data_directory)
    return run_tui(
        runtime_config,
        server_url=args.server,
        session_id=args.session,
        backend=args.backend,
        model=args.model,
        title=args.title,
    )


def _cmd_code(data_directory: Path, key_or_path: str) -> int:
    """Open a known mcon file or arbitrary path in the configured editor.

    Args:
        data_directory: Root directory used to load runtime configuration.
        key_or_path: Known path key or literal path passed by the user.

    Returns:
        ``EXIT_OK`` after the editor command returns.
    """

    runtime_config = load_config(data_directory)
    paths = _known_paths(runtime_config)
    target_path = paths.get(key_or_path, Path(key_or_path))
    editor = os.environ.get("EDITOR", runtime_config.editor)
    subprocess.run([editor, str(target_path)], check=False)
    return EXIT_OK


def _cmd_login(data_directory: Path, args: argparse.Namespace) -> int:
    """Store API keys or invoke provider OAuth helpers.

    Args:
        data_directory: Root directory used to load runtime configuration.
        args: Parsed argparse namespace for login options.

    Returns:
        ``EXIT_OK`` on a supported login path, otherwise ``EXIT_FAIL``.
    """

    initialize_data_directory(data_directory)
    runtime_config = load_config(data_directory)
    if args.method == "api-key":
        api_key = _read_api_key(args.target, args.api_key_env)
        if args.target == "claude":
            result = login_claude_with_api_key(runtime_config.vault_root, api_key)
        elif args.target == "codex":
            result = login_codex_with_api_key(runtime_config.vault_root, api_key)
        elif args.target == "copilot":
            result = login_copilot_with_api_key(runtime_config.vault_root, api_key)
        else:
            result = login_nvidia_with_api_key(runtime_config.vault_root, api_key)
        print(f"stored: target={result.target} source={result.credential_source} path={result.credentials_path}")
        return EXIT_OK

    if args.target == "claude":
        result = login_claude_with_oauth(setup_token=args.setup_token, run_command=not args.print_command)
    elif args.target == "codex":
        result = login_codex_with_oauth(device_auth=True, run_command=not args.print_command)
    elif args.target == "copilot":
        result = login_copilot_with_oauth(run_command=not args.print_command)
    else:
        print("oauth: target=nvidia unsupported; use mcon login nvidia --method api-key", file=sys.stderr)
        return EXIT_FAIL
    if result.command is not None:
        print("command: " + " ".join(result.command))
    print(f"oauth: target={result.target} source={result.credential_source}")
    return EXIT_OK


def _cmd_backend(data_directory: Path, args: argparse.Namespace) -> int:
    """薄い shell wrapper 向けに backend registry 情報を出力する。

    Args:
        data_directory: credential 状態の確認に使う data directory。
        args: backend subcommand の argparse namespace。

    Returns:
        成功時は ``EXIT_OK``、失敗時は ``EXIT_FAIL``。
    """

    try:
        if args.backend_command == "list":
            backend_names = [backend_spec.name for backend_spec in get_backend_specs()]
            print(" ".join(backend_names) if args.plain else json.dumps(backend_names, ensure_ascii=False))
            return EXIT_OK
        if args.backend_command == "normalize":
            print(normalize_backend_name(args.backend))
            return EXIT_OK
        if args.backend_command == "default-model":
            print(get_default_model(args.backend))
            return EXIT_OK
        if args.backend_command == "model-candidates":
            for model_candidate in get_model_candidates(args.backend):
                print(model_candidate)
            return EXIT_OK
        if args.backend_command == "auth-label":
            print(get_auth_label(data_directory, args.backend))
            return EXIT_OK
        if args.backend_command == "container-env":
            for environment_name in get_container_environment_names():
                print(environment_name)
            return EXIT_OK
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return EXIT_FAIL
    print(f"unknown backend command: {args.backend_command}", file=sys.stderr)
    return EXIT_FAIL


def _read_api_key(target: str, api_key_env: str | None) -> str:
    """Read an API key from an environment variable or secure prompt.

    Args:
        target: Provider name used to choose the default environment variable.
        api_key_env: Optional explicit environment variable name.

    Returns:
        API key text supplied by environment or prompt.
    """

    environment_name = api_key_env
    if environment_name is None:
        if target == "claude":
            environment_name = "ANTHROPIC_API_KEY"
        elif target == "codex":
            environment_name = "OPENAI_API_KEY"
        elif target == "copilot":
            environment_name = "COPILOT_GITHUB_TOKEN"
        else:
            environment_name = "NVIDIA_API_KEY"
    api_key = os.environ.get(environment_name)
    if api_key:
        return api_key
    return getpass.getpass(f"{environment_name}: ")


def _cmd_list_paths(data_directory: Path) -> int:
    """Print known path aliases and their resolved paths.

    Args:
        data_directory: Root directory used to load runtime configuration.

    Returns:
        ``EXIT_OK`` after printing the path list.
    """

    runtime_config = load_config(data_directory)
    for key, path in sorted(_known_paths(runtime_config).items()):
        print(f"{key}\t{path}")
    return EXIT_OK


def _known_paths(runtime_config: object) -> dict[str, Path]:
    """Build the map of editable/inspectable runtime paths.

    Args:
        runtime_config: Runtime configuration object with a ``data_directory``.

    Returns:
        Mapping from short path keys to concrete filesystem paths.
    """

    data_directory = runtime_config.data_directory
    return {
        "config": data_directory / "config.toml",
        "routing": data_directory / "llm-routing.yaml",
        "runtime": data_directory / "runtime.json",
        "orchestrator": data_directory / "policy" / "orchestrator.toml",
        "roles/main": data_directory / "policy" / "roles" / "main.toml",
        "roles/coder": data_directory / "policy" / "roles" / "coder.toml",
        "vault": data_directory / "vault" / "secrets.env",
        "audit": data_directory / "audit",
    }


def _pid_is_running(pid: int) -> bool:
    """Check whether a process id exists.

    Args:
        pid: Process id to check.

    Returns:
        ``True`` when the process exists, otherwise ``False``.
    """

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if __name__ == "__main__":
    raise SystemExit(main())
