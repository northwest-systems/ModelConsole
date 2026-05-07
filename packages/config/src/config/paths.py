"""Filesystem path helpers."""

from __future__ import annotations

import os
from pathlib import Path

from .constants import DEFAULT_DATA_DIRECTORY, ENV_DATA_DIRECTORY


def get_data_directory() -> Path:
    """data directory の path を決定する。

    Args:
        なし。

    Returns:
        `MCON_DATA_DIR` があればその path、無ければ default data directory。
    """

    configured_value = os.environ.get(ENV_DATA_DIRECTORY)
    if configured_value:
        return Path(configured_value).expanduser().resolve()
    return DEFAULT_DATA_DIRECTORY


def ensure_directory(path: Path, mode: int | None = None) -> None:
    """directory を作成し、必要なら permission mode を設定する。

    Args:
        path: 作成する directory path。
        mode: `chmod` に渡す mode。`None` の場合は変更しない。

    Returns:
        なし。
    """

    path.mkdir(parents=True, exist_ok=True)
    if mode is not None:
        try:
            path.chmod(mode)
        except PermissionError:
            pass
