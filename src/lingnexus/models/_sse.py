"""Bounded SSE framing over arbitrary byte packets, with strict UTF-8."""

import json
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Any

from .types import StreamLimits


class ProtocolError(Exception):
    def __init__(self, code: str = "malformed_stream", *, reason: str | None = None) -> None:
        self.code = code
        self.reason = reason
        super().__init__(code)


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def json_object(text: str, code: str = "malformed_stream") -> dict[str, Any]:
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ProtocolError(code, reason="invalid_json") from None
    if not isinstance(value, dict):
        raise ProtocolError(code)
    return value


@dataclass(frozen=True, slots=True)
class SSEFrame:
    event: str | None
    data: str


async def frames(chunks: AsyncIterable[bytes], limits: StreamLimits) -> AsyncIterator[SSEFrame]:
    line = bytearray()
    data: list[str] = []
    event: str | None = None
    total = size = count = 0
    first_line = True
    after_cr = False
    async for chunk in chunks:
        total += len(chunk)
        if total > limits.max_stream_bytes:
            raise ProtocolError("stream_limit")
        for byte in chunk:
            if after_cr and byte == 10:
                after_cr = False
                continue
            after_cr = byte == 13
            size += 1
            if size > limits.max_event_bytes:
                raise ProtocolError("stream_limit")
            if byte not in (10, 13):
                line.append(byte)
                continue
            try:
                decoded = line.decode("utf-8-sig" if first_line else "utf-8")
            except UnicodeDecodeError:
                raise ProtocolError(reason="invalid_utf8") from None
            first_line = False
            line.clear()
            if not decoded:
                if data:
                    count += 1
                    if count > limits.max_events:
                        raise ProtocolError("stream_limit")
                    yield SSEFrame(event, "\n".join(data))
                data = []
                event = None
                size = 0
            elif not decoded.startswith(":"):
                name, _, value = decoded.partition(":")
                value = value.removeprefix(" ")
                if name == "data":
                    data.append(value)
                elif name == "event":
                    event = value
    # An unterminated frame is not dispatched. The gateway reports unexpected EOF
    # unless it has already observed a provider's explicit terminal event.
