"""Phase 1 doctor checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import RuntimeConfig
from runtime import read_runtime


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str


def run_doctor(runtime_config: RuntimeConfig) -> list[DoctorCheck]:
    """runtime に必要な path と state を診断する。

    Args:
        runtime_config: 診断対象の runtime 設定。

    Returns:
        各診断項目の `DoctorCheck` 一覧。
    """

    checks = [
        _check_path("data", runtime_config.data_directory, must_be_directory=True),
        _check_path("config", runtime_config.data_directory / "config.toml", must_be_directory=False),
        _check_path("routing", runtime_config.routing_path, must_be_directory=False),
        _check_path("audit", runtime_config.audit_root, must_be_directory=True),
        _check_path("vault", runtime_config.vault_root, must_be_directory=True),
        _check_runtime(runtime_config.runtime_path),
    ]
    return checks


def _check_path(name: str, path: Path, must_be_directory: bool) -> DoctorCheck:
    """path の存在と file/directory 種別を診断する。

    Args:
        name: 診断項目名。
        path: 診断する path。
        must_be_directory: directory であるべきなら `True`、file であるべきなら `False`。

    Returns:
        診断結果。
    """

    if not path.exists():
        return DoctorCheck(name, "FAIL", f"{path} does not exist")
    if must_be_directory and not path.is_dir():
        return DoctorCheck(name, "FAIL", f"{path} is not a directory")
    if not must_be_directory and not path.is_file():
        return DoctorCheck(name, "FAIL", f"{path} is not a file")
    return DoctorCheck(name, "OK", str(path))


def _check_runtime(runtime_path: Path) -> DoctorCheck:
    """runtime.json が読める状態か診断する。

    Args:
        runtime_path: 診断対象の runtime.json path。

    Returns:
        runtime state の診断結果。
    """

    runtime_data = read_runtime(runtime_path)
    if not runtime_data:
        return DoctorCheck("runtime", "WARN", f"{runtime_path} is missing or empty; run mcon serve")
    return DoctorCheck("runtime", "OK", f"{runtime_path}")
