"""Pure geometry & alpha helpers shared by ops.

All functions operate on (H, W, 4) uint8 RGBA numpy arrays and are
deterministic — the golden-image suite pins their behavior.
"""

from __future__ import annotations

import re

import numpy as np
from PIL import Image

from .errors import OpError
from .image_io import _ensure_rgba

NAMED_COLORS = {
    "white": (255, 255, 255, 255),
    "black": (0, 0, 0, 255),
    "gray": (128, 128, 128, 255),
    "grey": (128, 128, 128, 255),
    "transparent": (0, 0, 0, 0),
}

_SIZE_RE = re.compile(r"^\s*(\d+)\s*[xX×]\s*(\d+)\s*$")
_PCT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")
_PX_RE = re.compile(r"^\s*(\d+)\s*(?:px)?\s*$")

# Output geometry limits. A typo like 100000x100000 must fail when the recipe
# is validated, not by exhausting the server's memory mid-batch (an RGBA
# float resample of 240 MP already needs ~4 GB).
MAX_SIDE = 20000
MAX_OUTPUT_PIXELS = 240_000_000


def _checked_size(width, height, raw) -> tuple[int, int]:
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        raise OpError(f"invalid size {raw!r}") from None
    if not (1 <= w <= MAX_SIDE and 1 <= h <= MAX_SIDE):
        raise OpError(f"invalid size {raw!r}: each side must be 1-{MAX_SIDE} px")
    return w, h


def parse_size(value: str | list | tuple | dict) -> tuple[int, int]:
    """Accept '2000x2000', [2000, 2000] or {width, height}."""
    if isinstance(value, str):
        m = _SIZE_RE.match(value)
        if m:
            return _checked_size(m.group(1), m.group(2), value)
        raise OpError(f"invalid size '{value}', expected 'WIDTHxHEIGHT' like '2000x2000'")
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return _checked_size(value[0], value[1], value)
    if isinstance(value, dict) and "width" in value and "height" in value:
        return _checked_size(value["width"], value["height"], value)
    raise OpError(f"invalid size {value!r}")


def check_fixed_size(width: int, height: int) -> None:
    """Recipe validation: a fixed target that every item would fail on."""
    if width * height > MAX_OUTPUT_PIXELS:
        raise ValueError(
            f"size {width}x{height} exceeds the {MAX_OUTPUT_PIXELS // 1_000_000} MP safety limit"
        )


def check_output_size(width: int, height: int, *, op: str) -> None:
    """Refuse results too large to process safely (per item, clean error)."""
    if width * height > MAX_OUTPUT_PIXELS:
        raise OpError(
            f"result {width}x{height} exceeds the "
            f"{MAX_OUTPUT_PIXELS // 1_000_000} MP safety limit",
            op=op,
            hint="use a smaller target size or scale",
        )


def parse_color(value: str) -> tuple[int, int, int, int]:
    """'#FFF', '#FFFFFF', '#FFFFFFCC' or a named color."""
    if not isinstance(value, str):
        raise OpError(f"invalid color {value!r}")
    key = value.strip().lower()
    if key in NAMED_COLORS:
        return NAMED_COLORS[key]
    if key.startswith("#"):
        hexpart = key[1:]
        if len(hexpart) == 3:
            hexpart = "".join(c * 2 for c in hexpart)
        if len(hexpart) == 6:
            hexpart += "ff"
        if len(hexpart) == 8:
            try:
                r, g, b, a = (int(hexpart[i : i + 2], 16) for i in (0, 2, 4, 6))
                return (r, g, b, a)
            except ValueError:
                pass
    raise OpError(f"invalid color '{value}', use '#RRGGBB', '#RRGGBBAA' or white/black/transparent")


def parse_padding(value: str | int | float) -> tuple[float, str]:
    """Return (amount, unit) where unit is 'pct' or 'px'.

    Percentages apply to each side, so they must stay below 50% to leave
    room for the content; pixel padding is capped like any output side."""
    amount: float | None = None
    unit = "px"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        amount = float(value)
    elif isinstance(value, str):
        m = _PCT_RE.match(value)
        if m:
            amount, unit = float(m.group(1)), "pct"
        else:
            m = _PX_RE.match(value)
            if m:
                amount = float(m.group(1))
    if amount is None:
        raise OpError(f"invalid padding '{value}', use e.g. '8%' or '32px'")
    if unit == "pct" and not 0 <= amount < 50:
        raise OpError(f"invalid padding '{value}': percentage must be 0 to under 50%")
    if unit == "px" and not 0 <= amount <= MAX_SIDE:
        raise OpError(f"invalid padding '{value}': pixels must be 0-{MAX_SIDE}")
    return amount, unit


def parse_aspect(value: str) -> tuple[int, int] | None:
    """'1:1', '4:5', '16:9' → ratio tuple; 'auto' → None."""
    if value in ("auto", "source", ""):
        return None
    m = re.match(r"^\s*(\d+)\s*[:/]\s*(\d+)\s*$", str(value))
    if not m:
        raise OpError(f"invalid aspect '{value}', use e.g. '1:1' or '4:5'")
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        raise OpError(f"invalid aspect '{value}'")
    return (w, h)


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------


def resize_rgba(
    pixels: np.ndarray,
    width: int,
    height: int,
    box: tuple[float, float, float, float] | None = None,
) -> np.ndarray:
    """High-quality Lanczos resample in premultiplied float (no halo artifacts).

    ``box`` resamples only that source region (x0, y0, x1, y1; fractional
    edges allowed), so a crop never needs a full-size intermediate."""
    pixels = _ensure_rgba(pixels)
    h, w = pixels.shape[:2]
    if (w, h) == (width, height) and box in (None, (0, 0, w, h)):
        return pixels.copy()
    if width < 1 or height < 1:
        raise OpError(f"invalid target size {width}x{height}")
    # Premultiply once, in float, so transparent-pixel RGB garbage doesn't
    # bleed into edges. Each channel is resampled as a 32-bit float image:
    # handing Pillow an RGBA image would premultiply a second time (light
    # fringes on every cut-out) and round the premultiplied values to 8 bits.
    alpha = pixels[:, :, 3].astype(np.float32)
    opaque = bool((pixels[:, :, 3] == 255).all())

    def channel(plane: np.ndarray) -> np.ndarray:
        img = Image.fromarray(np.ascontiguousarray(plane, dtype=np.float32), "F")
        return np.asarray(img.resize((width, height), Image.Resampling.LANCZOS, box=box))

    result = np.empty((height, width, 4), dtype=np.uint8)
    if opaque:
        for c in range(3):
            result[:, :, c] = np.clip(np.rint(channel(pixels[:, :, c])), 0, 255)
        result[:, :, 3] = 255
        return result
    # Unpremultiply by the unclipped alpha: Lanczos rings both by the same
    # factor, so their ratio stays true to the source colour at hard edges.
    out_alpha = channel(alpha)
    visible = out_alpha >= 0.5  # rounds to alpha >= 1
    safe_alpha = np.where(visible, out_alpha, 1.0)
    scale = alpha / 255.0
    for c in range(3):
        premult = channel(pixels[:, :, c].astype(np.float32) * scale)
        value = np.where(visible, premult * 255.0 / safe_alpha, 0.0)
        result[:, :, c] = np.clip(np.rint(value), 0, 255)
    result[:, :, 3] = np.clip(np.rint(out_alpha), 0, 255)
    return result


def fit_dimensions(
    src_w: int, src_h: int, target_w: int, target_h: int, mode: str
) -> tuple[int, int]:
    """Scaled draw size for contain/cover (before crop/pad)."""
    if mode == "stretch":
        return target_w, target_h
    scale_w = target_w / src_w
    scale_h = target_h / src_h
    scale = min(scale_w, scale_h) if mode in ("contain", "pad") else max(scale_w, scale_h)
    # never upscale implicitly in contain unless needed? Marketplace presets DO
    # expect exact canvases, so we scale both directions deliberately.
    return max(1, round(src_w * scale)), max(1, round(src_h * scale))


def place_on_canvas(
    pixels: np.ndarray,
    canvas_w: int,
    canvas_h: int,
    background: tuple[int, int, int, int],
) -> np.ndarray:
    """Center content on a canvas filled with background (alpha compositing)."""
    pixels = _ensure_rgba(pixels)
    h, w = pixels.shape[:2]
    if w > canvas_w or h > canvas_h:
        raise OpError(f"content {w}x{h} larger than canvas {canvas_w}x{canvas_h}")
    canvas = np.zeros((canvas_h, canvas_w, 4), dtype=np.uint8)
    canvas[:, :] = np.array(background, dtype=np.uint8)
    x0 = (canvas_w - w) // 2
    y0 = (canvas_h - h) // 2
    region = canvas[y0 : y0 + h, x0 : x0 + w]
    canvas[y0 : y0 + h, x0 : x0 + w] = composite_over(pixels, region)
    return canvas


def composite_over(fg: np.ndarray, bg: np.ndarray) -> np.ndarray:
    """Standard 'over' compositing of two straight-alpha RGBA arrays."""
    fa = fg[:, :, 3:4].astype(np.float32) / 255.0
    ba = bg[:, :, 3:4].astype(np.float32) / 255.0
    out_a = fa + ba * (1.0 - fa)
    with np.errstate(divide="ignore", invalid="ignore"):
        out_rgb = np.where(
            out_a > 0,
            (
                fg[:, :, :3].astype(np.float32) * fa
                + bg[:, :, :3].astype(np.float32) * ba * (1.0 - fa)
            )
            / out_a,
            0,
        )
    out = np.empty_like(fg)
    out[:, :, :3] = np.clip(np.rint(out_rgb), 0, 255).astype(np.uint8)
    out[:, :, 3] = np.clip(np.rint(out_a[:, :, 0] * 255.0), 0, 255).astype(np.uint8)
    return out


def crop_center(pixels: np.ndarray, width: int, height: int) -> np.ndarray:
    pixels = _ensure_rgba(pixels)
    h, w = pixels.shape[:2]
    if width > w or height > h:
        raise OpError(f"cannot crop {width}x{height} from {w}x{h}")
    x0 = (w - width) // 2
    y0 = (h - height) // 2
    return pixels[y0 : y0 + height, x0 : x0 + width].copy()


def alpha_bbox(pixels: np.ndarray, threshold: int = 8) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, y0, x1, y1 exclusive) of pixels with alpha > threshold."""
    alpha = _ensure_rgba(pixels)[:, :, 3]
    mask = alpha > threshold
    if not mask.any():
        return None
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def trim_to_alpha(pixels: np.ndarray, threshold: int = 8, margin: int = 0) -> np.ndarray:
    """Crop to the alpha bounding box (plus margin). No-op if fully transparent."""
    bbox = alpha_bbox(pixels, threshold)
    if bbox is None:
        return pixels.copy()
    x0, y0, x1, y1 = bbox
    h, w = pixels.shape[:2]
    x0 = max(0, x0 - margin)
    y0 = max(0, y0 - margin)
    x1 = min(w, x1 + margin)
    y1 = min(h, y1 + margin)
    return pixels[y0:y1, x0:x1].copy()


def resize_to_box(
    pixels: np.ndarray,
    target_w: int,
    target_h: int,
    mode: str = "pad",
    background: tuple[int, int, int, int] = (255, 255, 255, 255),
    padding: tuple[float, str] = (0.0, "px"),
) -> np.ndarray:
    """The workhorse behind resize/preset ops.

    contain: fit within box, result keeps aspect (≤ box)
    pad:     fit within box minus padding, centered on exact-box canvas
    cover:   fill box, center-crop overflow, exact box
    stretch: exact box, aspect ignored
    """
    pixels = _ensure_rgba(pixels)
    h, w = pixels.shape[:2]
    if mode not in ("contain", "pad", "cover", "stretch"):
        raise OpError(f"invalid fit mode '{mode}'")
    if mode == "pad":
        amount, unit = padding
        pad_x = round(target_w * amount / 100.0) if unit == "pct" else round(amount)
        pad_y = round(target_h * amount / 100.0) if unit == "pct" else round(amount)
        inner_w = max(1, target_w - 2 * pad_x)
        inner_h = max(1, target_h - 2 * pad_y)
        draw_w, draw_h = fit_dimensions(w, h, inner_w, inner_h, "contain")
        content = resize_rgba(pixels, draw_w, draw_h)
        return place_on_canvas(content, target_w, target_h, background)
    if mode == "contain":
        draw_w, draw_h = fit_dimensions(w, h, target_w, target_h, "contain")
        return resize_rgba(pixels, draw_w, draw_h)
    if mode == "cover":
        # Crop in source space, then resample straight to the box: scaling the
        # whole image first made a 2000x4 strip a 1,000,000x2000 intermediate.
        scale = max(target_w / w, target_h / h)
        crop_w, crop_h = min(w, target_w / scale), min(h, target_h / scale)
        x0, y0 = (w - crop_w) / 2, (h - crop_h) / 2
        return resize_rgba(pixels, target_w, target_h, box=(x0, y0, x0 + crop_w, y0 + crop_h))
    return resize_rgba(pixels, target_w, target_h)  # stretch
