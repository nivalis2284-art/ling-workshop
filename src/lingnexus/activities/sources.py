"""Bounded observations from explicitly authorized, untrusted sources."""

import asyncio
import hashlib
import ipaddress
import socket
from pathlib import Path

import httpx

from lingnexus.common.errors import PermissionDenied
from lingnexus.tools._paths import FileAccessDenied, directory, local_path, regular_file

MAX_BYTES = 1_048_576
MAX_EXCERPT = 16_384


class ObservationError(Exception):
    """Only the finite code is stored, never exception or upstream error text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def file_target(value: str) -> str:
    try:
        path = local_path(value)
    except FileAccessDenied:
        raise PermissionDenied("Unsafe file path") from None
    if not path.is_absolute():
        raise ValueError("An exact absolute file path is required")
    return str(path)


def validate_file(target: str) -> None:
    """Validate an owner selection's metadata without reading any contents."""
    try:
        path = Path(target)
        directory(path.parent)
        regular_file(path.lstat())
    except FileAccessDenied:
        raise PermissionDenied("An exact regular file without linked aliases is required") from None
    except OSError:
        raise ValueError("The selected file must exist") from None


def web_target(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("An exact HTTP(S) URL is required")
    if any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("Unsafe URL")
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError):
        raise ValueError("Invalid URL") from None
    if (
        url.scheme not in {"https", "http"}
        or not url.host
        or url.userinfo
        or url.fragment
        or url.port == 0
    ):
        raise ValueError("An HTTP(S) URL without credentials or fragments is required")
    host = url.host.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise PermissionDenied("Local destinations are not permitted")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not public_ip(str(address)):
        raise PermissionDenied("Nonpublic destinations are not permitted")
    return str(url), url.scheme + "://" + url.netloc.decode("ascii")


def web_origin(value: str) -> str:
    target, origin = web_target(value)
    url = httpx.URL(target)
    if url.path not in {"", "/"} or url.query:
        raise ValueError("Origin grants must contain only scheme, host and optional port")
    return origin


def public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value.split("%")[0])
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address.sixtofour is not None or address.teredo is not None:
            return False
    return address.is_global and not address.is_multicast and not address.is_reserved


def observation(data: bytes, provenance: dict) -> dict:
    if len(data) > MAX_BYTES:
        raise ObservationError("content_too_large")
    return {
        "hash": hashlib.sha256(data).hexdigest(),
        "byte_count": len(data),
        "excerpt": data[:MAX_EXCERPT].decode("utf-8", errors="replace"),
        "truncated": len(data) > MAX_EXCERPT,
        "provenance": provenance,
        "untrusted_source": True,
    }


def read_file(application, activity: dict) -> dict:
    # Permissions owns traversal, linked-file rejection and its 1 MiB read bound.
    text = application.permissions.read_text(activity["agent_id"], Path(activity["target"]))
    return observation(text.encode("utf-8"), {"path": activity["target"]})


async def fetch_web(client: httpx.AsyncClient, target: str) -> dict:
    original = httpx.URL(target)
    port = original.port or (443 if original.scheme == "https" else 80)
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            original.host,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        raise ObservationError("dns_failed") from None
    ips = list(dict.fromkeys(str(item[4][0]) for item in addresses))
    if not ips or any(not public_ip(ip) for ip in ips):
        raise ObservationError("unsafe_address")
    # Pin the connection to the validated IP. TLS still verifies the original hostname.
    pinned = original.copy_with(host=ips[0])
    request = httpx.Request(
        "GET",
        pinned,
        headers={
            "Host": original.netloc.decode("ascii"),
            "User-Agent": "LingNexus-Activities/1",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        },
        extensions={
            "sni_hostname": original.host,
            "timeout": {"connect": 5.0, "read": 10.0, "write": 5.0, "pool": 5.0},
        },
    )
    # Build a fresh request so injected client cookies/default authorization are not sent.
    response = await client.send(request, stream=True, follow_redirects=False, auth=None)
    try:
        if response.history or 300 <= response.status_code < 400:
            raise ObservationError("redirect_blocked")
        if not 200 <= response.status_code < 300:
            raise ObservationError("http_status")
        # Reject unsolicited compression before allocating a decompressed chunk.
        if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
            raise ObservationError("unsupported_encoding")
        if response.url != pinned:
            raise ObservationError("unexpected_response_source")
        size = response.headers.get("content-length")
        if size and (not size.isdecimal() or int(size) > MAX_BYTES):
            raise ObservationError("content_too_large")
        data = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65_536):
            if len(data) + len(chunk) > MAX_BYTES:
                raise ObservationError("content_too_large")
            data.extend(chunk)
        return observation(
            bytes(data),
            {
                "requested_url": target,
                "response_url": target,
                "transport_url": str(response.url),
                "connected_ip": ips[0],
                "status_code": response.status_code,
                "etag": response.headers.get("etag", "")[:512],
                "last_modified": response.headers.get("last-modified", "")[:512],
                "content_type": response.headers.get("content-type", "")[:256],
            },
        )
    finally:
        await response.aclose()
