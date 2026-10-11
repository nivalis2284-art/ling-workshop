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

from pydantic import BaseModel, ConfigDict

from lingnexus.actions.registry import ToolSpec
from lingnexus.configuration.settings import read_json

from .client import HttpTransport, McpConnection, StdioTransport
from .protocol import McpError, remote_tool_name, split_remote_tool

SETTINGS_KEY = "mcp.servers"
DEFAULT_TIMEOUT = 15.0
_RESULT_CHARS = 32_000

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

    # --- discovery ----------------------------------------------------------

    def specs(self) -> list[ToolSpec]:
        """Remote tools currently offered. Connects on first use, never raises."""
        self._ensure()
        return list(self._specs.values())

    def get(self, name: str) -> ToolSpec | None:
        self._ensure()
        return self._specs.get(name)

    def _ensure(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        for config in self.servers():
            self._connect(config)

    def reload(self) -> None:
        """Drop cached state so the next use re-reads the setting (tests, config change)."""
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
            status = "unknown" if exc.code in {"mcp_timeout", "mcp_connection_closed"} else "failed"
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
        for block in blocks[:_RESULT_CHARS]:
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
        for connection in self._connections.values():
            connection.close()
        self._connections.clear()
