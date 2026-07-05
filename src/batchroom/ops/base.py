"""Op registry and the state that flows through a pipeline.

An *op* is one step in a recipe. Ops are stateless singletons: they declare
a pydantic ``Params`` model (strict — unknown keys are rejected so recipe
typos surface at validation time, not mid-batch) and an ``apply`` function
that transforms an :class:`ItemState`.

Transform ops replace ``state.image``. Planning ops (convert, rename,
preset-pack) only set output intent; encoding happens once, at materialize
time, so intermediate steps never lose quality to re-encoding.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from ..core.errors import RecipeError
from ..core.image_io import ImageData

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from ..models.manager import ModelManager


class OpParams(BaseModel):
    """Base for all op parameter models. Unknown keys are an error."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@dataclass
class PipelineContext:
    """Job-scoped services and callbacks available to every op."""

    recipe_name: str = ""
    job_id: str = ""
    date: str = ""  # YYYYMMDD at job start, for {date} in rename patterns
    settings: Settings | None = None
    models: ModelManager | None = None
    cancel_check: Callable[[], None] = lambda: None
    progress: Callable[[str], None] = lambda message: None


@dataclass
class PlannedOutput:
    """One file the pipeline intends to write for the current item."""

    image: ImageData
    preset: str | None  # preset name, None for the single-output path
    fmt: str
    quality: int = 90
    lossless: bool = False
    background: tuple[int, int, int, int] = (255, 255, 255, 255)


@dataclass
class ItemState:
    """Everything known about one input file as it moves through the steps."""

    image: ImageData
    original_stem: str
    source_path: Path | None = None
    index: int = 0  # position within the batch, for {index}

    # Output intent accumulated by planning ops:
    rename_pattern: str | None = None
    default_format: str | None = None
    default_quality: int = 90
    default_lossless: bool = False
    keep_metadata: bool = False
    background: tuple[int, int, int, int] = (255, 255, 255, 255)
    planned: list[PlannedOutput] = field(default_factory=list)


class Op:
    """Base class. Subclasses set ``name``, ``Params`` and implement ``apply``."""

    name: ClassVar[str] = ""
    summary: ClassVar[str] = ""
    Params: ClassVar[type[OpParams]] = OpParams

    def validate_params(self, raw: dict, *, recipe: str | None = None, step: int | None = None):
        try:
            return self.Params.model_validate(raw)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'params'}: {e['msg']}"
                for e in exc.errors()
            )
            raise RecipeError(
                f"invalid params for op '{self.name}' — {details}",
                recipe=recipe,
                step=step,
                hint=f"see docs for '{self.name}' parameters",
            ) from exc

    def models_used(self, params) -> list[str]:
        """Model ids this step needs (for prefetch + UI warnings)."""
        return []

    def apply(self, state: ItemState, params, ctx: PipelineContext) -> ItemState:
        raise NotImplementedError


_REGISTRY: dict[str, Op] = {}


def register(op_cls: type[Op]) -> type[Op]:
    op = op_cls()
    if not op.name:
        raise ValueError(f"{op_cls.__name__} has no name")
    if op.name in _REGISTRY:
        raise ValueError(f"duplicate op '{op.name}'")
    _REGISTRY[op.name] = op
    return op_cls


def get_op(name: str) -> Op:
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY))
        raise RecipeError(f"unknown op '{name}'", hint=f"available ops: {known}")
    return _REGISTRY[name]


def all_ops() -> dict[str, Op]:
    return dict(_REGISTRY)
