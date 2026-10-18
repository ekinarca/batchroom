"""Pipeline execution: run a recipe over one input file, materialize outputs.

Workers call :func:`process_item` once per file — that granularity buys
parallelism inside a job, per-item error isolation and free resume (only
pending items re-run after a restart). Inputs are immutable: sources are
never modified, outputs always land in the job's output directory.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..ops.base import ItemState, PipelineContext, PlannedOutput
from .errors import BatchroomError, CancelledError
from .image_io import (
    ImageData,
    encode_image,
    extension_for,
    load_image,
    make_thumbnail,
)
from .output_naming import plan_relpath
from .recipe import Recipe

__all__ = ["ItemResult", "OutputRecord", "JobNames", "process_item"]


@dataclass
class OutputRecord:
    path: str  # relative to the job output dir, POSIX separators
    preset: str | None
    format: str
    width: int
    height: int
    bytes: int
    sha256: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "preset": self.preset,
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


@dataclass
class ItemResult:
    source: str
    status: str  # done | failed | cancelled
    error: str | None = None
    outputs: list[OutputRecord] = field(default_factory=list)
    duration_ms: int = 0
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "status": self.status,
            "error": self.error,
            "outputs": [o.to_dict() for o in self.outputs],
            "duration_ms": self.duration_ms,
            "steps": self.steps,
        }


class JobNames:
    """Thread-safe reservation of output paths within one job (collision-proof)."""

    def __init__(self, taken: set[str] | None = None):
        self._taken: set[str] = set(taken or ())
        self._lock = threading.Lock()

    def reserve(self, relpath: str) -> str:
        with self._lock:
            candidate = relpath
            if candidate in self._taken:
                stem, dot, ext = relpath.rpartition(".")
                base = stem if dot else relpath
                suffix_ext = f".{ext}" if dot else ""
                n = 2
                while f"{base}-{n}{suffix_ext}" in self._taken:
                    n += 1
                candidate = f"{base}-{n}{suffix_ext}"
            self._taken.add(candidate)
            return candidate


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def _materialize(
    planned: PlannedOutput,
    state: ItemState,
    recipe: Recipe,
    out_dir: Path,
    ctx: PipelineContext,
    names: JobNames,
) -> OutputRecord:
    fmt = planned.fmt
    image = planned.image
    relpath = plan_relpath(
        pattern=state.rename_pattern,
        structure=recipe.output.structure,
        preset=planned.preset,
        ext=extension_for(fmt),
        values={
            "original": state.original_stem,
            "preset": planned.preset or "",
            "index": str(state.index + 1),
            "recipe": ctx.recipe_name,
            "date": ctx.date,
            "width": str(image.width),
            "height": str(image.height),
        },
    )
    relpath = names.reserve(relpath)
    data = encode_image(
        image,
        fmt,
        quality=planned.quality,
        lossless=planned.lossless,
        keep_metadata=state.keep_metadata,
        # a transparent canvas color has no meaningful RGB (#00000000 -> black);
        # opaque-only formats flatten such pixels onto white instead
        background=planned.background[:3] if planned.background[3] == 255 else (255, 255, 255),
    )
    _write_atomic(out_dir / relpath, data)
    return OutputRecord(
        path=relpath,
        preset=planned.preset,
        format=fmt,
        width=image.width,
        height=image.height,
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def process_item(
    source: Path,
    recipe: Recipe,
    out_dir: Path,
    ctx: PipelineContext,
    *,
    index: int = 0,
    names: JobNames | None = None,
    thumbs_dir: Path | None = None,
    preloaded: ImageData | None = None,
) -> ItemResult:
    """Run every recipe step over one file and write its outputs.

    Never raises for per-item problems — returns a failed/cancelled result
    instead, so one broken file can't take down a 500-file batch.
    """
    names = names or JobNames()
    started = time.monotonic()
    source_name = Path(source).name
    result = ItemResult(source=source_name, status="done")
    try:
        ctx.cancel_check()
        image = preloaded if preloaded is not None else load_image(source)
        state = ItemState(
            image=image,
            original_stem=Path(source_name).stem,
            source_path=Path(source),
            index=index,
            default_format=recipe.output.format,
            default_quality=recipe.output.quality,
            default_lossless=recipe.output.lossless,
            keep_metadata=recipe.output.keep_metadata,
        )
        if thumbs_dir is not None:
            # 640px: crisp in the UI's before/after compare (renders up to ~460px)
            _write_atomic(thumbs_dir / f"{index:05d}-in.webp", make_thumbnail(image, 640))

        for step_no, step in enumerate(recipe.steps):
            ctx.cancel_check()
            ctx.progress(f"step {step_no + 1}/{len(recipe.steps)}: {step.op.name}")
            state = step.op.apply(state, step.params, ctx)
            result.steps.append(step.op.name)

        planned = state.planned or [
            PlannedOutput(
                image=state.image,
                preset=None,
                fmt=state.default_format or state.image.source_format,
                quality=state.default_quality,
                lossless=state.default_lossless,
                background=state.background,
            )
        ]
        for output in planned:
            ctx.cancel_check()
            result.outputs.append(_materialize(output, state, recipe, out_dir, ctx, names))

        if thumbs_dir is not None and planned:
            _write_atomic(
                thumbs_dir / f"{index:05d}-out.webp", make_thumbnail(planned[0].image, 640)
            )
    except CancelledError:
        result.status = "cancelled"
        result.error = "cancelled"
    except BatchroomError as exc:
        result.status = "failed"
        result.error = str(exc) + (f" — {exc.hint}" if exc.hint else "")
    except Exception as exc:  # unexpected: keep the batch alive, surface the bug
        result.status = "failed"
        result.error = f"unexpected error: {type(exc).__name__}: {exc}"
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result
