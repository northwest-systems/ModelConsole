"""Runtime state helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from audit import utc_now_iso


def read_runtime(runtime_path: Path) -> dict[str, Any]:
    """runtime.json を読み込む。

    Args:
        runtime_path: 読み込む runtime.json の path。

    Returns:
        JSON object。file が無い、または壊れている場合は空 dict。
    """

    if not runtime_path.exists():
        return {}
    try:
        return json.loads(runtime_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_runtime(runtime_path: Path, values: dict[str, Any]) -> None:
    """runtime.json に値を merge して保存する。

    Args:
        runtime_path: 書き込む runtime.json の path。
        values: 既存 runtime data に merge する値。

    Returns:
        なし。
    """

    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_data = read_runtime(runtime_path)
    runtime_data.update(values)
    runtime_data["updated_at"] = utc_now_iso()
    if "started_at" not in runtime_data:
        runtime_data["started_at"] = runtime_data["updated_at"]
    runtime_path.write_text(
        json.dumps(runtime_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
