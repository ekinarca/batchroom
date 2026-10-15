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

    def test_missing_weight_autodownloads(self, ctx, stub_model, settings, monkeypatch):
        """Ops must try to fetch a missing model (README: first use downloads)."""
        (settings.models_dir / f"{stub_model}.onnx").unlink()
        pulled: list[str] = []

        def fake_ensure(self, model_id, *, progress=None, allow_unpinned=False):
            pulled.append(model_id)
            path = self.path_for(model_id)
            from .golden.gen import build_stub_onnx

            path.write_bytes(build_stub_onnx())
            return path

        from batchroom.models.manager import ModelManager

        monkeypatch.setattr(ModelManager, "ensure", fake_ensure)
        op = get_op("remove-background")
        params = op.validate_params({"model": stub_model})
        state = op.apply(ItemState(image=bright_disc_image(), original_stem="x"), params, ctx)
        assert pulled == [stub_model]
        assert state.image.pixels[4, 4, 3] < 40  # and it actually worked

    def test_missing_weight_offline_gives_clear_error(self, ctx, stub_model, settings,
                                                      monkeypatch):
        (settings.models_dir / f"{stub_model}.onnx").unlink()
        from batchroom.models.manager import ModelManager

        def fail_ensure(self, model_id, *, progress=None, allow_unpinned=False):
            raise ModelError("download failed (offline)", hint="check network access")

        monkeypatch.setattr(ModelManager, "ensure", fail_ensure)
        op = get_op("remove-background")
        params = op.validate_params({"model": stub_model})
        with pytest.raises(ModelError, match="download failed"):
            op.apply(ItemState(image=bright_disc_image(), original_stem="x"), params, ctx)


class TestUpscale:
    def test_lanczos_scale2(self, ctx):
        op = get_op("upscale")
        params = op.validate_params({"scale": 2, "model": "lanczos"})
        state = op.apply(ItemState(image=gradient_image(50, 40), original_stem="x"), params, ctx)
        assert (state.image.width, state.image.height) == (100, 80)

    def test_auto_attempts_download_when_missing(self, ctx, monkeypatch):
        """auto -> realesrgan-x4 is fetched on first use (hermetic: stubbed)."""
        from batchroom.models.manager import ModelManager

        def fail_ensure(self, model_id, *, progress=None, allow_unpinned=False):
            assert model_id == "realesrgan-x4"
            raise ModelError("download failed (offline)", hint="check network access")

        monkeypatch.setattr(ModelManager, "ensure", fail_ensure)
        op = get_op("upscale")
        params = op.validate_params({"scale": 4})
        with pytest.raises(ModelError, match="download failed"):
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


class TestSessionCache:
    def test_concurrent_first_use_loads_the_model_once(self, settings, stub_model, monkeypatch):
        import threading
        import time

        import onnxruntime as ort

        from batchroom.models.manager import ModelManager

        created = []
        real = ort.InferenceSession

        def counting(*args, **kwargs):
            created.append(1)
            time.sleep(0.1)  # widen the race window
            return real(*args, **kwargs)

        monkeypatch.setattr(ort, "InferenceSession", counting)
        manager = ModelManager(settings.models_dir)
        sessions = []
        threads = [
            threading.Thread(target=lambda: sessions.append(manager.session(stub_model)))
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(created) == 1  # every worker asking at batch start shares one copy
        assert len({id(s) for s in sessions}) == 1
