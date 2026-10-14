"""The optional MCP client: configuration, lazy connections and tool shaping.

Two rules decide every choice here (see ``docs/计划/2026-10-11-F阶段-能力扩展契约.md``):

1. **Optional** — with nothing configured this service returns no tools and opens
   no connections. A configured-but-broken server contributes no tools and does
   not fail a turn; the failure is remembered rather than retried in a storm.
2. **No extra authority** — a remote tool is declared as ``mcp__<server>__<tool>``
   in the same registry the loops already read, so it passes the same
   ``BEFORE_TOOL`` hook (permission and side-effect confirmation) and the same
   dispatcher as any local tool. MCP grants nothing by itself.
"""

from __future__ import annotations

import json
import threading

from pydantic import BaseModel, ConfigDict

from lingnexus.actions.registry import ToolSpec
from lingnexus.common.errors import DomainError
from lingnexus.configuration.settings import read_json, write_json

from .client import HttpTransport, McpConnection, StdioTransport
from .protocol import McpError, remote_tool_name, split_remote_tool

SETTINGS_KEY = "mcp.servers"
DEFAULT_TIMEOUT = 15.0
_RESULT_CHARS = 32_000
_MAX_BLOCKS = 200
"""Per call. The joined text is capped at ``_RESULT_CHARS`` anyway; this only
bounds the work a chatty server can make the client do before that."""

# A remote tool validates against the schema its server published, so the local
# placeholder must accept any object shape.
class _RemoteArguments(BaseModel):
    model_config = ConfigDict(extra="allow")


class ServerConfig:
    """One configured server, already validated."""

    __slots__ = ("args", "command", "cwd", "env", "headers", "name", "transport", "url")

    def __init__(self, raw: dict):
        self.name = str(raw["name"])
        kind = str(raw.get("transport") or ("http" if raw.get("url") else "stdio"))
        self.transport = kind
        self.command = str(raw.get("command") or "")
        self.args = [str(item) for item in raw.get("args") or []]
        self.env = {str(k): str(v) for k, v in (raw.get("env") or {}).items()}
        self.cwd = str(raw["cwd"]) if raw.get("cwd") else None
        self.url = str(raw.get("url") or "")
        self.headers = {str(k): str(v) for k, v in (raw.get("headers") or {}).items()}
        if not self.name or "__" in self.name:
            raise ValueError("服务器名不能为空且不能包含双下划线")
        if kind == "stdio" and not self.command:
            raise ValueError("stdio 服务器缺少 command")
        if kind not in {"stdio", "http"}:
            raise ValueError(f"不支持的传输方式：{kind}")
        if kind == "http" and not self.url:
            raise ValueError("http 服务器缺少 url")

    def connection(self, timeout: float) -> McpConnection:
        if self.transport == "http":
            return McpConnection(HttpTransport(self.url, self.headers), timeout=timeout)
        return McpConnection(
            StdioTransport(self.command, self.args, self.env or None, self.cwd),
            timeout=timeout,
        )


def parse_servers(raw: object) -> list[ServerConfig]:
    """Read the setting into configs; anything malformed is skipped, never fatal."""
    if isinstance(raw, dict):
        raw = raw.get("servers")
    if not isinstance(raw, list):
        return []
    configs: list[ServerConfig] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        try:
            config = ServerConfig(item)
        except (KeyError, TypeError, ValueError):
            continue
        if config.name in seen:
            continue
        seen.add(config.name)
        configs.append(config)
    return configs


class McpService:
    """Configured servers, their tools, and calls to those tools."""

    def __init__(self, database, *, timeout: float = DEFAULT_TIMEOUT):
        self._database = database
        self._timeout = timeout
        self._connections: dict[str, McpConnection] = {}
        self._specs: dict[str, ToolSpec] = {}
        self.failures: dict[str, str] = {}
        self._loaded = False
        # Calls now run off the event loop (``asyncio.to_thread``), so two runs
        # can reach one connection at once. A connection is one child pipe or
        # session: interleaving their JSON-RPC ids would cross replies.
        self._lock = threading.RLock()

    # --- configuration ------------------------------------------------------

    def configured(self) -> bool:
        return bool(self.servers())

    def servers(self) -> list[ServerConfig]:
        value = read_json(self._database, SETTINGS_KEY, None)
        if value is None:
            return []
        try:
            return parse_servers(value)
        except (TypeError, ValueError):
            return []

    def view(self) -> list[dict]:
        """The stored server entries for the owner's settings view.

        Entries are returned as stored, including the disabled ``enabled:
        False`` flag and any configured ``env``/``headers``: this list is
        local, single-owner configuration, never a model input.
        """
        value = read_json(self._database, SETTINGS_KEY, None)
        if isinstance(value, dict):
            value = value.get("servers")
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    def configure(self, servers: list[dict]) -> list[dict]:
        """Validate and store the list, failing loud; the next use re-reads it.

        Unlike startup parsing (which skips broken entries), an owner edit must
        be told exactly what is wrong — a half-saved server list is worse than
        a rejected one. Stored entries are canonical; unknown keys are dropped.
        """
        cleaned: list[dict] = []
        seen: set[str] = set()
        for item in servers:
            if not isinstance(item, dict):
                raise DomainError("每个服务器条目都必须是一个对象。")
            raw = dict(item)
            raw.setdefault("enabled", True)
            try:
                config = ServerConfig(raw)
            except (KeyError, TypeError, ValueError) as error:
                raise DomainError(f"服务器配置无效：{error}") from None
            if config.name in seen:
                raise DomainError("服务器名不能重复：" + config.name)
            seen.add(config.name)
            entry: dict = {
                "name": config.name,
                "transport": config.transport,
                "enabled": raw.get("enabled") is not False,
            }
            if config.transport == "http":
                entry["url"] = config.url
                if config.headers:
                    entry["headers"] = config.headers
            else:
                entry["command"] = config.command
                if config.args:
                    entry["args"] = config.args
                if config.env:
                    entry["env"] = config.env
                if config.cwd:
                    entry["cwd"] = config.cwd
            cleaned.append(entry)
        write_json(self._database, SETTINGS_KEY, {"servers": cleaned})
        self.reload()
        return cleaned

    # --- discovery ----------------------------------------------------------

    def specs(self) -> list[ToolSpec]:
        """Remote tools currently offered. Connects on first use, never raises."""
        self._ensure()
        return list(self._specs.values())

    def get(self, name: str) -> ToolSpec | None:
        self._ensure()
        return self._specs.get(name)

    def _ensure(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            for config in self.servers():
                self._connect(config)

    def reload(self) -> None:
        """Drop cached state so the next use re-reads the setting (tests, config change)."""
        with self._lock:
            self.close()
            self._connections.clear()
            self._specs.clear()
            self.failures.clear()
            self._loaded = False

    def _connect(self, config: ServerConfig) -> None:
        connection = config.connection(self._timeout)
        try:
            connection.start()
        except (McpError, OSError, ValueError) as exc:
            connection.close()
            self.failures[config.name] = str(exc)
            return
        self._connections[config.name] = connection
        for tool in connection.tools:
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                continue
            declared = remote_tool_name(config.name, name)
            schema = tool.get("inputSchema")
            description = tool.get("description")
            self._specs[declared] = ToolSpec(
                name=declared,
                description=(
                    f"[MCP:{config.name}] "
                    + (description if isinstance(description, str) and description else name)
                ),
                parameters=_RemoteArguments,
                kind="mcp",
                input_schema=schema if isinstance(schema, dict) else {"type": "object"},
            )

    # --- calls --------------------------------------------------------------

    def call(self, name: str, arguments: dict) -> tuple[dict, None]:
        """Invoke a remote tool; always returns a payload, never raises.

        A timeout or a dropped connection *after* the request went out is reported
        as ``unknown``: the server may well have carried the call out, and saying
        "failed" would invite a duplicate side effect.
        """
        split = split_remote_tool(name)
        if split is None:
            return {"status": "failed", "error_code": "mcp_unknown_tool"}, None
        server, tool = split
        with self._lock:
            self._ensure()
            connection = self._connections.get(server)
            if connection is None:
                reason = self.failures.get(server, "服务器未连接")
                return {
                    "status": "failed",
                    "error_code": "mcp_server_unavailable",
                    "result": {"server": server, "message": reason},
                }, None
            try:
                result = connection.call_tool(tool, arguments)
            except McpError as exc:
                status = (
                    "unknown" if exc.code in {"mcp_timeout", "mcp_connection_closed"} else "failed"
                )
                return {
                    "status": status,
                    "error_code": exc.code,
                    "result": {"server": server, "tool": tool, "message": exc.message},
                }, None
            except (OSError, ValueError) as exc:
                return {
                    "status": "unknown",
                    "error_code": "mcp_call_unknown",
                    "result": {"server": server, "tool": tool, "message": str(exc)},
                }, None
        return self._shape(server, tool, result), None

    def _shape(self, server: str, tool: str, result: dict) -> dict:
        content = result.get("content")
        blocks = content if isinstance(content, list) else []
        rendered = []
        for block in blocks[:_MAX_BLOCKS]:
            if isinstance(block, dict) and block.get("type") == "text":
                rendered.append(str(block.get("text", ""))[:_RESULT_CHARS])
            elif isinstance(block, dict):
                rendered.append(json.dumps(block, ensure_ascii=False)[:_RESULT_CHARS])
        failed = bool(result.get("isError"))
        payload = {
            "server": server,
            "tool": tool,
            "text": "\n".join(rendered)[:_RESULT_CHARS],
        }
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            payload["structured"] = structured
        if failed:
            return {
                "status": "failed",
                "error_code": "mcp_tool_error",
                "result": payload,
            }
        return {"status": "completed", "result": payload}

    def close(self) -> None:
        with self._lock:
            for connection in self._connections.values():
                connection.close()
            self._connections.clear()
