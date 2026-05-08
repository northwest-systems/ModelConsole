# 説明: アダプターパッケージのバックエンド自動登録とファクトリーを提供する。
# 引数: なし。
# 返り値: なし。
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any

from .anthropic import AdapterError, RawAdapterResponse

# 説明: アダプターモジュールが公開する登録情報を保持する。
# 引数: クラス属性と __init__ の引数で状態を表す。
# 返り値: AdapterSpec インスタンス。
@dataclass(frozen=True)
class AdapterSpec:
    name: str  # name: アダプターの正規バックエンド名。
    aliases: tuple[str, ...]  # aliases: 正規名以外で受け付ける別名。
    adapter_class: type[Any]  # adapter_class: build_adapter が生成するアダプタークラス。
    model_override: str | None = None  # model_override: バックエンド固有モデル上書きのキーワード名。

# 説明: アダプターパッケージ内の ADAPTER_SPEC を収集して検索表を作る。
# 引数: なし。
# 返り値: バックエンド名または別名から AdapterSpec への対応表。
def _load_adapter_specs() -> dict[str, AdapterSpec]:
    adapter_specs: dict[str, AdapterSpec] = {}
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        raw_spec = getattr(module, "ADAPTER_SPEC", None)
        if raw_spec is None:
            continue
        adapter_spec = AdapterSpec(
            name=str(raw_spec["name"]),
            aliases=tuple(str(alias) for alias in raw_spec.get("aliases", ())),
            adapter_class=raw_spec["adapter_class"],
            model_override=raw_spec.get("model_override"),
        )
        adapter_specs[adapter_spec.name] = adapter_spec
        for alias in adapter_spec.aliases:
            adapter_specs[alias] = adapter_spec
        globals()[adapter_spec.adapter_class.__name__] = adapter_spec.adapter_class
    return adapter_specs

ADAPTER_SPECS = _load_adapter_specs()

# 説明: バックエンド名または別名から対応するアダプターを作成する。
# 引数: backend は選択するバックエンド名または別名。
# 引数: credentials は vault や環境変数から取得した認証情報。
# 引数: model は全バックエンド共通のモデル上書き。
# 引数: model_overrides はバックエンド固有のモデル上書き。
# 返り値: バックエンドに対応するアダプターインスタンス。
def build_adapter(
    backend: str = "anthropic",
    *,
    credentials: dict[str, str] | None = None,
    model: str | None = None,
    **model_overrides: str | None,
) -> Any:
    normalized_backend = backend.strip().lower()
    adapter_spec = ADAPTER_SPECS.get(normalized_backend)
    if adapter_spec is None:
        raise AdapterError(503, "configuration_error", f"未対応の backend です: {backend}")
    selected_model = model
    if selected_model is None and adapter_spec.model_override is not None:
        selected_model = model_overrides.get(adapter_spec.model_override)
    if adapter_spec.model_override is None:
        return adapter_spec.adapter_class(credentials=credentials)
    return adapter_spec.adapter_class(credentials=credentials, default_model=selected_model)

# 説明: 登録済みアダプター定義を正規バックエンド名だけで返す。
# 引数: なし。
# 返り値: 正規バックエンド名から AdapterSpec への対応表。
def get_adapter_specs() -> dict[str, AdapterSpec]:
    return {adapter_spec.name: adapter_spec for adapter_spec in dict.fromkeys(ADAPTER_SPECS.values())}

__all__ = [
    "AdapterError",
    "AdapterSpec",
    "RawAdapterResponse",
    "build_adapter",
    "get_adapter_specs",
    *sorted({adapter_spec.adapter_class.__name__ for adapter_spec in ADAPTER_SPECS.values()}),
]
