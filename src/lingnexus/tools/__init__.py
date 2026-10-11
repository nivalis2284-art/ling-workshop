"""Adapters for caller-authorized file access and isolated command execution."""
from ._jobobject import available as resource_limits_available
from ._paths import FileAccessDenied
from .docker import DockerExecutor, ExecutionLimits, ExecutionResult
from .sandbox import (
    DEFAULT_IMAGE,
    ContainerSandbox,
    ExecutionSandbox,
    LocalLimits,
    RestrictedLocalSandbox,
    SandboxLevel,
    configured_image,
    create_executor,
    describe,
    local_environment,
    probe_engine,
)
from .workspace import FileLimitExceeded, FilePermissions, WorkspaceTools

__all__ = [
    "DEFAULT_IMAGE", "ContainerSandbox", "DockerExecutor", "ExecutionLimits",
    "ExecutionResult", "ExecutionSandbox", "FileAccessDenied", "FileLimitExceeded",
    "FilePermissions", "LocalLimits", "RestrictedLocalSandbox", "SandboxLevel",
    "WorkspaceTools", "configured_image", "create_executor", "describe",
    "local_environment", "probe_engine", "resource_limits_available",
]
