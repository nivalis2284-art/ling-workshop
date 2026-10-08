"""Durable, single-process chat runs independent of SSE subscriber lifetimes.

Create one ChatRuntime(application) per database in the server lifespan, assign
it to server.state.chat_runtime, and await aclose() before Application.close().
Import the owner-protected routes from lingnexus.api.chat and mount at both the
root and /api. No credentials, raw provider responses or opaque reasoning are
persisted. Export/restore must include chat_runs and chat_events.
"""

from .service import ChatRuntime
from .types import ChatRequest

__all__ = ["ChatRequest", "ChatRuntime"]
