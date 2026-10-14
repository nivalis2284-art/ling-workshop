"""Owner-managed MCP server list (the ``mcp.servers`` setting).

MCP is optional: with nothing configured the service stays inert, and a broken
server contributes no tools. Editing the list here writes the same setting the
client reads and asks the live service to reload, so the next use re-connects
with the new configuration. A rejected edit is reported per entry — the current
configuration is never replaced by a half-valid one.
"""

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner

router = APIRouter(dependencies=[Depends(require_owner)])


class McpServersUpdate(Input):
    servers: list[dict] = Field(max_length=50)


@router.get("/mcp/servers")
def read_servers(request: Request):
    return {"servers": request.app.state.services.mcp.view()}


@router.put("/mcp/servers")
def write_servers(value: McpServersUpdate, request: Request):
    return {"servers": request.app.state.services.mcp.configure(value.servers)}
