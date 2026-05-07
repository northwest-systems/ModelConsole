# 説明: ホストラッパーと Python runtime で共有する backend registry。

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
# 説明: ユーザーが選択できる backend の定義。
# 引数: dataclass の各 field に backend metadata を渡す。
# 返り値: BackendSpec instance。
class BackendSpec:
    name: str
    aliases: tuple[str, ...]
    default_model: str
    model_candidates: tuple[str, ...]
    model_environment_name: str
    login_target: str
    login_method: str
    credential_environment_names: tuple[str, ...]
    extra_environment_names: tuple[str, ...] = ()
    oauth_file_paths: tuple[Path, ...] = ()


BACKEND_SPECS: tuple[BackendSpec, ...] = (
    BackendSpec(
        name="claude-code",
        aliases=("claude",),
        default_model="claude-sonnet-4-6",
        model_candidates=("claude-sonnet-4-6", "claude-3-5-sonnet-latest"),
        model_environment_name="MCON_CLAUDE_CODE_MODEL",
        login_target="claude",
        login_method="oauth",
        credential_environment_names=("ANTHROPIC_API_KEY",),
        oauth_file_paths=(Path("home/.claude.json"),),
    ),
    BackendSpec(
        name="anthropic",
        aliases=(),
        default_model="claude-3-5-sonnet-latest",
        model_candidates=("claude-3-5-sonnet-latest", "claude-sonnet-4-6"),
        model_environment_name="MCON_ANTHROPIC_MODEL",
        login_target="claude",
        login_method="api-key",
        credential_environment_names=("ANTHROPIC_API_KEY",),
    ),
    BackendSpec(
        name="codex",
        aliases=(),
        default_model="gpt-5.3-codex",
        model_candidates=("gpt-5.3-codex", "gpt-5.2"),
        model_environment_name="MCON_CODEX_MODEL",
        login_target="codex",
        login_method="oauth",
        credential_environment_names=("OPENAI_API_KEY",),
        oauth_file_paths=(Path("home/.codex/auth.json"),),
    ),
    BackendSpec(
        name="copilot",
        aliases=(),
        default_model="gpt-5.3-codex",
        model_candidates=("gpt-5.3-codex", "gpt-5.2"),
        model_environment_name="MCON_COPILOT_MODEL",
        login_target="copilot",
        login_method="oauth",
        credential_environment_names=("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
    ),
    BackendSpec(
        name="nvidia",
        aliases=("nim", "nvidia-nim"),
        default_model="nvidia/llama-3.3-nemotron-super-49b-v1.5",
        model_candidates=(
            "nvidia/llama-3.3-nemotron-super-49b-v1.5",
            "nvidia/llama-3.1-nemotron-ultra-253b-v1",
            "meta/llama-3.3-70b-instruct",
            "deepseek-ai/deepseek-v3.2",
            "minimaxai/minimax-m2.7",
            "qwen/qwen3-coder-480b-a35b-instruct",
        ),
        model_environment_name="MCON_NVIDIA_MODEL",
        login_target="nvidia",
        login_method="api-key",
        credential_environment_names=("NVIDIA_API_KEY", "NGC_API_KEY"),
        extra_environment_names=("MCON_NVIDIA_BASE_URL",),
    ),
)


# 説明: 有効な backend 定義を返す。
# 引数: なし。
# 返り値: 表示順の backend 定義。MCON_BACKENDS があれば表示対象だけを絞る。
def get_backend_specs() -> tuple[BackendSpec, ...]:
    configured_backends = os.environ.get("MCON_BACKENDS")
    if not configured_backends:
        return BACKEND_SPECS
    enabled_specs: list[BackendSpec] = []
    for backend_name in configured_backends.split():
        enabled_specs.append(get_backend_spec(backend_name))
    return tuple(enabled_specs)


# 説明: backend 名または alias から backend 定義を探す。
# 引数: backend_name はユーザーが指定した backend 名。
# 返り値: 一致した backend 定義。未知の backend なら ValueError を送出する。
def get_backend_spec(backend_name: str) -> BackendSpec:
    normalized_backend_name = backend_name.strip().lower()
    for backend_spec in BACKEND_SPECS:
        if normalized_backend_name == backend_spec.name or normalized_backend_name in backend_spec.aliases:
            return backend_spec
    raise ValueError(f"未知の backend です: {backend_name}")


# 説明: backend 名または alias を正規名へ変換する。
# 引数: backend_name はユーザーが指定した backend 名。
# 返り値: backend の正規名。
def normalize_backend_name(backend_name: str) -> str:
    return get_backend_spec(backend_name).name


# 説明: backend の標準 model を返す。
# 引数: backend_name は対象 backend 名。
# 返り値: 環境変数による上書きを反映した標準 model。
def get_default_model(backend_name: str) -> str:
    backend_spec = get_backend_spec(backend_name)
    environment_value = os.environ.get("MCON_BACKEND_MODEL") or os.environ.get(backend_spec.model_environment_name)
    return environment_value or backend_spec.default_model


# 説明: backend の model 候補を標準 model 優先で返す。
# 引数: backend_name は対象 backend 名。
# 返り値: 重複を除いた model 候補一覧。
def get_model_candidates(backend_name: str) -> tuple[str, ...]:
    default_model = get_default_model(backend_name)
    candidates = [default_model, *get_backend_spec(backend_name).model_candidates]
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


# 説明: backend が利用可能な認証情報を持っていそうか確認する。
# 引数: data_directory は mcon データディレクトリ、backend_name は対象 backend 名。
# 返り値: 認証情報が見つかれば True、それ以外は False。
def is_backend_authenticated(data_directory: Path, backend_name: str) -> bool:
    backend_spec = get_backend_spec(backend_name)
    if any(os.environ.get(environment_name) for environment_name in backend_spec.credential_environment_names):
        return True
    credentials_path = data_directory / "vault" / "cli-auth.env"
    if credentials_path.exists():
        credential_text = credentials_path.read_text(encoding="utf-8")
        for environment_name in backend_spec.credential_environment_names:
            if f"{environment_name}=" in credential_text:
                return True
    return any((data_directory / oauth_file_path).exists() for oauth_file_path in backend_spec.oauth_file_paths)


# 説明: boot 選択用の短い認証状態ラベルを返す。
# 引数: data_directory は mcon データディレクトリ、backend_name は対象 backend 名。
# 返り値: 認証済みまたは login 案内のラベル。
def get_auth_label(data_directory: Path, backend_name: str) -> str:
    backend_spec = get_backend_spec(backend_name)
    if is_backend_authenticated(data_directory, backend_name):
        return "[認証済み]"
    return f"[未認証] login: mcon login {backend_spec.login_target} --method {backend_spec.login_method}"


# 説明: コンテナに引き継ぐ環境変数名を registry から組み立てる。
# 引数: なし。
# 返り値: コンテナへ渡す環境変数名一覧。
def get_container_environment_names() -> tuple[str, ...]:
    environment_names = ["MCON_BACKENDS", "MCON_BACKEND_MODEL"]
    for backend_spec in BACKEND_SPECS:
        environment_names.extend(backend_spec.credential_environment_names)
        environment_names.append(backend_spec.model_environment_name)
        environment_names.extend(backend_spec.extra_environment_names)
    return tuple(dict.fromkeys(environment_names))
