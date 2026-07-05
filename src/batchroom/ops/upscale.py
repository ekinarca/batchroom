"""Upscaling: ESRGAN-class ONNX models with tiled inference, or Lanczos.

CPU honesty (plan §8.2): 4x ESRGAN on CPU is slow — tens of seconds per
image. The op reports per-tile progress so the queue UI can show honest
ETAs, and ``model: lanczos`` is always available as the instant path.
Alpha channels are upscaled separately with Lanczos, the standard practice.
"""

from __future__ import annotations

import numpy as np
from pydantic import Field, field_validator

from ..core import imaging
from ..core.errors import ModelError, OpError
from ..models.registry import CATALOG, get_spec
from .base import ItemState, Op, OpParams, PipelineContext, register

_MAX_OUTPUT_PIXELS = 240_000_000  # ~240MP result guard


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
    out = np.zeros((h * model_scale, w * model_scale, 3), dtype=np.float32)
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
            ] = sr[oy0 : oy0 + out_h, ox0 : ox0 + out_w]
            done += 1
            ctx.progress(f"upscale tile {done}/{total}")
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
            if ctx.models is not None and ctx.models.is_available("realesrgan-x4"):
                model_id = "realesrgan-x4"
            else:
                raise ModelError(
                    "no upscale model downloaded",
                    hint="run: batchroom models pull realesrgan-x4 — or set 'model: lanczos'",
                )

        if model_id == "lanczos":
            out = imaging.resize_rgba(img.pixels, target_w, target_h)
            state.image = img.with_pixels(out, f"upscale x{params.scale} lanczos")
            return state

        if ctx.models is None:
            raise OpError("no model manager configured", op=self.name)
        spec = get_spec(model_id)
        if spec is None:  # pragma: no cover - validator prevents
            raise ModelError(f"unknown model '{model_id}'")
        session = ctx.models.session(spec.id)
        ctx.progress(f"upscale ({spec.id})")

        rgb01 = img.pixels[:, :, :3].astype(np.float32) / 255.0
        sr = _tiled_sr(session, rgb01, spec.scale, params.tile, ctx)
        sr8 = np.rint(sr * 255.0).astype(np.uint8)

        # model_scale may exceed the requested scale (x4 model, scale: 2) — downsample.
        if sr8.shape[1] != target_w or sr8.shape[0] != target_h:
            tmp = np.dstack([sr8, np.full(sr8.shape[:2], 255, np.uint8)])
            sr8 = imaging.resize_rgba(tmp, target_w, target_h)[:, :, :3]

        alpha = img.pixels[:, :, 3]
        if (alpha != 255).any():
            alpha_rgba = np.dstack([alpha, alpha, alpha, np.full_like(alpha, 255)])
            alpha_up = imaging.resize_rgba(alpha_rgba, target_w, target_h)[:, :, 0]
        else:
            alpha_up = np.full((target_h, target_w), 255, np.uint8)

        out = np.dstack([sr8, alpha_up])
        state.image = img.with_pixels(out, f"upscale x{params.scale} {spec.id}")
        return state
