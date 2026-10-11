"""The single tool declaration source shared by every agent loop.

Chat and task loops must read tool declarations from one registry and nowhere
else; a tool that is not declared here cannot be requested. This module is pure
data — it never touches the filesystem, the sandbox, permissions or any service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

ToolKind = Literal["command", "file", "browser", "meta", "mcp"]
ToolStatus = Literal["completed", "failed", "blocked", "unknown"]


@dataclass(frozen=True)
class ToolSpec:
    """One declared tool. `parameters` is a pydantic model used for JSON Schema.

    A remote (MCP) tool arrives with its own JSON Schema rather than a pydantic
    model, so ``input_schema`` carries that schema verbatim; ``parameters`` then
    only keeps the field non-optional for local tools.
    """

    name: str
    description: str
    parameters: type[BaseModel]
    kind: ToolKind
    input_schema: dict | None = None

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema or self.parameters.model_json_schema(),
        }


@dataclass
class ToolResult:
    """The frozen result shape returned to every loop.

    `content` is what goes back to the model: a text block or a list of
    structured blocks. Callers that need the structured tool payload read
    ``data``. ``effect_id`` cross-references a real-world side effect so it can
    be reconciled afterwards; ``unknown`` never becomes a silent success.
    """

    status: ToolStatus
    content: str | list[dict] | None = None
    error_code: str | None = None
    effect_id: str | None = None

    @property
    def data(self) -> dict:
        """The structured tool payload carried in `content`, if any."""
        if isinstance(self.content, list) and self.content:
            return self.content[0]
        if isinstance(self.content, str):
            return {"text": self.content}
        return {}


class ToolRegistry:
    """The one place tools are declared; ordered, name-unique, read-only after build."""

    def __init__(self, specs: list[ToolSpec] | tuple[ToolSpec, ...] = ()):
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._specs[spec.name] = spec

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def names(self) -> list[str]:
        return list(self._specs)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def schema(self) -> list[dict]:
        """The tool declarations sent to the model, in declaration order."""
        return [spec.schema() for spec in self._specs.values()]
