"""Batchroom error hierarchy.

Every error carries a human-readable message and an optional ``hint``
telling the user what to do about it. API handlers map these onto HTTP
status codes; the CLI prints message + hint.
"""

from __future__ import annotations


class BatchroomError(Exception):
    """Base class for all expected Batchroom failures."""

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class ConfigError(BatchroomError):
    """Invalid server configuration or environment."""


class RecipeError(BatchroomError):
    """A recipe file is malformed or references unknown ops/params."""

    def __init__(
        self,
        message: str,
        *,
        recipe: str | None = None,
        step: int | None = None,
        hint: str | None = None,
    ):
        prefix = ""
        if recipe:
            prefix += f"recipe '{recipe}'"
        if step is not None:
            prefix += f", step {step + 1}"
        full = f"{prefix}: {message}" if prefix else message
        super().__init__(full, hint=hint)
        self.recipe = recipe
        self.step = step


class ImageIOError(BatchroomError):
    """A file could not be decoded or encoded."""


class OpError(BatchroomError, ValueError):
    """An operation failed while processing an image.

    Also a ValueError so pydantic validators that call parse helpers
    (parse_size, parse_color, ...) surface as clean validation errors.
    """

    def __init__(self, message: str, *, op: str | None = None, hint: str | None = None):
        full = f"[{op}] {message}" if op else message
        super().__init__(full, hint=hint)
        self.op = op


class ModelError(BatchroomError):
    """Model download, verification or inference failure."""


class JobError(BatchroomError):
    """Job queue lifecycle failure (not found, bad state transition...)."""


class CancelledError(BatchroomError):
    """Raised inside workers when a job has been cancelled."""

    def __init__(self) -> None:
        super().__init__("job cancelled")
