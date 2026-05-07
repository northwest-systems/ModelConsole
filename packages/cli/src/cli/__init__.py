# 説明: mcon コマンドライン処理。

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


# 説明: コマンドライン引数を解釈し、選択されたサブコマンドへ処理を振り分ける。
# 引数: argv は program 名を含まないコマンドライン引数。None の場合は sys.argv から読む。
# 返り値: 選択された command の process exit code。
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcon")
    parser.add_argument("--data-dir", type=Path, default=None, help="データディレクトリ (標準: $MCON_DATA_DIR または /data)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="mcon data file を初期化する")
    subparsers.add_parser("serve", help="mcon server API を起動する")
    subparsers.add_parser("stop", help="ホスト側の mcon server API を停止する")
    subparsers.add_parser("status", help="実行時状態を表示する")
    subparsers.add_parser("doctor", help="診断を実行する")
    tui_parser = subparsers.add_parser("tui", help="server API 経由で mcon ターミナル frontend を起動する")
    tui_parser.add_argument("--server", default=None, help="mcon server URL (標準: runtime.json または常駐ホスト server)")
    tui_parser.add_argument("--session", default=None, help="開く既存 session id")
    tui_parser.add_argument("--backend", default=None, help="新規 session 用バックエンド")
    tui_parser.add_argument("--model", default=None, help="新規 session 用 model")
    tui_parser.add_argument("--title", default=None, help="新規 session 用 title")
    code_parser = subparsers.add_parser("code", help="既知の mcon file を $EDITOR で開く")
    code_parser.add_argument("key_or_path")

    login_parser = subparsers.add_parser("login", help="provider 認証情報を準備する")
    login_parser.add_argument("target", choices=LOGIN_TARGETS)
    login_parser.add_argument("--method", choices=["api-key", "oauth"], required=True)
    login_parser.add_argument("--api-key-env", default=None, help="この環境変数から API key を読む")
    login_parser.add_argument("--device-auth", action="store_true", help="対応 provider では device auth を使う")
    login_parser.add_argument("--setup-token", action="store_true", help="Claude setup-token OAuth flow を使う")
    login_parser.add_argument("--print-command", action="store_true", help="OAuth command を実行せず表示だけ行う")

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
    subparsers.add_parser("list-paths", help="既知の mcon path を一覧する")

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


# 説明: データディレクトリを初期化し、作成済みまたは既知の file path を表示する。
# 引数: data_directory は config、runtime、vault、audit の root directory。
# 返り値: 初期化完了後の EXIT_OK。
def _cmd_init(data_directory: Path) -> int:
    paths = initialize_data_directory(data_directory)
    print(f"initialized: {data_directory}", file=sys.stderr)
    for path in paths:
        print(path)
    return EXIT_OK


# 説明: HTTP server を前面で起動する。
# 引数: data_directory は runtime 設定の読み込みに使う root directory。
# 返り値: clean に中断された場合は EXIT_OK。
def _cmd_serve(data_directory: Path) -> int:
    initialize_data_directory(data_directory)
    runtime_config = load_config(data_directory)
    server = run_server(runtime_config)

    print(f"mcon server: http://{server.server_address[0]}:{server.server_address[1]}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        return EXIT_OK


# 説明: runtime.json に記録された常駐ホスト server を停止する。
# 引数: data_directory は runtime state の読み込みに使う root directory。
# 返り値: server が動いていない、または停止に成功した場合は EXIT_OK。停止できなかった場合は EXIT_FAIL。
def _cmd_stop(data_directory: Path) -> int:
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


# 説明: 実行時 service の状態を JSON で表示する。
# 引数: data_directory は runtime 設定の読み込みに使う root directory。
# 返り値: service check が 1 つでも失敗した場合は EXIT_FAIL。それ以外は EXIT_OK。
def _cmd_status(data_directory: Path) -> int:
    runtime_config = load_config(data_directory)
    status = build_status(runtime_config)
    print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
    failed_services = [
        service_name
        for service_name, service_status in status["services"].items()
        if service_status.get("status") == "fail"
    ]
    return EXIT_FAIL if failed_services else EXIT_OK


# 説明: 人間が読める形式の診断を実行する。
# 引数: data_directory は runtime 設定の読み込みに使う root directory。
# 返り値: 診断結果のうち最も重い exit code。ok、warn、fail のいずれか。
def _cmd_doctor(data_directory: Path) -> int:
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


# 説明: server API 経由でターミナル frontend を起動する。
# 引数: data_directory は runtime 設定の読み込みに使う root directory。args は TUI option 用に parse 済みの argparse namespace。
# 返り値: TUI process の exit code。
def _cmd_tui(data_directory: Path, args: argparse.Namespace) -> int:
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


# 説明: 既知の mcon file または任意 path を設定済み editor で開く。
# 引数: data_directory は runtime 設定の読み込みに使う root directory。key_or_path は既知 path key または literal path。
# 返り値: editor command の終了後に EXIT_OK。
def _cmd_code(data_directory: Path, key_or_path: str) -> int:
    runtime_config = load_config(data_directory)
    paths = _known_paths(runtime_config)
    target_path = paths.get(key_or_path, Path(key_or_path))
    editor = os.environ.get("EDITOR", runtime_config.editor)
    subprocess.run([editor, str(target_path)], check=False)
    return EXIT_OK


# 説明: API key を保存するか、provider OAuth helper を呼び出す。
# 引数: data_directory は runtime 設定の読み込みに使う root directory。args は login option 用に parse 済みの argparse namespace。
# 返り値: 対応済み login path では EXIT_OK。それ以外は EXIT_FAIL。
def _cmd_login(data_directory: Path, args: argparse.Namespace) -> int:
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
        print("oauth: target=nvidia は未対応です。mcon login nvidia --method api-key を使ってください", file=sys.stderr)
        return EXIT_FAIL
    if result.command is not None:
        print("command: " + " ".join(result.command))
    print(f"oauth: target={result.target} source={result.credential_source}")
    return EXIT_OK


# 説明: 薄い shell ラッパー向けに backend registry 情報を出力する。
# 引数: data_directory は credential 状態の確認に使う data directory。args は backend subcommand の argparse namespace。
# 返り値: 成功時は EXIT_OK、失敗時は EXIT_FAIL。
def _cmd_backend(data_directory: Path, args: argparse.Namespace) -> int:
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
    print(f"未知の backend command です: {args.backend_command}", file=sys.stderr)
    return EXIT_FAIL


# 説明: 環境変数または secure prompt から API key を読む。
# 引数: target は標準環境変数を選ぶための provider 名。api_key_env は明示的に指定された任意の環境変数名。
# 返り値: 環境変数または prompt から受け取った API key 文字列。
def _read_api_key(target: str, api_key_env: str | None) -> str:
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


# 説明: 既知の path alias と解決後 path を表示する。
# 引数: data_directory は runtime 設定の読み込みに使う root directory。
# 返り値: path 一覧の表示後に EXIT_OK。
def _cmd_list_paths(data_directory: Path) -> int:
    runtime_config = load_config(data_directory)
    for key, path in sorted(_known_paths(runtime_config).items()):
        print(f"{key}\t{path}")
    return EXIT_OK


# 説明: 編集または確認可能な runtime path の map を組み立てる。
# 引数: runtime_config は data_directory を持つ runtime 設定 object。
# 返り値: 短い path key から具体的な filesystem path への mapping。
def _known_paths(runtime_config: object) -> dict[str, Path]:
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


# 説明: process id が存在するか確認する。
# 引数: pid は確認対象の process id。
# 返り値: process が存在する場合は True。それ以外は False。
def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if __name__ == "__main__":
    raise SystemExit(main())
