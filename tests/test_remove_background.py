"""remove-background + upscale ops against the stub ONNX model."""

import numpy as np
import pytest

from batchroom.core.errors import ModelError
from batchroom.core.matting import box_mean, guided_filter
from batchroom.ops.base import ItemState, PipelineContext, get_op

from .conftest import bright_disc_image, gradient_image


@pytest.fixture()
def ctx(settings, models, stub_model):
    return PipelineContext(recipe_name="t", job_id="t", date="20260705", models=models)


class TestRemoveBackground:
    def test_disc_cutout(self, ctx, stub_model):
        op = get_op("remove-background")
        params = op.validate_params({"model": stub_model})
        state = ItemState(image=bright_disc_image(200, 160), original_stem="disc")
        state = op.apply(state, params, ctx)
        px = state.image.pixels
        h, w = px.shape[:2]
        assert px[h // 2, w // 2, 3] > 200  # bright disc kept
        assert px[4, 4, 3] < 40  # dark field removed

    def test_alpha_matting_path(self, ctx, stub_model):
        op = get_op("remove-background")
        params = op.validate_params({"model": stub_model, "alpha_matting": True})
        state = ItemState(image=bright_disc_image(200, 160), original_stem="disc")
        state = op.apply(state, params, ctx)
        px = state.image.pixels
        assert px[80, 100, 3] > 200
        assert px[4, 4, 3] < 40

    def test_existing_alpha_multiplied(self, ctx, stub_model):
        img = bright_disc_image(120, 120)
        img.pixels[:, :60, 3] = 0  # left half pre-masked
        op = get_op("remove-background")
        params = op.validate_params({"model": stub_model})
        state = op.apply(ItemState(image=img, original_stem="x"), params, ctx)
        assert state.image.pixels[60, 30, 3] == 0  # stays transparent

    def test_unknown_model_rejected_at_validation(self):
        op = get_op("remove-background")
        with pytest.raises(Exception, match="unknown background-removal model"):
            op.validate_params({"model": "not-a-model"})

    def test_missing_weight_gives_hint(self, ctx, stub_model, settings):
        (settings.models_dir / f"{stub_model}.onnx").unlink()
        op = get_op("remove-background")
        params = op.validate_params({"model": stub_model})
        with pytest.raises(ModelError, match="not downloaded"):
            op.apply(ItemState(image=bright_disc_image(), original_stem="x"), params, ctx)


class TestUpscale:
    def test_lanczos_scale2(self, ctx):
        op = get_op("upscale")
        params = op.validate_params({"scale": 2, "model": "lanczos"})
        state = op.apply(ItemState(image=gradient_image(50, 40), original_stem="x"), params, ctx)
        assert (state.image.width, state.image.height) == (100, 80)

    def test_auto_without_model_hints_pull(self, ctx):
        op = get_op("upscale")
        params = op.validate_params({"scale": 4})
        with pytest.raises(ModelError, match="no upscale model"):
            op.apply(ItemState(image=gradient_image(10, 10), original_stem="x"), params, ctx)

    def test_scale_validation(self):
        op = get_op("upscale")
        with pytest.raises(Exception, match="scale"):
            op.validate_params({"scale": 3})

    def test_output_size_guard(self, ctx):
        op = get_op("upscale")
        params = op.validate_params({"scale": 4, "model": "lanczos"})
        big = ItemState(image=gradient_image(5000, 4000), original_stem="x")
        with pytest.raises(Exception, match="safety limit"):
            op.apply(big, params, ctx)


class TestMattingMath:
    def test_box_mean_matches_bruteforce(self):
        rng = np.random.default_rng(7)
        x = rng.random((17, 13))
        r = 3
        got = box_mean(x, r)
        h, w = x.shape
        for yy in (0, 5, 16):
            for xx in (0, 6, 12):
                y0, y1 = max(0, yy - r), min(h, yy + r + 1)
                x0, x1 = max(0, xx - r), min(w, xx + r + 1)
                want = x[y0:y1, x0:x1].mean()
                assert abs(got[yy, xx] - want) < 1e-9

    def test_guided_filter_snaps_to_edge(self):
        # guide: sharp vertical edge; src: blurry version of the same edge
        w = 64
        guide = np.zeros((w, w))
        guide[:, w // 2 :] = 1.0
        xs = np.arange(w, dtype=np.float64)
        soft = 1.0 / (1.0 + np.exp(-(xs - w / 2) / 6.0))
        src = np.tile(soft, (w, 1))
        out = guided_filter(guide, src, radius=8, eps=1e-4)
        mid = w // 2
        # after filtering, the transition should be steeper around the edge
        assert (out[10, mid + 3] - out[10, mid - 3]) > (src[10, mid + 3] - src[10, mid - 3]) * 1.5
