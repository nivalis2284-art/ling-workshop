"""Bounded structural diagnostics, without exception messages or native data."""

import traceback
from pathlib import Path
from uuid import uuid4

_PACKAGE = Path(__file__).absolute().parents[1]
_TYPES = frozenset({"ValueError", "TypeError", "KeyError", "IndexError", "AttributeError",
                    "RuntimeError", "OSError", "PermissionError", "FileNotFoundError",
                    "OperationalError", "IntegrityError", "TimeoutError", "AssertionError"})
_STAGES = frozenset({"initialization", "checkpoint", "model", "tools", "completion", "control"})
_REASONS = frozenset({"response_id_changed", "missing_terminal", "invalid_json", "invalid_utf8"})


def diagnostic(stage: str, exc: BaseException | None = None, *, reason: str | None = None) -> dict:
    """Only local source locations and fixed identifiers may cross this boundary."""
    result = {"diagnostic_id": uuid4().hex,
              "stage": stage if stage in _STAGES else "initialization"}
    if reason in _REASONS:
        result["reason"] = reason
    if exc is not None:
        name = type(exc).__name__
        result["exception_type"] = name if name in _TYPES else "Exception"
        frames = []
        for frame in traceback.extract_tb(exc.__traceback__):
            path = Path(frame.filename).absolute()
            if path.is_relative_to(_PACKAGE) and path.suffix == ".py":
                frames.append({"file": path.relative_to(_PACKAGE).as_posix(), "line": frame.lineno})
        result["frames"] = frames[-8:]
    return result
