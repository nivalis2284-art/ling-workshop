"""Optional MCP client: connect, list tools, call tools, negotiate a version.

Nothing in this package grants authority. A remote tool is declared into the same
registry the agent loops already read and is dispatched by the same dispatcher,
so the permission hook, the side-effect confirmation gate and the execution
sandbox apply to it exactly as they do to a local tool.
"""

from .protocol import (
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpError,
    remote_tool_name,
    split_remote_tool,
)
from .service import McpService, ServerConfig, parse_servers

__all__ = [
    "PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "McpError",
    "McpService",
    "ServerConfig",
    "parse_servers",
    "remote_tool_name",
    "split_remote_tool",
]
