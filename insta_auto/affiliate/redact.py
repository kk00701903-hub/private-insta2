"""로그·에러 메시지에서 토큰/키를 지운다."""
from __future__ import annotations

import re
from collections.abc import Iterable

_PATTERNS = [
    re.compile(r"(access_token=)[^&\s\"']+", re.I),
    re.compile(r"(Authorization['\"]?\s*[:=]\s*['\"]?)[^'\"\n]+", re.I),
    re.compile(r"(signature=)[0-9a-f]{16,}", re.I),
    re.compile(r"(access-key=)[^,\s]+", re.I),
]


def redact(text: object, secrets: Iterable[str] = ()) -> str:
    s = str(text)
    for value in secrets:
        if value and len(value) >= 4:
            s = s.replace(value, "***")
    for pat in _PATTERNS:
        s = pat.sub(r"\1***", s)
    return s
