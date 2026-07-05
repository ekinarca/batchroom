"""Image decode/encode layer.

Internal representation: ``numpy`` array, shape (H, W, 4), dtype uint8,
RGBA with straight (non-premultiplied) alpha. Every op in the pipeline
consumes and produces this shape, which keeps the math explicit and unit
testable. Pillow supplies the codecs; HEIC/AVIF come from pillow-heif when
present.

Privacy default: EXIF is captured on load but only written back when a
recipe explicitly asks for it (``keep_metadata``). ICC profiles are kept by
default because dropping them shifts colors.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .errors import ImageIOError

Image.MAX_IMAGE_PIXELS = 300_000_000  # ~300MP guard against decompression bombs

# ---------------------------------------------------------------------------
# Optional codec plugins
# ---------------------------------------------------------------------------
_HEIF_OK = False
_AVIF_OK = False
try:  # pillow-heif provides HEIC/HEIF and (in most builds) AVIF
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HEIF_OK = True
    try:
        pillow_heif.register_avif_opener()
        _AVIF_OK = True
    except Exception:
        _AVIF_OK = False
except Exception:  # pragma: no cover - depends on wheel availability
    pass

if not _AVIF_OK:  # Pillow >= 11.2 ships a native AVIF plugin in most wheels
    try:
        from PIL import AvifImagePlugin  # noqa: F401

        _AVIF_OK = True
    except Exception:  # pragma: no cover
        pass


# Canonical format ids used across recipes/API. Maps to (pillow format, extension).
FORMATS: dict[str, tuple[str, str]] = {
    "jpg": ("JPEG", ".jpg"),
    "jpeg": ("JPEG", ".jpg"),
    "png": ("PNG", ".png"),
    "webp": ("WEBP", ".webp"),
    "avif": ("AVIF", ".avif"),
    "heic": ("HEIF", ".heic"),
    "tiff": ("TIFF", ".tiff"),
    "bmp": ("BMP", ".bmp"),
}

# Formats that support an alpha channel on save.
ALPHA_FORMATS = {"png", "webp", "avif", "heic", "tiff"}

READ_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tiff",
    ".tif",
    ".gif",
    *([".heic", ".heif"] if _HEIF_OK else []),
    *([".avif"] if _AVIF_OK else []),
}


def normalize_format(name: str) -> str:
    """Return the canonical format id ('jpeg' -> 'jpg') or raise."""
    key = name.strip().lower().lstrip(".")
    if key == "jpeg":
        key = "jpg"
    if key not in FORMATS:
        raise ImageIOError(
            f"unsupported format '{name}'",
            hint=f"supported: {', '.join(sorted(set(FORMATS) - {'jpeg'}))}",
        )
    return key


def format_available(fmt: str) -> bool:
    fmt = normalize_format(fmt)
    if fmt == "avif":
        return _AVIF_OK
    if fmt == "heic":
        return _HEIF_OK
    return True


def extension_for(fmt: str) -> str:
    return FORMATS[normalize_format(fmt)][1]


@dataclass
class ImageData:
    """Decoded image plus the metadata we may carry to outputs."""

    pixels: np.ndarray  # (H, W, 4) uint8 RGBA, straight alpha
    icc_profile: bytes | None = None
    exif: bytes | None = None
    dpi: tuple[float, float] | None = None
    source_format: str = "png"
    source_path: Path | None = None
    step_log: list[str] = field(default_factory=list)

    @property
    def width(self) -> int:
        return int(self.pixels.shape[1])

    @property
    def height(self) -> int:
        return int(self.pixels.shape[0])

    @property
    def has_alpha(self) -> bool:
        return bool((self.pixels[:, :, 3] != 255).any())

    def with_pixels(self, pixels: np.ndarray, note: str | None = None) -> ImageData:
        img = replace(self, pixels=_ensure_rgba(pixels))
        if note:
            img.step_log = [*self.step_log, note]
        return img


def _ensure_rgba(pixels: np.ndarray) -> np.ndarray:
    if pixels.dtype != np.uint8:
        raise ImageIOError(f"internal: expected uint8 pixels, got {pixels.dtype}")
    if pixels.ndim != 3 or pixels.shape[2] != 4:
        raise ImageIOError(f"internal: expected (H, W, 4) RGBA, got shape {pixels.shape}")
    return np.ascontiguousarray(pixels)


def pil_to_rgba_array(pil: Image.Image) -> np.ndarray:
    if pil.mode != "RGBA":
        pil = pil.convert("RGBA")
    return np.asarray(pil, dtype=np.uint8).copy()


def rgba_array_to_pil(pixels: np.ndarray) -> Image.Image:
    return Image.fromarray(_ensure_rgba(pixels), mode="RGBA")


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def _guard_pixel_count(pil: Image.Image, name: str) -> None:
    """Enforce the documented ~300MP ceiling BEFORE decoding pixel data.

    Pillow's own MAX_IMAGE_PIXELS only warns at 1x and raises at 2x; the
    header gives us width/height for free, so we refuse deterministically."""
    if pil.width * pil.height > Image.MAX_IMAGE_PIXELS:
        raise ImageIOError(
            f"image too large to decode safely: {name} "
            f"({pil.width}x{pil.height}, limit ~{Image.MAX_IMAGE_PIXELS // 1_000_000}MP)"
        )


def load_image(path: str | Path) -> ImageData:
    path = Path(path)
    if not path.is_file():
        raise ImageIOError(f"file not found: {path}")
    try:
        with Image.open(path) as pil:
            _guard_pixel_count(pil, path.name)
            pil.load()
            source_format = (pil.format or "png").lower()
            if source_format == "jpeg":
                source_format = "jpg"
            if source_format == "heif":
                source_format = "heic"
            # Respect camera orientation, then drop the tag by re-reading exif below.
            oriented = ImageOps.exif_transpose(pil)
            if oriented is None:  # pragma: no cover - PIL returns image in practice
                oriented = pil
            icc = oriented.info.get("icc_profile") or None
            exif = oriented.info.get("exif") or None
            dpi = oriented.info.get("dpi")
            pixels = pil_to_rgba_array(oriented)
    except ImageIOError:
        raise
    except Image.DecompressionBombError as exc:
        raise ImageIOError(f"image too large to decode safely: {path.name}") from exc
    except Exception as exc:
        raise ImageIOError(
            f"cannot decode '{path.name}': {exc}",
            hint="supported inputs: " + ", ".join(sorted(e.lstrip('.') for e in READ_EXTENSIONS)),
        ) from exc
    return ImageData(
        pixels=pixels,
        icc_profile=icc if isinstance(icc, bytes) else None,
        exif=exif if isinstance(exif, bytes) else None,
        dpi=tuple(dpi) if dpi else None,
        source_format=source_format if source_format in FORMATS else "png",
        source_path=path,
    )


def load_image_bytes(data: bytes, *, name: str = "upload") -> ImageData:
    try:
        with Image.open(io.BytesIO(data)) as pil:
            _guard_pixel_count(pil, name)
            pil.load()
            source_format = (pil.format or "png").lower()
            if source_format == "jpeg":
                source_format = "jpg"
            oriented = ImageOps.exif_transpose(pil) or pil
            icc = oriented.info.get("icc_profile") or None
            exif = oriented.info.get("exif") or None
            pixels = pil_to_rgba_array(oriented)
    except Exception as exc:
        raise ImageIOError(f"cannot decode '{name}': {exc}") from exc
    return ImageData(
        pixels=pixels,
        icc_profile=icc if isinstance(icc, bytes) else None,
        exif=exif if isinstance(exif, bytes) else None,
        source_format=source_format if source_format in FORMATS else "png",
    )


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def flatten_alpha(pixels: np.ndarray, background: tuple[int, int, int]) -> np.ndarray:
    """Composite RGBA onto an opaque background, returning RGBA with alpha=255."""
    pixels = _ensure_rgba(pixels)
    alpha = pixels[:, :, 3:4].astype(np.float32) / 255.0
    rgb = pixels[:, :, :3].astype(np.float32)
    bg = np.array(background, dtype=np.float32).reshape(1, 1, 3)
    out_rgb = rgb * alpha + bg * (1.0 - alpha)
    out = np.empty_like(pixels)
    out[:, :, :3] = np.clip(np.rint(out_rgb), 0, 255).astype(np.uint8)
    out[:, :, 3] = 255
    return out


def encode_image(
    image: ImageData,
    fmt: str,
    *,
    quality: int = 90,
    lossless: bool = False,
    keep_metadata: bool = False,
    background: tuple[int, int, int] = (255, 255, 255),
) -> bytes:
    """Encode to bytes. Alpha is flattened for formats that cannot store it."""
    fmt = normalize_format(fmt)
    if not format_available(fmt):
        raise ImageIOError(
            f"format '{fmt}' encoder not available in this build",
            hint="install pillow-heif for HEIC/AVIF support",
        )
    pil_fmt, _ = FORMATS[fmt]
    pixels = image.pixels
    if fmt not in ALPHA_FORMATS:
        pixels = flatten_alpha(pixels, background)
        pil = Image.fromarray(pixels[:, :, :3], mode="RGB")
    else:
        pil = rgba_array_to_pil(pixels)
        if fmt in {"png", "webp", "avif", "heic", "tiff"} and not image.has_alpha:
            # Opaque image: save as RGB to shave bytes (PNG/WebP treat this well).
            pil = pil.convert("RGB")

    options: dict = {}
    if fmt == "jpg":
        options.update(quality=quality, optimize=True, progressive=True)
    elif fmt == "png":
        options.update(optimize=True)
    elif fmt == "webp":
        options.update(quality=quality, lossless=lossless, method=5)
    elif fmt == "avif" or fmt == "heic":
        options.update(quality=quality)
    elif fmt == "tiff":
        options.update(compression="tiff_deflate")

    if image.icc_profile:
        options["icc_profile"] = image.icc_profile
    if keep_metadata and image.exif and fmt in {"jpg", "webp", "png", "tiff", "avif", "heic"}:
        options["exif"] = image.exif
    if image.dpi:
        options["dpi"] = image.dpi

    buf = io.BytesIO()
    try:
        pil.save(buf, format=pil_fmt, **options)
    except Exception as exc:
        raise ImageIOError(f"cannot encode to {fmt}: {exc}") from exc
    return buf.getvalue()


def save_image(
    image: ImageData,
    path: str | Path,
    fmt: str | None = None,
    **kwargs,
) -> Path:
    """Encode and write atomically (temp file + rename) so watchers never see partial files."""
    path = Path(path)
    fmt = normalize_format(fmt or path.suffix or image.source_format)
    data = encode_image(image, fmt, **kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def make_thumbnail(image: ImageData, max_px: int = 320) -> bytes:
    """Small WebP preview for the UI, on a checker-free white background."""
    pil = rgba_array_to_pil(image.pixels)
    pil.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    pil.save(buf, format="WEBP", quality=80, method=4)
    return buf.getvalue()
