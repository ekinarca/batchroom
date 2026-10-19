"""Regression tests for core / ops / recipe bug fixes."""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from batchroom.core.errors import RecipeError
from batchroom.core.image_io import load_image, load_image_bytes, save_image
from batchroom.core.pipeline import JobNames, process_item
from batchroom.core.recipe import BUILTIN_RECIPES_DIR, load_recipe_file, load_recipe_text
from batchroom.ops.base import PipelineContext

from .conftest import alpha_ring_image


def _ctx() -> PipelineContext:
    return PipelineContext(recipe_name="t", job_id="j-fix", date="20260101")


def _fake_icc(space: bytes) -> bytes:
    return b"\0" * 16 + space + b"\0" * 108


class TestDecode:
    def test_16bit_grayscale_is_scaled_not_clipped(self, tmp_path):
        ramp = np.linspace(0, 65535, 256, dtype=np.float64).round().astype(np.uint16)
        path = tmp_path / "scan16.png"
        Image.fromarray(np.tile(ramp, (4, 1))).save(path)
        assert Image.open(path).mode == "I;16"
        row = load_image(path).pixels[0, :, 0].astype(int)
        # previously everything above 255/65535 clipped to white
        assert row[0] == 0 and row[-1] == 255
        assert abs(row[128] - 128) <= 1
        assert np.all(np.diff(row) >= 0)

    def test_16bit_tiff_bytes_loader(self, tmp_path):
        buf = io.BytesIO()
        Image.fromarray(np.full((4, 4), 32896, np.uint16)).save(buf, format="TIFF")
        px = load_image_bytes(buf.getvalue()).pixels
        assert px[0, 0, 0] == 128 and px[0, 0, 3] == 255

    def test_mpo_jpeg_keeps_jpg_source_format(self, tmp_path):
        path = tmp_path / "camera.jpg"
        first, second = Image.new("RGB", (8, 8), "red"), Image.new("RGB", (8, 8), "blue")
        first.save(path, format="MPO", save_all=True, append_images=[second])
        assert Image.open(path).format == "MPO"
        assert load_image(path).source_format == "jpg"

    def test_mpo_keep_source_format_outputs_jpg(self, tmp_path):
        src = tmp_path / "in" / "camera.jpg"
        src.parent.mkdir()
        a, b = Image.new("RGB", (8, 8), "red"), Image.new("RGB", (8, 8), "blue")
        a.save(src, format="MPO", save_all=True, append_images=[b])
        recipe = load_recipe_text("name: keep-src\nsteps:\n  - op: trim\noutput: {structure: flat}\n")
        result = process_item(src, recipe, tmp_path / "out", _ctx(), names=JobNames())
        assert result.status == "done", result.error
        assert result.outputs[0].path == "camera.jpg"
        assert result.outputs[0].format == "jpg"

    def test_cmyk_icc_profile_dropped(self, tmp_path):
        path = tmp_path / "print.jpg"
        Image.new("CMYK", (8, 8), (0, 255, 255, 0)).save(path, icc_profile=_fake_icc(b"CMYK"))
        img = load_image(path)
        assert img.icc_profile is None  # pixels are RGB now; a CMYK tag would mislabel them

    def test_gray_icc_profile_dropped(self, tmp_path):
        path = tmp_path / "gray.png"
        Image.new("L", (8, 8), 128).save(path, icc_profile=_fake_icc(b"GRAY"))
        assert load_image(path).icc_profile is None

    def test_rgb_icc_profile_kept(self, tmp_path):
        path = tmp_path / "rgb.png"
        icc = _fake_icc(b"RGB ")
        Image.new("RGB", (8, 8), "red").save(path, icc_profile=icc)
        assert load_image(path).icc_profile == icc


class TestRecipeValidation:
    BASE = "name: r-fix\nsteps:\n  - op: trim\n"

    def test_unsupported_output_format_is_recipe_error(self):
        with pytest.raises(RecipeError, match="output.format"):
            load_recipe_text(self.BASE + "output: {format: gif}\n")

    def test_non_integer_version_is_recipe_error(self):
        with pytest.raises(RecipeError, match="version"):
            load_recipe_text(self.BASE + "version: v2\n")
        assert load_recipe_text(self.BASE + "version: '3'\n").version == 3

    def test_bool_quality_rejected(self):
        with pytest.raises(RecipeError, match="quality"):
            load_recipe_text(self.BASE + "output: {quality: yes}\n")

    def test_null_description_is_empty(self):
        assert load_recipe_text(self.BASE + "description:\n").description == ""

    def test_second_preset_pack_fails_at_load(self):
        text = (
            "name: two-packs\nsteps:\n"
            "  - op: preset-pack\n    presets: [{name: a, size: 10x10}]\n"
            "  - op: preset-pack\n    presets: [{name: b, size: 10x10}]\n"
        )
        with pytest.raises(RecipeError, match="only one preset-pack"):
            load_recipe_text(text)

    def test_bad_user_recipe_does_not_break_discovery(self, tmp_path):
        from batchroom.core.recipe import discover_recipes

        (tmp_path / "bad.yaml").write_text(self.BASE + "output: {format: gif}\n")
        (tmp_path / "bad2.yaml").write_text("name: r-fix2\nversion: x\nsteps:\n  - op: trim\n")
        recipes, problems = discover_recipes([BUILTIN_RECIPES_DIR, tmp_path])
        assert "ecom-packshot" in recipes and len(problems) == 2


class TestFlattenTransparentCanvas:
    def test_transparent_composite_to_jpg_flattens_white(self, tmp_path):
        src = tmp_path / "ring.png"
        save_image(alpha_ring_image(), src, "png")
        recipe = load_recipe_text(
            "name: t-jpg\nsteps:\n"
            "  - op: composite\n    background: transparent\n    padding: 10%\n"
            "  - op: convert\n    format: jpg\noutput: {structure: flat}\n"
        )
        result = process_item(src, recipe, tmp_path / "out", _ctx(), names=JobNames())
        assert result.status == "done", result.error
        px = load_image(tmp_path / "out" / result.outputs[0].path).pixels
        assert px[0, 0, :3].min() > 240  # was black (#00000000's RGB)


class TestEcomPackshot:
    def test_transparent_variant_keeps_alpha(self, tmp_path):
        recipe = load_recipe_file(BUILTIN_RECIPES_DIR / "ecom-packshot.yaml", builtin=True)
        recipe.steps = recipe.steps[1:]  # skip the model; the ring already has alpha
        src = tmp_path / "ring.png"
        save_image(alpha_ring_image(), src, "png")
        result = process_item(src, recipe, tmp_path / "out", _ctx(), names=JobNames())
        assert result.status == "done", result.error
        by_preset = {o.preset: load_image(tmp_path / "out" / o.path).pixels for o in result.outputs}
        assert by_preset["transparent-1500"][0, 0, 3] == 0
        assert by_preset["transparent-1500"][750, 750, 3] == 0  # ring hole
        for name in ("web-2048", "web-1024"):
            corner = by_preset[name][0, 0]
            assert corner[3] == 255 and corner[:3].min() > 245, name


def test_long_original_names_fit_the_filesystem(tmp_path):
    from batchroom.core.output_naming import plan_relpath

    original = "ü" * 200  # 400 UTF-8 bytes, beyond any filesystem's 255-byte name limit
    rel = plan_relpath(pattern=None, structure="flat", preset="web-2048", ext=".jpg",
                       values={"original": original, "preset": "web-2048"})
    assert len(rel.encode()) <= 210
    rel.encode().decode()  # still valid UTF-8
    (tmp_path / (rel + ".part")).write_bytes(b"x")  # the atomic write's temp name fits too


class TestResampling:
    def test_transparent_edges_keep_their_colour(self):
        # alpha used to be premultiplied twice: light fringes on every cut-out
        from batchroom.core.imaging import resize_rgba

        yy, xx = np.mgrid[:200, :200]
        dist = np.hypot(yy - 100, xx - 100)
        img = np.zeros((200, 200, 4), np.uint8)
        img[:, :, :3] = 60
        img[:, :, 3] = np.clip((70 - dist) * 128 + 128, 0, 255).astype(np.uint8)
        img[img[:, :, 3] == 0, :3] = 255  # garbage under fully transparent pixels
        for size in (66, 500):
            out = resize_rgba(img, size, size)
            visible = out[:, :, 3] > 0
            assert out[visible][:, :3].min() == 60 and out[visible][:, :3].max() == 60

    def test_cover_never_builds_a_huge_intermediate(self, monkeypatch):
        from batchroom.core import imaging

        seen = []
        real = imaging.Image.Image.resize

        def spy(self, size, *args, **kwargs):
            seen.append(size)
            return real(self, size, *args, **kwargs)

        monkeypatch.setattr(imaging.Image.Image, "resize", spy)
        strip = np.full((4, 2000, 4), 200, np.uint8)  # used to scale to 1,000,000x2000
        out = imaging.resize_to_box(strip, 400, 400, mode="cover")
        assert out.shape == (400, 400, 4)
        assert max(w * h for w, h in seen) == 400 * 400

    def test_cover_matches_crop_of_scaled_image(self):
        from batchroom.core.imaging import resize_to_box

        img = np.zeros((100, 200, 4), np.uint8)
        img[:, :100, 0] = 255  # left half red, right half black
        img[:, :, 3] = 255
        out = resize_to_box(img, 50, 50, mode="cover")  # keeps the middle 100x100
        assert out[25, 5, 0] == 255 and out[25, 44, 0] == 0


class TestRecipeSemantics:
    def test_convert_keeps_recipe_level_quality_and_metadata(self):
        from batchroom.ops.base import ItemState

        recipe = load_recipe_text(
            "name: qual\nsteps:\n  - op: convert\n    format: jpg\n"
            "output:\n  quality: 30\n  keep_metadata: true\n"
        )
        step = recipe.steps[0]
        state = ItemState(image=None, original_stem="x", index=0)  # type: ignore[arg-type]
        state.default_quality, state.keep_metadata = 30, True
        step.op.apply(state, step.params, _ctx())
        assert state.default_quality == 30 and state.keep_metadata is True

    @pytest.mark.parametrize("op", ["resize\n    size: 50x50", "convert\n    format: png"])
    def test_ignored_steps_after_preset_pack_are_rejected(self, op):
        text = (
            "name: late\nsteps:\n  - op: preset-pack\n    presets:\n"
            "      - { name: web, size: 200x200, format: png }\n"
            f"  - op: {op}\n"
        )
        with pytest.raises(RecipeError, match="after preset-pack"):
            load_recipe_text(text)

    def test_strip_metadata_after_preset_pack_reaches_the_outputs(self, tmp_path):
        recipe = load_recipe_text(
            "name: late\nsteps:\n  - op: preset-pack\n    presets:\n"
            "      - { name: web, size: 20x20, format: png }\n"
            "  - op: strip-metadata\n    icc: true\n"
        )
        src = tmp_path / "a.png"
        Image.new("RGB", (32, 32), "red").save(src, icc_profile=_fake_icc(b"RGB "))
        result = process_item(src, recipe, tmp_path / "out", _ctx(), names=JobNames())
        assert result.status == "done", result.error
        out = Image.open(tmp_path / "out" / result.outputs[0].path)
        assert not out.info.get("icc_profile")

    def test_webp_preset_beyond_libwebp_limit_fails_validation(self):
        with pytest.raises(RecipeError, match="16383"):
            load_recipe_text(
                "name: wide\nsteps:\n  - op: preset-pack\n    presets:\n"
                "      - { name: banner, size: 17000x1000, format: webp }\n"
            )

    @pytest.mark.parametrize("value", ['"false"', "no-quotes-string", "1"])
    def test_output_flags_must_be_booleans(self, value):
        with pytest.raises(RecipeError, match="keep_metadata must be true or false"):
            load_recipe_text(
                "name: bools\nsteps:\n  - op: convert\n    format: jpg\n"
                f"output:\n  keep_metadata: {value}\n"
            )


class TestDpi:
    def test_zero_resolution_tiff_still_encodes(self, tmp_path):
        from PIL import TiffImagePlugin

        src = tmp_path / "zero.tif"
        info = TiffImagePlugin.ImageFileDirectory_v2()
        info[282] = info[283] = TiffImagePlugin.IFDRational(0, 0)  # reads as nan dpi
        info[296] = 2
        Image.new("RGB", (8, 8), "blue").save(src, tiffinfo=info)
        img = load_image(src)
        assert img.dpi is None
        for fmt in ("jpg", "png", "tiff"):  # all failed with "cannot convert float NaN"
            save_image(img, tmp_path / f"out.{fmt}", fmt)

    def test_dpi_axes_follow_a_quarter_turn(self, tmp_path):
        src = tmp_path / "rot.jpg"
        exif = Image.Exif()
        exif[0x0112] = 6
        Image.new("RGB", (40, 20), "white").save(src, dpi=(300, 72), exif=exif)
        img = load_image(src)
        assert (img.width, img.height) == (20, 40)
        assert img.dpi == (72, 300)
