"""Op registry. Importing this package registers all built-in ops."""

from . import geometry, output, remove_background, upscale  # noqa: F401
from .base import (
    ItemState,
    Op,
    OpParams,
    PipelineContext,
    PlannedOutput,
    all_ops,
    get_op,
    register,
)

__all__ = [
    "ItemState",
    "Op",
    "OpParams",
    "PipelineContext",
    "PlannedOutput",
    "all_ops",
    "get_op",
    "register",
]
