"""Text normalization helpers shared by mcon frontends."""

from __future__ import annotations

import re


_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def utf8_safe(value: str) -> str:
    """Replace unpaired surrogate code points before UTF-8 IO."""

    if not _SURROGATE_RE.search(value):
        return value
    return _SURROGATE_RE.sub("\ufffd", value)
