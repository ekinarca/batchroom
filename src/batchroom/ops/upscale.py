"""Upscaling: ESRGAN-class ONNX models with tiled inference, or Lanczos.

CPU honesty (plan §8.2): 4x ESRGAN on CPU is slow — tens of seconds per
image. The op reports per-tile progress so the queue UI can show honest
ETAs, and ``model: lanczos`` is always available as the instant path.
Alpha channels are upscaled separately with Lanczos, the standard practice;
the colour hidden under transparent pixels is replaced by the visible
colour around it first, so it can't bleed onto the edges.
"""

from __future__ import annotations

import numpy as np
from PIL import Image
from pydantic import Field, field_validator

from ..core import imaging
from ..core.errors import ModelError, OpError
from ..core.matting import _box_sum
from ..models.registry import CATALOG, get_spec
from .base import ItemState, Op, OpParams, PipelineContext, register

_MAX_OUTPUT_PIXELS = imaging.MAX_OUTPUT_PIXELS  # ~240MP result guard


class UpscaleParams(OpParams):
    scale: int = Field(default=4, description="output scale factor")
    model: str = "auto"  # auto | lanczos | a catalog upscale model id
    tile: int = Field(default=256, ge=64, le=1024)

    @field_validator("scale")
    @classmethod
    def _scale_ok(cls, value: int) -> int:
        if value not in (2, 4):
            raise ValueError("scale must be 2 or 4")
        return value

    @field_validator("model")
    @classmethod
    def _model_ok(cls, value: str) -> str:
        if value in ("auto", "lanczos"):
            return value
        spec = get_spec(value)
        if spec is None or spec.task != "upscale":
            options = ", ".join(
                ["lanczos"] + sorted(mid for mid, s in CATALOG.items() if s.task == "upscale")
            )
            raise ValueError(f"unknown upscale model '{value}' (available: {options})")
        return value


def _tiled_sr(session, rgb01: np.ndarray, model_scale: int, tile: int, ctx: PipelineContext):
    """Run super-resolution tile by tile with overlap to hide seams."""
    h, w = rgb01.shape[:2]
    pad = 16
    # uint8 straight away: a float32 result plus its x255 copy needed ~37 bytes
    # per output pixel, ~8 GiB at the size limit
    out = np.zeros((h * model_scale, w * model_scale, 3), dtype=np.uint8)
    input_name = session.get_inputs()[0].name
    xs = list(range(0, w, tile))
    ys = list(range(0, h, tile))
    total = len(xs) * len(ys)
    done = 0
    for y0 in ys:
        for x0 in xs:
            ctx.cancel_check()
            x1, y1 = min(x0 + tile, w), min(y0 + tile, h)
            px0, py0 = max(0, x0 - pad), max(0, y0 - pad)
            px1, py1 = min(w, x1 + pad), min(h, y1 + pad)
            patch = rgb01[py0:py1, px0:px1]
            nchw = np.expand_dims(patch.transpose(2, 0, 1), 0).astype(np.float32)
            sr = session.run(None, {input_name: nchw})[0]
            sr = np.clip(sr[0].transpose(1, 2, 0), 0.0, 1.0)
            # cut the padding off in output coordinates
            ox0 = (x0 - px0) * model_scale
            oy0 = (y0 - py0) * model_scale
            out_h = (y1 - y0) * model_scale
            out_w = (x1 - x0) * model_scale
            out[
                y0 * model_scale : y0 * model_scale + out_h,
                x0 * model_scale : x0 * model_scale + out_w,
            ] = np.rint(sr[oy0 : oy0 + out_h, ox0 : ox0 + out_w] * 255.0)
            done += 1
            ctx.progress(f"upscale tile {done}/{total}")
    return out


def _bleed_under_alpha(pixels: np.ndarray, radius: int = 8) -> np.ndarray:
    """RGB in [0, 1] for super-resolution, with the colour under (semi-)transparent
    pixels pulled toward the visible colours around them.

    A cut-out still holds the removed background's colour where it is
    transparent; the model and the downsample spread it onto visible edges as
    a coloured fringe. Mixing in the alpha-weighted neighbourhood colour by
    ``1 - alpha`` keeps opaque pixels exact and weighs the rest the way
    premultiplied compositing would."""
    weight = pixels[:, :, 3].astype(np.float64) / 255.0
    norm = _box_sum(weight, radius)
    covered = norm > 1e-6
    total = float(weight.sum())
    out = np.empty(pixels.shape[:2] + (3,), dtype=np.float32)
    for c in range(3):
        plane = pixels[:, :, c].astype(np.float64) / 255.0
        bleed = _box_sum(plane * weight, radius)
        # far from anything visible: the image's mean visible colour
        fallback = float((plane * weight).sum()) / total if total > 0 else 0.0
        np.divide(bleed, norm, out=bleed, where=covered)
        bleed[~covered] = fallback
        out[:, :, c] = plane * weight + bleed * (1.0 - weight)
    return out


@register
class UpscaleOp(Op):
    name = "upscale"
    summary = "2x/4x upscale (ESRGAN-class ONNX or Lanczos)"
    Params = UpscaleParams

    def models_used(self, params: UpscaleParams) -> list[str]:
        if params.model == "lanczos":
            return []
        if params.model == "auto":
            return ["realesrgan-x4"]
        return [params.model]

    def apply(self, state: ItemState, params: UpscaleParams, ctx: PipelineContext) -> ItemState:
        img = state.image
        h, w = img.height, img.width
        target_w, target_h = w * params.scale, h * params.scale
        if target_w * target_h > _MAX_OUTPUT_PIXELS:
            raise OpError(
                f"upscale result {target_w}x{target_h} exceeds the safety limit",
                op=self.name,
                hint="resize down first or lower the scale",
            )

        model_id = params.model
        if model_id == "auto":
            model_id = "realesrgan-x4"

        if model_id == "lanczos":
            out = imaging.resize_rgba(img.pixels, target_w, target_h)
            state.image = img.with_pixels(out, f"upscale x{params.scale} lanczos")
            return state

        if ctx.models is None:
            raise OpError("no model manager configured", op=self.name)
        spec = get_spec(model_id)
        if spec is None:  # pragma: no cover - validator prevents
            raise ModelError(f"unknown model '{model_id}'")
        # First use downloads the weights (checksum-verified) — README promise.
        # scale: 2 on an x4 model still runs (and holds) the full x4 result
        model_w, model_h = w * spec.scale, h * spec.scale
        if model_w * model_h > _MAX_OUTPUT_PIXELS:
            raise OpError(
                f"upscale via {spec.id} needs a {model_w}x{model_h} intermediate,"
                " which exceeds the safety limit",
                op=self.name,
                hint="resize down first, or use model: lanczos",
            )
        session = ctx.models.ensure_session(spec.id, progress_text=ctx.progress)
        ctx.progress(f"upscale ({spec.id})")

        alpha = img.pixels[:, :, 3]
        has_alpha = bool((alpha != 255).any())
        if has_alpha:
            rgb01 = _bleed_under_alpha(img.pixels)
        else:
            rgb01 = img.pixels[:, :, :3].astype(np.float32) / 255.0
        sr8 = _tiled_sr(session, rgb01, spec.scale, params.tile, ctx)
        del rgb01

        # model_scale may exceed the requested scale (x4 model, scale: 2) — downsample.
        # Opaque RGB: Pillow's 8-bit resample is exact here and needs no float copies.
        if sr8.shape[1] != target_w or sr8.shape[0] != target_h:
            sr8 = np.asarray(
                Image.fromarray(sr8, "RGB").resize(
                    (target_w, target_h), Image.Resampling.LANCZOS
                )
            )

        if has_alpha:
            alpha_up = np.asarray(
                Image.fromarray(alpha, "L").resize((target_w, target_h), Image.Resampling.LANCZOS)
            )
        else:
            alpha_up = np.full((target_h, target_w), 255, np.uint8)

        out = np.dstack([sr8, alpha_up])
        state.image = img.with_pixels(out, f"upscale x{params.scale} {spec.id}")
        return state
