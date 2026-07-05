"""Shared fixtures: temp settings, deterministic images, a stub ONNX model.

The stub model ("stub-matte") is a tiny hand-authored ONNX graph that maps
pixel brightness to alpha — deterministic, milliseconds-fast, and exercises
the exact same pre/post-processing path as the real weights, so the whole
remove-background pipeline is testable offline in CI. Its single source of
truth lives in tests/golden/gen.py (shared with the golden suite).
"""

from __future__ import annotations

import pytest

from batchroom.config import Settings
from batchroom.core.image_io import save_image
from batchroom.models import registry
from batchroom.models.manager import ModelManager

from .golden.gen import (  # noqa: F401  (re-exported for test modules)
    STUB_MODEL_ID,
    STUB_SPEC,
    alpha_ring_image,
    bright_disc_image,
    build_stub_onnx,
    gradient_image,
)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings(data_dir=tmp_path / "data", workers=2, watch_enabled=False)
    s.watch_poll_seconds = 0.05
    s.watch_stable_seconds = 0.05
    s.ensure_dirs()
    return s


@pytest.fixture()
def stub_model(settings) -> str:
    """Register the stub in the catalog and place its weight in the cache."""
    registry.CATALOG[STUB_MODEL_ID] = STUB_SPEC
    (settings.models_dir / f"{STUB_MODEL_ID}.onnx").write_bytes(build_stub_onnx())
    yield STUB_MODEL_ID
    registry.CATALOG.pop(STUB_MODEL_ID, None)


@pytest.fixture()
def models(settings) -> ModelManager:
    return ModelManager(settings.models_dir)


# ---------------------------------------------------------------------------
# sample files (image builders live in tests/golden/gen.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_jpg(tmp_path):
    path = tmp_path / "inputs" / "photo.jpg"
    save_image(gradient_image(), path, "jpg")
    return path


@pytest.fixture()
def sample_disc_png(tmp_path):
    path = tmp_path / "inputs" / "disc.png"
    save_image(bright_disc_image(), path, "png")
    return path
