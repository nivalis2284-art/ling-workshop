"""Bounded native model streaming. See README.md for the public contract.

Runtime requirements: Python 3.12+, pydantic >=2.8,<3, httpx >=0.28,<0.29.
Tests require pytest >=8,<9; no async pytest plugin or provider SDK is needed.
"""

from .gateway import ModelGateway
from .types import (
    APIFamily,
    ModelCapabilities,
    ModelError,
    ModelEvent,
    ModelProfile,
    ProviderContinuation,
    RuntimeCredentials,
    StreamLimits,
    ToolCall,
    Usage,
)

__all__ = [
    "APIFamily",
    "ModelCapabilities",
    "ModelError",
    "ModelEvent",
    "ModelGateway",
    "ModelProfile",
    "ProviderContinuation",
    "RuntimeCredentials",
    "StreamLimits",
    "ToolCall",
    "Usage",
]
