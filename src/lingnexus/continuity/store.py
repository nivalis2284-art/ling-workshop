"""Small helpers shared by the continuity services; no business policy here."""

from __future__ import annotations

import base64
import hashlib
import json

from lingnexus.common.errors import Conflict


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    if not isinstance(value, (str, bytes)):
        value = encode(value)
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def cursor(value) -> str:
    return base64.urlsafe_b64encode(encode(value).encode()).decode().rstrip("=")


def decode_cursor(value: str | None, *, expected: str) -> int:
    if not value:
        return 0
    try:
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if (
            not isinstance(data, list)
            or len(data) != 3
            or data[0] != expected
            or not isinstance(data[1], int)
            or data[1] < 0
            or not isinstance(data[2], int)
            or data[2] < 0
        ):
            raise ValueError
        return data[1]
    except (ValueError, TypeError, UnicodeError) as error:
        raise Conflict("分页游标无效或已失效，请重新打开目录。") from error


def cursor_for(value: str, revision: int, offset: int) -> str:
    return cursor([value, revision, offset])
