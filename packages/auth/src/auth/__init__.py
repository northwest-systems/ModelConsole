"""Login helpers for CLI-backed providers."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from config.constants import ENV_ANTHROPIC_API_KEY

AuthTarget = Literal["claude", "codex", "copilot", "nvidia"]
AuthMethod = Literal["api_key", "oauth"]

# Codex/OpenAI API key を保存・注入する環境変数名。
ENV_OPENAI_API_KEY = "OPENAI_API_KEY"

# GitHub Copilot CLI 用 token を保存・注入する環境変数名。
ENV_COPILOT_GITHUB_TOKEN = "COPILOT_GITHUB_TOKEN"

# NVIDIA NIM API key を保存・注入する環境変数名。
ENV_NVIDIA_API_KEY = "NVIDIA_API_KEY"

# Claude Code OAuth setup-token flow で参照する token 名。
ENV_CLAUDE_CODE_OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"

# vault directory 内に置く CLI/API credential file 名。
CLI_AUTH_FILE_NAME = "cli-auth.env"


@dataclass(frozen=True)
class LoginResult:
    """Result of preparing or running a provider login."""

    target: AuthTarget
    method: AuthMethod
    credential_source: str
    environment: dict[str, str]
    command: tuple[str, ...] | None = None
    credentials_path: Path | None = None


def login_claude_with_api_key(vault_root: Path, api_key: str) -> LoginResult:
    """Anthropic API key を local vault に保存する。

    Args:
        vault_root: credential file を置く vault directory。
        api_key: 保存する Anthropic API key。

    Returns:
        保存結果を表す `LoginResult`。
    """

    credentials_path = _write_env_credential(vault_root, ENV_ANTHROPIC_API_KEY, api_key)
    return LoginResult(
        target="claude",
        method="api_key",
        credential_source=ENV_ANTHROPIC_API_KEY,
        environment={ENV_ANTHROPIC_API_KEY: api_key},
        credentials_path=credentials_path,
    )


def login_claude_with_oauth(
    *,
    executable: str = "claude",
    setup_token: bool = False,
    run_command: bool = True,
) -> LoginResult:
    """Claude CLI の OAuth login command を実行または準備する。

    Args:
        executable: 実行する Claude CLI command 名。
        setup_token: `claude setup-token` flow を使う場合は `True`。
        run_command: 実際に command を実行するなら `True`。

    Returns:
        OAuth flow の command と credential source を含む `LoginResult`。
    """

    command = (executable, "setup-token") if setup_token else (executable, "auth", "login")
    if run_command:
        _run_interactive_command(command)
    return LoginResult(
        target="claude",
        method="oauth",
        credential_source=ENV_CLAUDE_CODE_OAUTH_TOKEN if setup_token else "claude credential store",
        environment={},
        command=command,
    )


def login_codex_with_api_key(vault_root: Path, api_key: str) -> LoginResult:
    """OpenAI API key を Codex backend 用に local vault へ保存する。

    Args:
        vault_root: credential file を置く vault directory。
        api_key: 保存する OpenAI API key。

    Returns:
        保存結果を表す `LoginResult`。
    """

    credentials_path = _write_env_credential(vault_root, ENV_OPENAI_API_KEY, api_key)
    return LoginResult(
        target="codex",
        method="api_key",
        credential_source=ENV_OPENAI_API_KEY,
        environment={ENV_OPENAI_API_KEY: api_key},
        credentials_path=credentials_path,
    )


def login_codex_with_oauth(
    *,
    executable: str = "codex",
    device_auth: bool = True,
    run_command: bool = True,
) -> LoginResult:
    """Codex CLI の OAuth login command を実行または準備する。

    Args:
        executable: 実行する Codex CLI command 名。
        device_auth: device auth option を付けるなら `True`。
        run_command: 実際に command を実行するなら `True`。

    Returns:
        OAuth flow の command と credential source を含む `LoginResult`。
    """

    command = (executable, "login", "--device-auth") if device_auth else (executable, "login")
    if run_command:
        _run_interactive_command(command)
    return LoginResult(
        target="codex",
        method="oauth",
        credential_source="codex credential store",
        environment={},
        command=command,
    )


def login_copilot_with_api_key(vault_root: Path, api_key: str) -> LoginResult:
    """GitHub token を Copilot backend 用に local vault へ保存する。

    Args:
        vault_root: credential file を置く vault directory。
        api_key: 保存する GitHub token。

    Returns:
        保存結果を表す `LoginResult`。
    """

    credentials_path = _write_env_credential(vault_root, ENV_COPILOT_GITHUB_TOKEN, api_key)
    return LoginResult(
        target="copilot",
        method="api_key",
        credential_source=ENV_COPILOT_GITHUB_TOKEN,
        environment={ENV_COPILOT_GITHUB_TOKEN: api_key},
        credentials_path=credentials_path,
    )


def login_copilot_with_oauth(
    *,
    executable: str = "copilot",
    run_command: bool = True,
) -> LoginResult:
    """Copilot CLI の OAuth login command を実行または準備する。

    Args:
        executable: 実行する Copilot CLI command 名。
        run_command: 実際に command を実行するなら `True`。

    Returns:
        OAuth flow の command と credential source を含む `LoginResult`。
    """

    command = (executable, "login")
    if run_command:
        _run_interactive_command(command)
    return LoginResult(
        target="copilot",
        method="oauth",
        credential_source="copilot credential store",
        environment={},
        command=command,
    )


def login_nvidia_with_api_key(vault_root: Path, api_key: str) -> LoginResult:
    """NVIDIA API key を local vault へ保存する。

    Args:
        vault_root: credential file を置く vault directory。
        api_key: 保存する NVIDIA API key。

    Returns:
        保存結果を表す `LoginResult`。
    """

    credentials_path = _write_env_credential(vault_root, ENV_NVIDIA_API_KEY, api_key)
    return LoginResult(
        target="nvidia",
        method="api_key",
        credential_source=ENV_NVIDIA_API_KEY,
        environment={ENV_NVIDIA_API_KEY: api_key},
        credentials_path=credentials_path,
    )


def build_login_environment(result: LoginResult, base_environment: dict[str, str] | None = None) -> dict[str, str]:
    """login 結果の environment 値を base environment に反映する。

    Args:
        result: 反映する login result。
        base_environment: base にする environment。`None` なら `os.environ` を使う。

    Returns:
        login credential を含む environment dict。
    """

    environment = dict(os.environ if base_environment is None else base_environment)
    environment.update(result.environment)
    return environment


def load_cli_auth_environment(vault_root: Path, target: AuthTarget) -> dict[str, str]:
    """target provider 用の credential environment を vault から読み込む。

    Args:
        vault_root: credential file を含む vault directory。
        target: credential を読み込む provider target。

    Returns:
        provider CLI/API に渡す environment 変数 dict。
    """

    credentials_path = vault_root / CLI_AUTH_FILE_NAME
    stored_values = _read_env_credentials(credentials_path)
    if target == "claude":
        api_key = stored_values.get(ENV_ANTHROPIC_API_KEY)
        return {ENV_ANTHROPIC_API_KEY: api_key} if api_key else {}
    if target == "codex":
        api_key = stored_values.get(ENV_OPENAI_API_KEY)
        return {ENV_OPENAI_API_KEY: api_key} if api_key else {}
    if target == "copilot":
        api_key = stored_values.get(ENV_COPILOT_GITHUB_TOKEN)
        return {ENV_COPILOT_GITHUB_TOKEN: api_key} if api_key else {}
    api_key = stored_values.get(ENV_NVIDIA_API_KEY)
    return {ENV_NVIDIA_API_KEY: api_key} if api_key else {}


def _write_env_credential(vault_root: Path, variable_name: str, secret_value: str) -> Path:
    """credential env file に secret を保存する。

    Args:
        vault_root: credential file を置く vault directory。
        variable_name: 保存する environment variable 名。
        secret_value: 保存する secret 値。

    Returns:
        保存した credential file の path。
    """

    if not secret_value:
        raise ValueError(f"{variable_name} must not be empty")
    vault_root.mkdir(parents=True, exist_ok=True)
    vault_root.chmod(0o700)
    credentials_path = vault_root / CLI_AUTH_FILE_NAME
    existing_values = _read_env_credentials(credentials_path)
    existing_values[variable_name] = secret_value
    serialized_lines = [
        "# mcon CLI provider credentials. This file must remain local and private.",
        *[f"{key}={shlex.quote(value)}" for key, value in sorted(existing_values.items())],
    ]
    credentials_path.write_text("\n".join(serialized_lines) + "\n", encoding="utf-8")
    credentials_path.chmod(0o600)
    return credentials_path


def _read_env_credentials(credentials_path: Path) -> dict[str, str]:
    """credential env file を読み込む。

    Args:
        credentials_path: 読み込む env file path。

    Returns:
        env file に含まれる key/value dict。file が無ければ空 dict。
    """

    if not credentials_path.exists():
        return {}
    values: dict[str, str] = {}
    for line in credentials_path.read_text(encoding="utf-8").splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue
        key, raw_value = stripped_line.split("=", 1)
        values[key] = shlex.split(raw_value)[0] if raw_value else ""
    return values


def _run_interactive_command(command: tuple[str, ...]) -> None:
    """対話的な login command を実行する。

    Args:
        command: 実行する command と argv。

    Returns:
        なし。command が失敗した場合は `RuntimeError` を送出する。
    """

    completed_process = subprocess.run(command, check=False)
    if completed_process.returncode != 0:
        command_label = " ".join(command)
        raise RuntimeError(f"{command_label} failed with exit code {completed_process.returncode}")
