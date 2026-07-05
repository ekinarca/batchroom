"""Geometry ops: resize, trim, composite."""

from __future__ import annotations

from pydantic import Field, model_validator

from ..core import imaging
from ..core.errors import OpError
from ..core.image_io import ImageData
from .base import ItemState, Op, OpParams, PipelineContext, register


class ResizeParams(OpParams):
    size: str | None = None
    width: int | None = Field(default=None, ge=1, le=20000)
    height: int | None = Field(default=None, ge=1, le=20000)
    scale: float | None = Field(default=None, gt=0, le=16)
    fit: str = Field(default="contain", pattern="^(contain|pad|cover|stretch)$")
    background: str = "transparent"
    padding: str | int | float = 0

    @model_validator(mode="after")
    def _one_target(self):
        given = [v is not None for v in (self.size, self.scale)]
        if self.size is not None and (self.width or self.height):
            raise ValueError("use either 'size' or 'width'/'height', not both")
        if self.scale is not None and (self.size or self.width or self.height):
            raise ValueError("'scale' cannot be combined with size/width/height")
        if not any(given) and self.width is None and self.height is None:
            raise ValueError("resize needs 'size', 'width', 'height' or 'scale'")
        # validate eagerly so recipe validation catches bad values
        if self.size is not None:
            imaging.parse_size(self.size)
        imaging.parse_color(self.background)
        imaging.parse_padding(self.padding)
        return self


@register
class ResizeOp(Op):
    name = "resize"
    summary = "Resize with contain/pad/cover/stretch fitting"
    Params = ResizeParams

    def apply(self, state: ItemState, params: ResizeParams, ctx: PipelineContext) -> ItemState:
        img = state.image
        w, h = img.width, img.height
        if params.scale is not None:
            tw, th = max(1, round(w * params.scale)), max(1, round(h * params.scale))
            state.image = img.with_pixels(
                imaging.resize_rgba(img.pixels, tw, th), f"resize x{params.scale}"
            )
            return state
        if params.size is not None:
            tw, th = imaging.parse_size(params.size)
        elif params.width and params.height:
            tw, th = params.width, params.height
        elif params.width:
            tw, th = params.width, max(1, round(h * params.width / w))
        elif params.height:
            tw, th = max(1, round(w * params.height / h)), params.height
        else:  # pragma: no cover - validator prevents
            raise OpError("no resize target", op=self.name)
        out = imaging.resize_to_box(
            img.pixels,
            tw,
            th,
            mode=params.fit,
            background=imaging.parse_color(params.background),
            padding=imaging.parse_padding(params.padding),
        )
        state.image = img.with_pixels(out, f"resize {tw}x{th} {params.fit}")
        return state


class TrimParams(OpParams):
    threshold: int = Field(default=8, ge=0, le=255)
    margin: int = Field(default=0, ge=0, le=4096)


@register
class TrimOp(Op):
    name = "trim"
    summary = "Crop to the alpha bounding box"
    Params = TrimParams

    def apply(self, state: ItemState, params: TrimParams, ctx: PipelineContext) -> ItemState:
        out = imaging.trim_to_alpha(state.image.pixels, params.threshold, params.margin)
        state.image = state.image.with_pixels(out, "trim")
        return state


class CompositeParams(OpParams):
    background: str = "#FFFFFF"
    padding: str | int | float = 0
    trim: bool = True
    aspect: str = "auto"
    threshold: int = Field(default=8, ge=0, le=255)

    @model_validator(mode="after")
    def _check(self):
        imaging.parse_color(self.background)
        imaging.parse_padding(self.padding)
        imaging.parse_aspect(self.aspect)
        return self


@register
class CompositeOp(Op):
    name = "composite"
    summary = "Place the (trimmed) subject on a background canvas"
    Params = CompositeParams

    def apply(self, state: ItemState, params: CompositeParams, ctx: PipelineContext) -> ItemState:
        img = state.image
        pixels = img.pixels
        if params.trim:
            pixels = imaging.trim_to_alpha(pixels, params.threshold)
        h, w = pixels.shape[:2]

        aspect = imaging.parse_aspect(params.aspect)
        amount, unit = imaging.parse_padding(params.padding)

        if aspect is None:
            # Canvas grows around the content by the padding amount.
            pad_x = round(w * amount / 100.0) if unit == "pct" else round(amount)
            pad_y = round(h * amount / 100.0) if unit == "pct" else round(amount)
            canvas_w, canvas_h = w + 2 * pad_x, h + 2 * pad_y
        else:
            ar_w, ar_h = aspect
            # Smallest canvas with the requested aspect that holds content + padding.
            if unit == "pct":
                # padding is relative to the canvas: content occupies (1 - 2p) of each side
                frac = max(0.0, 1.0 - 2 * amount / 100.0)
                if frac <= 0:
                    raise OpError("padding too large, no room for content", op=self.name)
                need_w, need_h = w / frac, h / frac
            else:
                need_w, need_h = w + 2 * amount, h + 2 * amount
            scale = max(need_w / ar_w, need_h / ar_h)
            canvas_w = max(1, round(ar_w * scale))
            canvas_h = max(1, round(ar_h * scale))

        background = imaging.parse_color(params.background)
        out = imaging.place_on_canvas(pixels, canvas_w, canvas_h, background)
        state.image = ImageData(
            pixels=out,
            icc_profile=img.icc_profile,
            exif=img.exif,
            dpi=img.dpi,
            source_format=img.source_format,
            source_path=img.source_path,
            step_log=[*img.step_log, f"composite {canvas_w}x{canvas_h}"],
        )
        state.background = background
        return state
