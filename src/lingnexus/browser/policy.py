"""Model action contracts and exact owner-granted HTTP origin policy."""

import asyncio
import ipaddress
import socket
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from lingnexus.common.errors import PermissionDenied
from lingnexus.common.types import Value


def normalize_origin(url: str, *, origin_only: bool = False) -> str:
    if not url or any(ord(c) <= 32 for c in url) or chr(92) in url:
        raise ValueError("Invalid HTTP origin")
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("Only HTTP(S) origins without credentials are supported")
    if origin_only and (parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise ValueError("An origin must not contain a path, query, or fragment")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    if any(c in host for c in "*%/@?#") or host.endswith("."):
        raise ValueError("Origin must name one exact host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if any(
            not label or not all(c.isalnum() or c == "-" for c in label)
            for label in host.split(".")
        ):
            raise ValueError("Invalid origin host") from None
    else:
        host = address.compressed
    port = parts.port
    if port == 0:
        raise ValueError("Invalid origin port")
    authority = f"[{host}]" if ":" in host else host
    if port and port != (443 if parts.scheme == "https" else 80):
        authority += f":{port}"
    return f"{parts.scheme}://{authority}"


def is_local_host(host: str) -> bool:
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return not address.is_global


class OriginGrant(Value):
    origin: str = Field(min_length=1, max_length=2048)
    allow_local: bool = False

    @model_validator(mode="after")
    def exact_origin(self) -> Self:
        object.__setattr__(self, "origin", normalize_origin(self.origin, origin_only=True))
        return self


class ActionInput(Value):
    """The entire model-facing surface; it carries no owner authority."""

    request_id: str = Field(min_length=1, max_length=200)
    action: Literal["goto", "read", "fill", "click", "submit", "send", "publish"]
    origin_revision: int = Field(ge=0)
    page_version: str | None = Field(default=None, min_length=1, max_length=256)
    url: str | None = Field(default=None, min_length=1, max_length=8192)
    selector: str | None = Field(default=None, min_length=1, max_length=1000)
    value: str | None = Field(default=None, max_length=20000)
    max_chars: int = Field(default=8000, ge=1, le=20000)

    @model_validator(mode="after")
    def action_arguments(self) -> Self:
        if self.action == "goto":
            if self.url is None or self.selector is not None or self.value is not None:
                raise ValueError("goto requires only a URL")
            normalize_origin(self.url)
        else:
            if self.page_version is None or self.url is not None:
                raise ValueError("Current page_version is required")
            if self.action == "read":
                if self.selector is not None or self.value is not None:
                    raise ValueError("read returns bounded visible DOM text")
            elif self.selector is None or (self.action == "fill" and self.value is None):
                raise ValueError("A target selector (and fill value) is required")
            elif self.action != "fill" and self.value is not None:
                raise ValueError("Click intents cannot fill values")
        return self


EFFECT_ACTIONS = frozenset({"click", "submit", "send", "publish"})


async def check_local(grant: OriginGrant) -> None:
    """Check literal and DNS-resolved private addresses, failing closed."""
    if grant.allow_local:
        return
    parts = urlsplit(grant.origin)
    host = parts.hostname or ""
    if is_local_host(host):
        raise PermissionDenied("Local/private origins require explicit owner allow_local")
    try:
        records = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                host,
                parts.port or (443 if parts.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
            timeout=5,
        )
    except (OSError, TimeoutError):
        raise PermissionDenied("Origin address could not be verified") from None
    if not records or any(is_local_host(str(record[4][0])) for record in records):
        raise PermissionDenied("Local/private origins require explicit owner allow_local")


async def authorize(state: dict, url: str, expected_revision: int | None = None) -> str:
    origin = normalize_origin(url)
    grants = {g["origin"]: OriginGrant.model_validate(g) for g in state["grants"]}
    if origin not in grants:
        raise PermissionDenied("This exact browser origin has no owner grant")
    if expected_revision is not None and expected_revision != state["revision"]:
        raise PermissionDenied("Browser origin grant changed; inspect current state")
    await check_local(grants[origin])
    return origin
