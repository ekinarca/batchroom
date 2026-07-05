#!/usr/bin/env python3
"""Golden-image corpus + expected-output generator.

The golden suite is Batchroom's regression net (plan §8.3): deterministic
input images, a fixed set of pipeline cases, committed expected outputs,
pixel-tolerance comparison in CI. A visual regression = OSS trust death.

Regenerate after an INTENTIONAL rendering change, then eyeball the diffs:

    .venv/bin/python tests/golden/gen.py --update

The stub ONNX model here is also the one the unit tests use.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

GOLDEN_DIR = Path(__file__).resolve().parent
CORPUS_DIR = GOLDEN_DIR / "corpus"
EXPECTED_DIR = GOLDEN_DIR / "expected"

sys.path.insert(0, str(GOLDEN_DIR.parent.parent / "src"))

from batchroom.core.image_io import ImageData, load_image, save_image  # noqa: E402
from batchroom.models import registry  # noqa: E402

# ---------------------------------------------------------------------------
# stub ONNX model (single source of truth; conftest imports these)
# ---------------------------------------------------------------------------

STUB_MODEL_ID = "stub-matte"

STUB_SPEC = registry.ModelSpec(
    id=STUB_MODEL_ID,
    task="background-removal",
    description="test stub: brightness -> alpha",
    url="https://invalid.example/stub.onnx",
    sha256=None,
    size_bytes=None,
    license="MIT",
    license_url="https://example.com",
    homepage="https://example.com",
    input_size=64,
    mean=(0.0, 0.0, 0.0),
    std=(1.0, 1.0, 1.0),
    norm="255",
)


def build_stub_onnx() -> bytes:
    """input [1,3,H,W] float32 -> sigmoid((mean_c(x) - 0.45) * 14) as [1,1,H,W]."""
    import onnx
    from onnx import TensorProto, helper

    inp = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, "h", "w"])
    out = helper.make_tensor_value_info("matte", TensorProto.FLOAT, [1, 1, "h", "w"])
    nodes = [
        helper.make_node("ReduceMean", ["input"], ["mean"], axes=[1], keepdims=1),
        helper.make_node("Sub", ["mean", "bias"], ["centered"]),
        helper.make_node("Mul", ["centered", "gain"], ["scaled"]),
        helper.make_node("Sigmoid", ["scaled"], ["matte"]),
    ]
    graph = helper.make_graph(
        nodes, "stub_matte", [inp], [out],
        initializer=[
            helper.make_tensor("bias", TensorProto.FLOAT, [], [0.45]),
            helper.make_tensor("gain", TensorProto.FLOAT, [], [14.0]),
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)], ir_version=8)
    onnx.checker.check_model(model)
    return model.SerializeToString()


# ---------------------------------------------------------------------------
# deterministic corpus
# ---------------------------------------------------------------------------


def gradient_image(w: int = 320, h: int = 240) -> ImageData:
    y, x = np.mgrid[0:h, 0:w]
    px = np.zeros((h, w, 4), np.uint8)
    px[:, :, 0] = (x * 255 // max(1, w - 1)).astype(np.uint8)
    px[:, :, 1] = (y * 255 // max(1, h - 1)).astype(np.uint8)
    px[:, :, 2] = 96
    px[:, :, 3] = 255
    return ImageData(pixels=px)


def bright_disc_image(w: int = 200, h: int = 160) -> ImageData:
    y, x = np.mgrid[0:h, 0:w]
    px = np.zeros((h, w, 4), np.uint8)
    px[:, :, :3] = 25
    r = np.hypot(x - w / 2, y - h / 2)
    px[r < min(w, h) * 0.3, :3] = 230
    px[:, :, 3] = 255
    return ImageData(pixels=px)


def alpha_ring_image(size: int = 128) -> ImageData:
    y, x = np.mgrid[0:size, 0:size]
    r = np.hypot(x - size / 2, y - size / 2)
    px = np.zeros((size, size, 4), np.uint8)
    px[:, :, 0] = 200
    px[:, :, 1] = 60
    px[:, :, 2] = 60
    px[:, :, 3] = np.where((r > size * 0.18) & (r < size * 0.38), 255, 0).astype(np.uint8)
    return ImageData(pixels=px)


def checker_photo_image(w: int = 260, h: int = 180) -> ImageData:
    """Checker + seeded noise: stresses resamplers and JPEG encoding."""
    rng = np.random.default_rng(42)
    y, x = np.mgrid[0:h, 0:w]
    checker = (((x // 20) + (y // 20)) % 2) * 120 + 60
    noise = rng.integers(-18, 19, size=(h, w))
    base = np.clip(checker + noise, 0, 255).astype(np.uint8)
    px = np.zeros((h, w, 4), np.uint8)
    px[:, :, 0] = base
    px[:, :, 1] = np.clip(base + 20, 0, 255)
    px[:, :, 2] = np.clip(255 - base, 0, 255)
    px[:, :, 3] = 255
    return ImageData(pixels=px)


CORPUS = {
    "gradient.png": lambda: gradient_image(),
    "disc.png": lambda: bright_disc_image(),
    "ring.png": lambda: alpha_ring_image(),
    "checker.png": lambda: checker_photo_image(),
}


# ---------------------------------------------------------------------------
# golden cases: (name, recipe_yaml, corpus_file)
# outputs land in expected/<case>/<...>; each case is one recipe run.
# ---------------------------------------------------------------------------

CASES: list[tuple[str, str, str]] = [
    (
        "resize-pad",
        "name: g-resize-pad\nsteps:\n"
        "  - op: resize\n    size: 100x100\n    fit: pad\n    background: '#FFFFFF'\n"
        "    padding: 8%\n  - op: convert\n    format: png\n",
        "gradient.png",
    ),
    (
        "resize-cover",
        "name: g-resize-cover\nsteps:\n"
        "  - op: resize\n    size: 100x100\n    fit: cover\n  - op: convert\n    format: png\n",
        "gradient.png",
    ),
    (
        "composite-square",
        "name: g-composite\nsteps:\n"
        "  - op: composite\n    background: '#FFFFFF'\n    padding: 10%\n    aspect: '1:1'\n"
        "  - op: convert\n    format: png\n",
        "ring.png",
    ),
    (
        "trim",
        "name: g-trim\nsteps:\n  - op: trim\n  - op: convert\n    format: png\n",
        "ring.png",
    ),
    (
        "preset-jpg",
        "name: g-preset\nsteps:\n"
        "  - op: preset-pack\n    presets:\n"
        "      - { name: web, size: 64x64, format: jpg, quality: 90 }\n"
        "      - { name: sq, size: 48x48, format: png, background: '#000000' }\n",
        "checker.png",
    ),
    (
        "stub-bg",
        "name: g-stub-bg\nsteps:\n"
        f"  - op: remove-background\n    model: {STUB_MODEL_ID}\n"
        "  - op: convert\n    format: png\n",
        "disc.png",
    ),
    (
        "stub-bg-matting",
        "name: g-stub-bg-matting\nsteps:\n"
        f"  - op: remove-background\n    model: {STUB_MODEL_ID}\n    alpha_matting: true\n"
        "  - op: composite\n    background: '#FFFFFF'\n    padding: 6%\n"
        "  - op: convert\n    format: png\n",
        "disc.png",
    ),
]


def run_case(case_name: str, recipe_yaml: str, corpus_file: str, out_dir: Path, models_dir: Path):
    from batchroom.core.pipeline import JobNames, process_item
    from batchroom.core.recipe import load_recipe_text
    from batchroom.models.manager import ModelManager
    from batchroom.ops.base import PipelineContext

    registry.CATALOG.setdefault(STUB_MODEL_ID, STUB_SPEC)
    stub_path = models_dir / f"{STUB_MODEL_ID}.onnx"
    if not stub_path.is_file():
        stub_path.parent.mkdir(parents=True, exist_ok=True)
        stub_path.write_bytes(build_stub_onnx())

    recipe = load_recipe_text(recipe_yaml)
    ctx = PipelineContext(
        recipe_name=recipe.name, job_id=f"golden-{case_name}", date="20260101",
        models=ModelManager(models_dir),
    )
    result = process_item(
        CORPUS_DIR / corpus_file, recipe, out_dir, ctx, names=JobNames()
    )
    if result.status != "done":
        raise RuntimeError(f"golden case {case_name} failed: {result.error}")
    return result


def update(write_corpus: bool = True) -> None:
    if write_corpus:
        CORPUS_DIR.mkdir(parents=True, exist_ok=True)
        for name, builder in CORPUS.items():
            save_image(builder(), CORPUS_DIR / name, "png")
        print(f"corpus: {len(CORPUS)} images -> {CORPUS_DIR}")
    import tempfile

    EXPECTED_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        models_dir = Path(tmp) / "models"
        for case_name, recipe_yaml, corpus_file in CASES:
            case_dir = EXPECTED_DIR / case_name
            result = run_case(case_name, recipe_yaml, corpus_file, case_dir, models_dir)
            print(f"expected: {case_name}: {[o.path for o in result.outputs]}")


def compare_images(expected_path: Path, actual_path: Path, *, jpg: bool) -> str | None:
    exp = load_image(expected_path).pixels.astype(np.int16)
    act = load_image(actual_path).pixels.astype(np.int16)
    if exp.shape != act.shape:
        return f"shape {act.shape} != expected {exp.shape}"
    diff = np.abs(exp - act)
    mean = float(diff.mean())
    peak = int(diff.max())
    mean_tol, peak_tol = (3.0, 48) if jpg else (1.5, 32)
    if mean > mean_tol or peak > peak_tol:
        return f"pixel drift: mean {mean:.2f} (tol {mean_tol}), max {peak} (tol {peak_tol})"
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="regenerate corpus + expected")
    args = parser.parse_args()
    if args.update:
        update()
    else:
        parser.print_help()
