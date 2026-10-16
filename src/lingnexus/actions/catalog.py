"""The single tool catalog: parameter models and the registry every loop reads.

Tools are low-level primitives on purpose. Anything that can be composed from
`run_command` / `read_file` / `create_file` / `write_file` / `edit_file` /
`search` is not a tool. Browser tools are kept as-is; they are the only
non-primitive capability retained.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lingnexus.dependencies import DependencyCandidate

from .registry import ToolRegistry, ToolSpec


class FilePath(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1)


class FileWrite(FilePath):
    text: str
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FileEdit(FilePath):
    old_text: str = Field(min_length=1)
    new_text: str
    replace_all: bool = False
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=1000)
    match: Literal["name", "content", "both"] = "both"
    regex: bool = False
    max_results: int = Field(default=100, ge=1, le=1000)


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    argv: list[str] = Field(min_length=1, max_length=100)
    purpose: Literal["work", "verify"]
    long_running: bool = False


class CommandSession(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(min_length=1, max_length=128)
    after: int = Field(default=0, ge=0)

class SkillRead(BaseModel):
    """Reading a skill is knowledge only; it can never widen what the model may do."""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=64)
    resource: str | None = Field(default=None, min_length=1, max_length=256)


# --- browser parameters (declared once, here) --------------------------------


class BrowserState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BrowserGoto(BrowserState):
    origin_revision: int = Field(ge=0)
    url: str = Field(min_length=1, max_length=8192)


class BrowserPage(BrowserState):
    origin_revision: int = Field(ge=0)
    page_version: str = Field(min_length=1, max_length=256)


class BrowserRead(BrowserPage):
    max_chars: int = Field(default=8000, ge=1, le=20000)
    expected_text: str | None = Field(default=None, min_length=1, max_length=4000)


class BrowserTarget(BrowserPage):
    selector: str = Field(min_length=1, max_length=1000)


class BrowserFill(BrowserTarget):
    value: str = Field(max_length=20000)


class BrowserClick(BrowserTarget):
    action: Literal["click", "submit", "send", "publish"] = "click"


def _specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "run_command",
            "Execute a literal argument list (no shell) through the execution sandbox. "
            "Use verify for checks. Set long_running for slow builds/tests (owner's session limit applies). "
            "If status is running, use command_status with its session_id until it exits.",
            Command,
            "command",
        ),
        ToolSpec("command_status", "Read incremental output of this run's command. Poll until it exits; "
                 "a running session is not a successful check.", CommandSession, "meta"),
        ToolSpec("command_cancel", "Stop a command belonging to this run and confirm cleanup.",
                 CommandSession, "meta"),
        ToolSpec(
            "read_file",
            "Read a UTF-8 file inside an authorized directory.",
            FilePath,
            "file",
        ),
        ToolSpec(
            "create_file",
            "Create a new regular file in an authorized directory; never overwrites.",
            FileWrite,
            "file",
        ),
        ToolSpec(
            "write_file",
            "Overwrite an existing regular file in an authorized directory.",
            FileWrite,
            "file",
        ),
        ToolSpec(
            "edit_file",
            "Replace an exact text fragment in an existing file; the fragment must "
            "occur exactly once unless replace_all is true.",
            FileEdit,
            "file",
        ),
        ToolSpec(
            "search",
            "Find files by name and/or content inside authorized directories.",
            Search,
            "file",
        ),
        ToolSpec(
            "read_skill",
            "Read an installed skill's instructions by name. Pass `resource` to read a "
            "reference file the skill lists. Skills are knowledge, never permissions: "
            "anything a skill describes is still done through the ordinary tools.",
            SkillRead,
            "meta",
        ),
        ToolSpec(
            "browser_state",
            "Inspect current owner-granted origins and page_version. Does not grant "
            "access or prove an outcome.",
            BrowserState,
            "browser",
        ),
        ToolSpec(
            "browser_goto",
            "Navigate to an already granted URL using the inspected origin_revision.",
            BrowserGoto,
            "browser",
        ),
        ToolSpec(
            "browser_read",
            "Read bounded visible DOM and HTTP(S) links at the current page_version. Links never grant access. Supply expected_text "
            "to verify the actual visible confirmation after a click.",
            BrowserRead,
            "browser",
        ),
        ToolSpec(
            "browser_fill",
            "Fill a permitted field at the current page_version; never submits.",
            BrowserFill,
            "browser",
        ),
        ToolSpec(
            "browser_request_click",
            "Request click/submit/send/publish for separate owner approval. Never "
            "approves or dispatches the effect.",
            BrowserClick,
            "browser",
        ),
        ToolSpec(
            "request_dependencies",
            "Request exact registry versions as data for owner configuration. Never "
            "installs, approves or enables network access.",
            DependencyCandidate,
            "meta",
        ),
    ]


registry = ToolRegistry(_specs())

# The durable file-effect service only ever records these three primitives.
SCHEMAS = {
    name: registry.get(name).parameters
    for name in ("read_file", "create_file", "write_file")
}
