# 説明: mcon 実行基盤が使うバックエンドアダプターの自動登録 factory。

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any

from .anthropic import AdapterError, RawAdapterResponse


@dataclass(frozen=True)
# 説明: アダプターモジュールが公開するバックエンド登録情報。
# 引数: name は正規バックエンド名、aliases は別名、adapter_class は生成する class、model_override はバックエンド固有モデル引数名。
# 返り値: AdapterSpec instance。
class AdapterSpec:
    name: str
    aliases: tuple[str, ...]
    adapter_class: type[Any]
    model_override: str | None = None


# 説明: アダプターパッケージ内のモジュールから ADAPTER_SPEC を収集する。
# 引数: なし。
# 返り値: バックエンド名と別名から AdapterSpec への対応表。
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


# 説明: 選択されたバックエンドのアダプター instance を作成する。
# 引数: backend はバックエンド名または別名。credentials は vault 由来の認証情報。model は共通モデル上書き。model_overrides はバックエンド固有モデル上書き。
# 返り値: バックエンド request method を実装するアダプター instance。未対応バックエンドなら AdapterError を送出する。
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


# 説明: 登録済みアダプター spec を正規バックエンド名だけで返す。
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
