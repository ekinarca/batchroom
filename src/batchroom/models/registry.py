"""Model catalog.

v1 policy (see plan §7): only permissively-licensed weights ship in this
catalog — Apache-2.0 / MIT / BSD. Non-commercial weights (BRIA RMBG, S-Lab
CodeFormer, ...) are deliberately absent and PRs adding them are rejected
per CONTRIBUTING.md.

Every entry pins a SHA-256. The manager refuses to use a weight file whose
hash does not match, so a compromised mirror cannot silently swap weights.
Licenses are surfaced in the UI and API before download — trust signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelSpec:
    id: str
    task: str  # "background-removal" | "upscale"
    description: str
    url: str
    sha256: str | None  # None = not pinned; download requires explicit opt-in
    size_bytes: int | None
    license: str
    license_url: str
    homepage: str
    # Inference config (background-removal):
    input_size: int = 1024
    mean: tuple[float, float, float] = (0.485, 0.456, 0.406)
    std: tuple[float, float, float] = (1.0, 1.0, 1.0)
    norm: str = "max"  # "max": divide by image max (rembg convention); "255": divide by 255
    activation: str = "none"  # "sigmoid" for models that emit logits (BiRefNet)
    # Inference config (upscale):
    scale: int = 1
    default: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


_RE_MBG_BASE = "https://github.com/danielgatis/rembg/releases/download/v0.0.0"

CATALOG: dict[str, ModelSpec] = {
    spec.id: spec
    for spec in [
        ModelSpec(
            id="isnet-general",
            task="background-removal",
            description="ISNet (DIS) general-use — best quality/speed balance on CPU; the default.",
            url=f"{_RE_MBG_BASE}/isnet-general-use.onnx",
            sha256="60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a",
            size_bytes=178648008,
            license="Apache-2.0",
            license_url="https://github.com/xuebinqin/DIS/blob/main/LICENSE.md",
            homepage="https://github.com/xuebinqin/DIS",
            input_size=1024,
            mean=(0.485, 0.456, 0.406),
            std=(1.0, 1.0, 1.0),
            norm="max",
            default=True,
            tags=("recommended",),
        ),
        ModelSpec(
            id="u2net",
            task="background-removal",
            description="U²-Net full — the classic; heavier than ISNet with similar quality.",
            url=f"{_RE_MBG_BASE}/u2net.onnx",
            sha256="8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491",
            size_bytes=175997641,
            license="Apache-2.0",
            license_url="https://github.com/xuebinqin/U-2-Net/blob/master/LICENSE",
            homepage="https://github.com/xuebinqin/U-2-Net",
            input_size=320,
            mean=(0.485, 0.456, 0.406),
            std=(0.229, 0.224, 0.225),
            norm="max",
        ),
        ModelSpec(
            id="u2netp",
            task="background-removal",
            description="U²-Net small (4.5 MB) — fastest, lower fidelity; good for previews/NAS.",
            url=f"{_RE_MBG_BASE}/u2netp.onnx",
            sha256="309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8",
            size_bytes=4574861,
            license="Apache-2.0",
            license_url="https://github.com/xuebinqin/U-2-Net/blob/master/LICENSE",
            homepage="https://github.com/xuebinqin/U-2-Net",
            input_size=320,
            mean=(0.485, 0.456, 0.406),
            std=(0.229, 0.224, 0.225),
            norm="max",
            tags=("fast",),
        ),
        ModelSpec(
            id="silueta",
            task="background-removal",
            description="Silueta (43 MB) — U²-Net compressed; small download, decent quality.",
            url=f"{_RE_MBG_BASE}/silueta.onnx",
            sha256="75da6c8d2f8096ec743d071951be73b4a8bc7b3e51d9a6625d63644f90ffeedb",
            size_bytes=44173029,
            license="MIT",
            license_url="https://github.com/xuebinqin/U-2-Net/blob/master/LICENSE",
            homepage="https://developers.silueta.me/",
            input_size=320,
            mean=(0.485, 0.456, 0.406),
            std=(0.229, 0.224, 0.225),
            norm="max",
            tags=("small",),
        ),
        ModelSpec(
            id="birefnet-general-lite",
            task="background-removal",
            description="BiRefNet lite — near-SOTA edges; large download, slower on CPU.",
            url=f"{_RE_MBG_BASE}/BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
            sha256="5600024376f572a557870a5eb0afb1e5961636bef4e1e22132025467d0f03333",
            size_bytes=224005088,
            license="MIT",
            license_url="https://github.com/ZhengPeng7/BiRefNet/blob/main/LICENSE",
            homepage="https://github.com/ZhengPeng7/BiRefNet",
            input_size=1024,
            mean=(0.485, 0.456, 0.406),
            std=(0.229, 0.224, 0.225),
            norm="255",
            activation="sigmoid",
            tags=("quality",),
        ),
        ModelSpec(
            id="realesrgan-x4",
            task="upscale",
            description=(
                "Real-ESRGAN x4plus — photographic 4x upscale. Converted to ONNX from the "
                "official BSD-3 weights by scripts/convert_realesrgan_onnx.py (reproducible). "
                "Slow on CPU; queue it."
            ),
            # Batchroom-hosted artifact built by the conversion script above.
            url="https://github.com/ekinarca/batchroom/releases/download/models-v1/realesrgan-x4plus.onnx",
            sha256="0bc07a359c7e5f878cb0310096c61db43e6be189aa0b17ea2a50dad946bad2b2",
            size_bytes=67051622,
            license="BSD-3-Clause",
            license_url="https://github.com/xinntao/Real-ESRGAN/blob/master/LICENSE",
            homepage="https://github.com/xinntao/Real-ESRGAN",
            scale=4,
        ),
    ]
}


def get_spec(model_id: str) -> ModelSpec | None:
    return CATALOG.get(model_id)


def default_model(task: str) -> ModelSpec | None:
    for spec in CATALOG.values():
        if spec.task == task and spec.default:
            return spec
    return None


def catalog_for_task(task: str) -> list[ModelSpec]:
    return [s for s in CATALOG.values() if s.task == task]
