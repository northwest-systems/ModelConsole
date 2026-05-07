"""Shared constants for the mcon phase 1 runtime."""

from __future__ import annotations

from pathlib import Path

# 標準のデータ置き場。Docker 実行時は /data volume、ホスト実行時は MCON_DATA_DIR で上書きする。
DEFAULT_DATA_DIRECTORY = Path("/data")

# データ置き場を指定する環境変数名。config, runtime, vault, audit をまとめて移動する。
ENV_DATA_DIRECTORY = "MCON_DATA_DIR"

# Anthropic/Claude 系の認証情報と upstream URL を指定する環境変数名。
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
ENV_ANTHROPIC_BASE_URL = "MCON_ANTHROPIC_BASE_URL"

# data directory 直下に作る設定ファイル名。
CONFIG_FILE_NAME = "config.toml"
RUNTIME_FILE_NAME = "runtime.json"
ROUTING_FILE_NAME = "llm-routing.yaml"

# data directory 直下に作る永続ディレクトリ名。
AUDIT_DIRECTORY_NAME = "audit"
VAULT_DIRECTORY_NAME = "vault"
POLICY_DIRECTORY_NAME = "policy"
ROLE_DIRECTORY_NAME = "roles"

# doctor/status/audit で使うサービス識別子。
SERVER_SERVICE_NAME = "server"
AUDIT_SERVICE_NAME = "audit"
VAULT_SERVICE_NAME = "vault"

# 埋め込み server の標準 bind host。port は config.toml 側で管理する。
DEFAULT_SERVER_HOST = "127.0.0.1"

# CLI の終了コード。shell script から扱いやすいように成功/警告/失敗へ絞る。
EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2
