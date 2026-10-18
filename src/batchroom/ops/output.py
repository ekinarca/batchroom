"""Output-planning ops: convert, rename, strip-metadata, preset-pack.

These do not touch pixels (except preset-pack's per-preset fitting); they
record intent that the pipeline honors when materializing files, so image
data is encoded exactly once.
"""

from __future__ import annotations

import re

from pydantic import Field, model_validator

from ..core import imaging
from ..core.errors import RecipeError
from ..core.image_io import ALPHA_FORMATS, format_available, normalize_format
from .base import ItemState, Op, OpParams, PipelineContext, PlannedOutput, register

# Vars usable in rename patterns; validated at recipe load.
RENAME_VARS = {"original", "preset", "index", "recipe", "date", "width", "height"}
_VAR_RE = re.compile(r"\{([a-z_]+)\}")
# Windows-unsafe + path characters removed from final filenames.
_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Filesystems cap a name at 255 bytes; leave room for the extension, a
# "-N" collision suffix and the ".part" of the atomic write.
_MAX_NAME_BYTES = 200


def validate_pattern(pattern: str) -> str:
    names = set(_VAR_RE.findall(pattern))
    unknown = names - RENAME_VARS
    if unknown:
        raise ValueError(
            f"unknown rename variable(s): {', '.join(sorted(unknown))} "
            f"(available: {', '.join(sorted(RENAME_VARS))})"
        )
    if not pattern.strip():
        raise ValueError("pattern is empty")
    return pattern


def render_pattern(pattern: str, values: dict[str, str]) -> str:
    def sub(match: re.Match) -> str:
        return values.get(match.group(1), "")

    name = _VAR_RE.sub(sub, pattern).strip()
    name = _UNSAFE_RE.sub("-", name)
    # collapse artifacts like "photo--" when {preset} is empty
    name = re.sub(r"-{2,}", "-", name).strip("-. ")
    if len(name.encode()) > _MAX_NAME_BYTES:
        # cut on a character boundary, never mid-way through a UTF-8 sequence
        name = name.encode()[:_MAX_NAME_BYTES].decode(errors="ignore").rstrip("-. ")
    return name or "output"


class ConvertParams(OpParams):
    format: str
    quality: int = Field(default=90, ge=1, le=100)
    lossless: bool = False
    keep_metadata: bool = False

    @model_validator(mode="after")
    def _check(self):
        normalize_format(self.format)
        return self


@register
class ConvertOp(Op):
    name = "convert"
    summary = "Set the output format/quality for this item"
    Params = ConvertParams

    def apply(self, state: ItemState, params: ConvertParams, ctx: PipelineContext) -> ItemState:
        state.default_format = normalize_format(params.format)
        state.default_quality = params.quality
        state.default_lossless = params.lossless
        state.keep_metadata = params.keep_metadata
        return state


class RenameParams(OpParams):
    pattern: str

    @model_validator(mode="after")
    def _check(self):
        validate_pattern(self.pattern)
        return self


@register
class RenameOp(Op):
    name = "rename"
    summary = "Set the output filename pattern"
    Params = RenameParams

    def apply(self, state: ItemState, params: RenameParams, ctx: PipelineContext) -> ItemState:
        state.rename_pattern = params.pattern
        return state


class StripMetadataParams(OpParams):
    exif: bool = True
    icc: bool = False


@register
class StripMetadataOp(Op):
    name = "strip-metadata"
    summary = "Drop EXIF (and optionally ICC) from outputs"
    Params = StripMetadataParams

    def apply(
        self, state: ItemState, params: StripMetadataParams, ctx: PipelineContext
    ) -> ItemState:
        img = state.image
        if params.exif:
            img.exif = None
            state.keep_metadata = False
        if params.icc:
            img.icc_profile = None
        img.step_log.append("strip-metadata")
        return state


class PresetSpec(OpParams):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    size: str | list | dict
    format: str = "jpg"
    quality: int = Field(default=90, ge=1, le=100)
    lossless: bool = False
    fit: str = Field(default="pad", pattern="^(contain|pad|cover|stretch)$")
    background: str | None = None  # None -> white for opaque formats, transparent otherwise
    padding: str | int | float = 0

    @model_validator(mode="after")
    def _check(self):
        imaging.parse_size(self.size)
        fmt = normalize_format(self.format)
        if self.background is not None:
            color = imaging.parse_color(self.background)
            if color[3] < 255 and fmt not in ALPHA_FORMATS:
                raise ValueError(
                    f"preset '{self.name}': transparent background needs an alpha format "
                    f"(png/webp/avif), not {fmt}"
                )
        imaging.parse_padding(self.padding)
        return self

    def resolved_background(self) -> tuple[int, int, int, int]:
        if self.background is not None:
            return imaging.parse_color(self.background)
        return (0, 0, 0, 0) if normalize_format(self.format) in ALPHA_FORMATS else (
            255,
            255,
            255,
            255,
        )


class PresetPackParams(OpParams):
    presets: list[PresetSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self):
        names = [p.name for p in self.presets]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate preset name(s): {', '.join(sorted(dupes))}")
        return self


@register
class PresetPackOp(Op):
    name = "preset-pack"
    summary = "Produce one output per preset (size + format + naming)"
    Params = PresetPackParams

    def apply(self, state: ItemState, params: PresetPackParams, ctx: PipelineContext) -> ItemState:
        if state.planned:
            raise RecipeError(
                "only one preset-pack step is allowed per recipe",
                recipe=ctx.recipe_name,
            )
        for preset in params.presets:
            ctx.cancel_check()
            tw, th = imaging.parse_size(preset.size)
            imaging.check_output_size(tw, th, op=self.name)
            background = preset.resolved_background()
            fitted = imaging.resize_to_box(
                state.image.pixels,
                tw,
                th,
                mode=preset.fit,
                background=background,
                padding=imaging.parse_padding(preset.padding),
            )
            state.planned.append(
                PlannedOutput(
                    image=state.image.with_pixels(fitted, f"preset {preset.name}"),
                    preset=preset.name,
                    fmt=normalize_format(preset.format),
                    quality=preset.quality,
                    lossless=preset.lossless,
                    background=background,
                )
            )
        return state


def check_formats_available(params: PresetPackParams) -> list[str]:
    """Formats referenced by presets that this build cannot encode."""
    missing = []
    for preset in params.presets:
        fmt = normalize_format(preset.format)
        if not format_available(fmt):
            missing.append(fmt)
    return sorted(set(missing))
