import pytest

from batchroom.core.pipeline import JobNames
from batchroom.ops.base import ItemState, PipelineContext, get_op
from batchroom.ops.output import render_pattern, validate_pattern

from .conftest import alpha_ring_image, gradient_image


def make_state(img=None) -> ItemState:
    return ItemState(image=img or gradient_image(120, 80), original_stem="photo")


def apply_op(name: str, params: dict, state: ItemState | None = None) -> ItemState:
    op = get_op(name)
    validated = op.validate_params(params)
    return op.apply(state or make_state(), validated, PipelineContext())


class TestResizeOp:
    def test_size_pad(self):
        state = apply_op("resize", {"size": "60x60", "fit": "pad", "background": "#000000"})
        assert (state.image.width, state.image.height) == (60, 60)

    def test_width_only_proportional(self):
        state = apply_op("resize", {"width": 60})
        assert (state.image.width, state.image.height) == (60, 40)

    def test_scale(self):
        state = apply_op("resize", {"scale": 0.5})
        assert (state.image.width, state.image.height) == (60, 40)

    def test_conflicting_params_rejected(self):
        op = get_op("resize")
        with pytest.raises(Exception, match="size"):
            op.validate_params({"size": "10x10", "width": 5})

    def test_needs_a_target(self):
        op = get_op("resize")
        with pytest.raises(Exception, match="resize needs"):
            op.validate_params({"fit": "pad"})


class TestTrimComposite:
    def test_trim(self):
        state = apply_op("trim", {}, make_state(alpha_ring_image(128)))
        assert state.image.width < 128

    def test_composite_square_padding(self):
        state = make_state(alpha_ring_image(128))
        state = apply_op(
            "composite",
            {"background": "#FF0000", "padding": "10%", "aspect": "1:1"},
            state,
        )
        img = state.image
        assert img.width == img.height
        assert tuple(img.pixels[1, 1, :3]) == (255, 0, 0)  # background corner
        assert not img.has_alpha  # opaque background

    def test_composite_transparent_keeps_alpha(self):
        state = apply_op(
            "composite", {"background": "transparent", "padding": 10},
            make_state(alpha_ring_image(64)),
        )
        assert state.image.has_alpha


class TestOutputPlanning:
    def test_convert_sets_defaults(self):
        state = apply_op("convert", {"format": "webp", "quality": 70, "lossless": True})
        assert state.default_format == "webp"
        assert state.default_quality == 70
        assert state.default_lossless is True

    def test_rename_sets_pattern(self):
        state = apply_op("rename", {"pattern": "{original}-{preset}-{index}"})
        assert state.rename_pattern == "{original}-{preset}-{index}"

    def test_strip_metadata(self):
        img = gradient_image(10, 10)
        img.exif = b"Exif\x00\x00fake"
        img.icc_profile = b"fakeicc"
        state = apply_op("strip-metadata", {}, make_state(img))
        assert state.image.exif is None
        assert state.image.icc_profile == b"fakeicc"
        state = apply_op("strip-metadata", {"icc": True}, state)
        assert state.image.icc_profile is None

    def test_preset_pack_fan_out(self):
        state = apply_op(
            "preset-pack",
            {"presets": [
                {"name": "big", "size": "100x100", "format": "jpg", "quality": 92},
                {"name": "small", "size": "50x50", "format": "png", "background": "transparent"},
            ]},
        )
        assert [p.preset for p in state.planned] == ["big", "small"]
        assert state.planned[0].image.width == 100
        assert state.planned[0].fmt == "jpg"
        assert state.planned[1].background == (0, 0, 0, 0)


class TestRenamePatterns:
    def test_render_basic(self):
        out = render_pattern("{original}-{preset}", {"original": "img", "preset": "web"})
        assert out == "img-web"

    def test_render_missing_var_collapses(self):
        out = render_pattern("{original}-{preset}", {"original": "img", "preset": ""})
        assert out == "img"

    def test_render_sanitizes(self):
        out = render_pattern("{original}", {"original": 'a<b>:"/\\|?*c'})
        assert "/" not in out and "\\" not in out and ":" not in out

    def test_render_never_empty(self):
        assert render_pattern("{preset}", {"preset": ""}) == "output"

    def test_validate_unknown_var(self):
        with pytest.raises(ValueError, match="unknown rename"):
            validate_pattern("{nope}")


class TestJobNames:
    def test_collision_suffixes(self):
        names = JobNames()
        assert names.reserve("a/x.jpg") == "a/x.jpg"
        assert names.reserve("a/x.jpg") == "a/x-2.jpg"
        assert names.reserve("a/x.jpg") == "a/x-3.jpg"
        assert names.reserve("noext") == "noext"
        assert names.reserve("noext") == "noext-2"

    def test_preseeded(self):
        names = JobNames({"x.png"})
        assert names.reserve("x.png") == "x-2.png"
