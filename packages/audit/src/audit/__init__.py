"""JSON Lines audit logging."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """現在時刻を UTC の ISO-8601 文字列で返す。

    Args:
        なし。

    Returns:
        `Z` suffix 付きの UTC timestamp。
    """

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class AuditLogger:
    """Append-only JSON Lines audit logger."""

    def __init__(self, audit_root: Path) -> None:
        """audit log の保存先を保持する。

        Args:
            audit_root: 日付別 JSONL file を保存する root directory。

        Returns:
            なし。
        """

        self.audit_root = audit_root

    def write_event(self, kind: str, payload: dict[str, Any]) -> Path:
        """audit event を JSON Lines として追記する。

        Args:
            kind: event 種別。
            payload: event payload。secret らしい key は masking される。

        Returns:
            追記した JSONL file の path。
        """

        timestamp = utc_now_iso()
        event = {"ts": timestamp, "kind": kind, **_mask_secrets(payload)}
        now = datetime.now(UTC)
        log_directory = self.audit_root / now.strftime("%Y-%m-%d")
        log_directory.mkdir(parents=True, exist_ok=True)
        log_path = log_directory / f"{now.strftime('%H')}.jsonl"
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            log_file.write("\n")
        return log_path


def _mask_secrets(value: Any) -> Any:
    """payload 内の secret 値を再帰的に masking する。

    Args:
        value: masking 対象の任意 JSON-like value。

    Returns:
        secret key の値を `***` に置き換えた value。
    """

    if isinstance(value, dict):
        masked_value: dict[str, Any] = {}
        for key, child_value in value.items():
            lowered_key = str(key).lower()
            if _is_secret_key(lowered_key):
                masked_value[key] = "***"
            else:
                masked_value[key] = _mask_secrets(child_value)
        return masked_value
    if isinstance(value, list):
        return [_mask_secrets(child_value) for child_value in value]
    return value


def _is_secret_key(lowered_key: str) -> bool:
    """key 名が secret を含むものか判定する。

    Args:
        lowered_key: 小文字化済みの key 名。

    Returns:
        secret とみなす key なら `True`。
    """

    secret_fragments = (
        "authorization",
        "api_key",
        "apikey",
        "password",
        "token",
        "secret",
    )
    return any(secret_fragment in lowered_key for secret_fragment in secret_fragments)
