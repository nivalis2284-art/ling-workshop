"""The MCP wire contract we implement, and nothing more.

Deliberately small: JSON-RPC 2.0 with three methods (``initialize``,
``tools/list``, ``tools/call``) plus the ``notifications/initialized`` handshake
notice. No prompts, resources, sampling, subscriptions or multi-round requests —
those are out of scope for the minimal set, and a server that only offers them
simply contributes no tools.

Standard library only. Nothing here touches the filesystem, permissions or the
sandbox; a remote tool is dispatched exactly like a local one.
"""

from __future__ import annotations

import json

PROTOCOL_VERSION = "2025-06-18"
"""The revision we advertise first."""

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
"""Revisions we can speak. A server answering outside this set is refused."""

CLIENT_INFO = {"name": "lingnexus", "version": "0.2.0"}

TOOL_PREFIX = "mcp__"
"""Remote tools are declared as ``mcp__<server>__<tool>``.

Two underscores are used as the separator and no local tool name contains them,
so a remote name can never collide with a built-in one.
"""


class McpError(Exception):
    """A protocol-level failure carrying a stable code for the loop to report."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def remote_tool_name(server: str, tool: str) -> str:
    return f"{TOOL_PREFIX}{server}__{tool}"


def split_remote_tool(name: str) -> tuple[str, str] | None:
    """Split ``mcp__<server>__<tool>``; None when the name is not one."""
    if not name.startswith(TOOL_PREFIX):
        return None
    remainder = name[len(TOOL_PREFIX) :]
    server, separator, tool = remainder.partition("__")
    if not separator or not server or not tool:
        return None
    return server, tool


def request(identifier: int, method: str, params: dict | None = None) -> dict:
    message: dict = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification(method: str, params: dict | None = None) -> dict:
    message: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def encode(message: dict) -> str:
    """One newline-delimited frame; MCP's stdio transport is line oriented."""
    return json.dumps(message, ensure_ascii=False) + "\n"


def decode(line: str) -> dict:
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise McpError("mcp_invalid_message", "服务器返回的不是 JSON-RPC 消息") from exc
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise McpError("mcp_invalid_message", "服务器返回的消息不是 JSON-RPC 2.0")
    return message


def negotiate(server_version: object) -> str:
    """Accept the server's revision only if we actually speak it.

    MCP servers answer ``initialize`` with the revision they will use. A version
    outside our set is a real incompatibility, so it is reported as a failure
    rather than quietly proceeding on a guess.
    """
    if not isinstance(server_version, str) or not server_version:
        raise McpError("mcp_protocol_mismatch", "服务器未返回协议版本")
    if server_version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise McpError(
            "mcp_protocol_mismatch",
            f"协议版本不匹配：服务器使用 {server_version}，本端支持 "
            + "、".join(SUPPORTED_PROTOCOL_VERSIONS),
        )
    return server_version


def error_from(message: dict) -> McpError:
    error = message.get("error")
    if isinstance(error, dict):
        return McpError(
            "mcp_server_error",
            str(error.get("message") or error.get("code") or "服务器返回错误"),
        )
    return McpError("mcp_server_error", "服务器返回错误")
