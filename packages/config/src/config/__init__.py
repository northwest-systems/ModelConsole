"""Configuration loading and initialization."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    AUDIT_DIRECTORY_NAME,
    CONFIG_FILE_NAME,
    DEFAULT_SERVER_HOST,
    POLICY_DIRECTORY_NAME,
    ROLE_DIRECTORY_NAME,
    ROUTING_FILE_NAME,
    RUNTIME_FILE_NAME,
    VAULT_DIRECTORY_NAME,
)
from .paths import ensure_directory


@dataclass(frozen=True)
class ServiceConfig:
    host: str
    port: int


@dataclass(frozen=True)
class RuntimeConfig:
    data_directory: Path
    server: ServiceConfig
    backend: str
    audit_root: Path
    vault_root: Path
    runtime_path: Path
    routing_path: Path
    editor: str


def load_config(data_directory: Path) -> RuntimeConfig:
    """config.toml を読み込み runtime 用設定に変換する。

    Args:
        data_directory: `config.toml` や runtime data を置く root directory。

    Returns:
        読み込んだ値と default 値を統合した `RuntimeConfig`。
    """

    config_path = data_directory / CONFIG_FILE_NAME
    config_data: dict = {}
    if config_path.exists():
        with config_path.open("rb") as config_file:
            config_data = tomllib.load(config_file)

    core_config = config_data.get("core", {})
    server_config = config_data.get("server", {})
    backend_config = config_data.get("backend", config_data.get("proxy", {}))

    audit_root = _resolve_data_path(core_config.get("audit_root"), data_directory, AUDIT_DIRECTORY_NAME)
    vault_root = data_directory / VAULT_DIRECTORY_NAME

    return RuntimeConfig(
        data_directory=data_directory,
        server=ServiceConfig(
            host=str(server_config.get("host", DEFAULT_SERVER_HOST)),
            port=int(server_config.get("port", 0)),
        ),
        backend=str(backend_config.get("default", backend_config.get("backend", "anthropic"))).strip().lower(),
        audit_root=audit_root,
        vault_root=vault_root,
        runtime_path=data_directory / RUNTIME_FILE_NAME,
        routing_path=data_directory / ROUTING_FILE_NAME,
        editor=str(core_config.get("editor", "vi")),
    )


def initialize_data_directory(data_directory: Path) -> list[Path]:
    """data directory の初期構造と default file を作る。

    Args:
        data_directory: 初期化する data root directory。

    Returns:
        作成済み、または既に存在していた path の一覧。
    """

    created_or_existing_paths: list[Path] = []
    ensure_directory(data_directory)
    created_or_existing_paths.append(data_directory)

    audit_root = data_directory / AUDIT_DIRECTORY_NAME
    vault_root = data_directory / VAULT_DIRECTORY_NAME
    sessions_root = data_directory / "sessions"
    policy_root = data_directory / POLICY_DIRECTORY_NAME
    role_root = policy_root / ROLE_DIRECTORY_NAME

    for directory_path, mode in (
        (audit_root, None),
        (vault_root, 0o700),
        (sessions_root, None),
        (policy_root, None),
        (role_root, None),
    ):
        ensure_directory(directory_path, mode)
        created_or_existing_paths.append(directory_path)

    default_files = {
        data_directory / CONFIG_FILE_NAME: _default_config_text(data_directory),
        data_directory / ROUTING_FILE_NAME: _default_routing_text(),
        policy_root / "orchestrator.toml": _default_orchestrator_policy_text(),
        role_root / "main.toml": _default_role_policy_text("main"),
        role_root / "coder.toml": _default_role_policy_text("coder"),
    }

    for file_path, file_content in default_files.items():
        if not file_path.exists():
            file_path.write_text(file_content, encoding="utf-8")
        created_or_existing_paths.append(file_path)

    vault_file = vault_root / "secrets.env"
    try:
        vault_file_exists = vault_file.exists()
    except PermissionError:
        created_or_existing_paths.append(vault_file)
        return created_or_existing_paths
    if not vault_file_exists:
        vault_file.write_text("# Phase 1 local secret store. Prefer Docker env for API keys.\n", encoding="utf-8")
        try:
            vault_file.chmod(0o600)
        except PermissionError:
            pass
    created_or_existing_paths.append(vault_file)

    return created_or_existing_paths


def _default_config_text(data_directory: Path) -> str:
    """default の config.toml 内容を生成する。

    Args:
        data_directory: path 展開に使う data root directory。

    Returns:
        config.toml に書き込む TOML text。
    """

    workspace_root = data_directory / "projects"
    audit_root = data_directory / AUDIT_DIRECTORY_NAME
    return f"""[core]
workspace_root = "{workspace_root}"
audit_root = "{audit_root}"
editor = "vi"

[server]
host = "127.0.0.1"
port = 8765

[backend]
default = "anthropic"
claude_code_model = "claude-sonnet-4-6"
codex_model = "gpt-5.3-codex"
copilot_model = "gpt-5.3-codex"
nvidia_model = "nvidia/llama-3.3-nemotron-super-49b-v1.5"
"""


def _resolve_data_path(configured_value: object, data_directory: Path, default_child: str) -> Path:
    """config の path 値を host 側 data directory に解決する。

    Args:
        configured_value: config に書かれた path 値。
        data_directory: host 側 data root directory。
        default_child: 値が無い場合に data root 配下で使う child name。

    Returns:
        host filesystem 上で使う `Path`。
    """

    if configured_value is None:
        return data_directory / default_child
    configured_path = Path(str(configured_value)).expanduser()
    if configured_path == Path("/data"):
        return data_directory
    try:
        relative_to_container_data = configured_path.relative_to("/data")
    except ValueError:
        return configured_path
    return data_directory / relative_to_container_data


def _default_routing_text() -> str:
    """default routing file の内容を生成する。

    Args:
        なし。

    Returns:
        llm-routing.yaml に書き込む YAML text。
    """

    return """routes:
  - match: { model: "claude-*" }
    backend: anthropic
  - match: { model: "*" }
    backend: anthropic
limits:
  warn_at: 80
  fallback_at: 95
"""


def _default_orchestrator_policy_text() -> str:
    """default orchestrator policy file の内容を生成する。

    Args:
        なし。

    Returns:
        orchestrator.toml に書き込む TOML text。
    """

    return """[phase1]
description = "Server-centered TUI runtime. Sandbox enforcement is implemented in later phases."
"""


def _default_role_policy_text(role_name: str) -> str:
    """default role policy file の内容を生成する。

    Args:
        role_name: role 名。description に埋め込む。

    Returns:
        role policy TOML text。
    """

    return f"""description = "Phase 1 {role_name} role placeholder"

[llm]
default_model = "claude-3-5-sonnet-latest"
allowed_backends = ["anthropic"]
"""
