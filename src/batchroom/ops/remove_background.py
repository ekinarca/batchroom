"""Background removal via ONNX salience/segmentation models.

Pre/post-processing mirrors the rembg conventions the bundled models were
distributed with: square resize to the model's input size, per-model
normalization, min-max normalized matte, resized back to source size.
"""

from __future__ import annotations

import numpy as np
from PIL import Image
from pydantic import field_validator

from ..core.errors import ModelError, OpError
from ..core.matting import refine_matte
from ..models.registry import CATALOG, ModelSpec, default_model, get_spec
from .base import ItemState, Op, OpParams, PipelineContext, register


class RemoveBackgroundParams(OpParams):
    model: str = "auto"
    alpha_matting: bool = False  # guided-filter edge refinement against the source image

    @field_validator("model")
    @classmethod
    def _known_model(cls, value: str) -> str:
        if value == "auto":
            return value
        spec = get_spec(value)
        if spec is None or spec.task != "background-removal":
            options = ", ".join(
                sorted(mid for mid, s in CATALOG.items() if s.task == "background-removal")
            )
            raise ValueError(f"unknown background-removal model '{value}' (available: {options})")
        return value


def _resolve_spec(model_id: str) -> ModelSpec:
    if model_id == "auto":
        spec = default_model("background-removal")
        if spec is None:  # pragma: no cover - catalog always has a default
            raise ModelError("no default background-removal model in catalog")
        return spec
    spec = get_spec(model_id)
    if spec is None:
        raise ModelError(f"unknown model '{model_id}'")
    return spec


def _preprocess(rgb: np.ndarray, spec: ModelSpec) -> np.ndarray:
    size = spec.input_size
    pil = Image.fromarray(rgb, mode="RGB").resize((size, size), Image.Resampling.LANCZOS)
    arr = np.asarray(pil, dtype=np.float32)
    if spec.norm == "max":
        peak = float(arr.max())
        arr = arr / peak if peak > 0 else arr
    else:  # "255"
        arr = arr / 255.0
    mean = np.asarray(spec.mean, dtype=np.float32)
    std = np.asarray(spec.std, dtype=np.float32)
    arr = (arr - mean) / std
    return np.expand_dims(arr.transpose(2, 0, 1), 0)  # NCHW


def _postprocess(pred: np.ndarray, width: int, height: int, spec: ModelSpec) -> np.ndarray:
    matte = np.asarray(pred, dtype=np.float32)
    matte = np.squeeze(matte)
    if matte.ndim != 2:
        raise OpError(
            f"unexpected model output shape {pred.shape}", op="remove-background"
        )
    if spec.activation == "sigmoid":
        matte = 1.0 / (1.0 + np.exp(-matte))
    lo, hi = float(matte.min()), float(matte.max())
    if hi > lo:
        matte = (matte - lo) / (hi - lo)
    matte8 = np.rint(np.clip(matte, 0.0, 1.0) * 255.0).astype(np.uint8)
    pil = Image.fromarray(matte8, mode="L").resize((width, height), Image.Resampling.LANCZOS)
    return np.asarray(pil, dtype=np.uint8)


def infer_matte(session, spec: ModelSpec, rgb: np.ndarray) -> np.ndarray:
    """Run the model and return a uint8 matte at the source resolution."""
    h, w = rgb.shape[:2]
    inputs = {session.get_inputs()[0].name: _preprocess(rgb, spec)}
    outputs = session.run(None, inputs)
    return _postprocess(outputs[0], w, h, spec)


@register
class RemoveBackgroundOp(Op):
    name = "remove-background"
    summary = "Remove the background, producing a transparent subject"
    Params = RemoveBackgroundParams

    def models_used(self, params: RemoveBackgroundParams) -> list[str]:
        return [_resolve_spec(params.model).id]

    def apply(
        self, state: ItemState, params: RemoveBackgroundParams, ctx: PipelineContext
    ) -> ItemState:
        if ctx.models is None:
            raise OpError("no model manager configured", op=self.name)
        spec = _resolve_spec(params.model)
        ctx.progress(f"remove-background ({spec.id})")
        # First use downloads the weights (checksum-verified) — README promise.
        session = ctx.models.ensure_session(spec.id, progress_text=ctx.progress)
        ctx.cancel_check()

        img = state.image
        rgb = img.pixels[:, :, :3]
        matte = infer_matte(session, spec, rgb)
        ctx.cancel_check()

        if params.alpha_matting:
            ctx.progress("refining edges")
            matte = refine_matte(rgb, matte)

        out = img.pixels.copy()
        # Respect pre-existing transparency: multiply, don't replace.
        existing = out[:, :, 3].astype(np.uint16)
        out[:, :, 3] = ((existing * matte.astype(np.uint16)) // 255).astype(np.uint8)
        state.image = img.with_pixels(out, f"remove-background {spec.id}")
        return state
