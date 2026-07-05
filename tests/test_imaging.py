import numpy as np
import pytest

from batchroom.core import imaging
from batchroom.core.errors import OpError


class TestParsers:
    def test_size(self):
        assert imaging.parse_size("2000x2000") == (2000, 2000)
        assert imaging.parse_size("570X456") == (570, 456)
        assert imaging.parse_size([100, 50]) == (100, 50)
        assert imaging.parse_size({"width": 8, "height": 9}) == (8, 9)
        with pytest.raises(OpError):
            imaging.parse_size("2000")
        with pytest.raises(OpError):
            imaging.parse_size("axb")

    def test_color(self):
        assert imaging.parse_color("#FFFFFF") == (255, 255, 255, 255)
        assert imaging.parse_color("#fff") == (255, 255, 255, 255)
        assert imaging.parse_color("#00000080") == (0, 0, 0, 128)
        assert imaging.parse_color("transparent") == (0, 0, 0, 0)
        assert imaging.parse_color("white") == (255, 255, 255, 255)
        with pytest.raises(OpError):
            imaging.parse_color("#GGHHII")
        with pytest.raises(OpError):
            imaging.parse_color("bleu")

    def test_padding(self):
        assert imaging.parse_padding("8%") == (8.0, "pct")
        assert imaging.parse_padding("32px") == (32.0, "px")
        assert imaging.parse_padding(16) == (16.0, "px")
        with pytest.raises(OpError):
            imaging.parse_padding("large")

    def test_aspect(self):
        assert imaging.parse_aspect("1:1") == (1, 1)
        assert imaging.parse_aspect("16/9") == (16, 9)
        assert imaging.parse_aspect("auto") is None
        with pytest.raises(OpError):
            imaging.parse_aspect("wide")


def _solid(w, h, rgba=(10, 20, 30, 255)):
    px = np.zeros((h, w, 4), np.uint8)
    px[:, :] = rgba
    return px


class TestResizeToBox:
    def test_pad_exact_canvas(self):
        out = imaging.resize_to_box(_solid(200, 100), 100, 100, "pad", (255, 0, 0, 255))
        assert out.shape == (100, 100, 4)
        # letterbox bands must be background
        assert tuple(out[0, 50]) == (255, 0, 0, 255)
        assert tuple(out[50, 50, :3]) == (10, 20, 30)

    def test_pad_padding_pct(self):
        out = imaging.resize_to_box(
            _solid(100, 100), 100, 100, "pad", (0, 0, 0, 255), padding=(10.0, "pct")
        )
        # content is inset by 10% per side
        assert tuple(out[5, 5]) == (0, 0, 0, 255)
        assert tuple(out[50, 50, :3]) == (10, 20, 30)

    def test_contain_keeps_aspect(self):
        out = imaging.resize_to_box(_solid(200, 100), 100, 100, "contain")
        assert out.shape == (50, 100, 4)

    def test_cover_fills_and_crops(self):
        out = imaging.resize_to_box(_solid(200, 100), 100, 100, "cover")
        assert out.shape == (100, 100, 4)

    def test_stretch(self):
        out = imaging.resize_to_box(_solid(200, 100), 60, 90, "stretch")
        assert out.shape == (90, 60, 4)

    def test_invalid_mode(self):
        with pytest.raises(OpError):
            imaging.resize_to_box(_solid(10, 10), 5, 5, "zoom")


class TestAlpha:
    def test_bbox_and_trim(self):
        px = _solid(100, 80, (5, 5, 5, 0))
        px[20:60, 30:70, 3] = 255
        assert imaging.alpha_bbox(px) == (30, 20, 70, 60)
        trimmed = imaging.trim_to_alpha(px)
        assert trimmed.shape == (40, 40, 4)
        with_margin = imaging.trim_to_alpha(px, margin=5)
        assert with_margin.shape == (50, 50, 4)

    def test_bbox_none_when_transparent(self):
        assert imaging.alpha_bbox(_solid(10, 10, (0, 0, 0, 0))) is None
        # trim on fully transparent = no-op
        assert imaging.trim_to_alpha(_solid(10, 10, (0, 0, 0, 0))).shape == (10, 10, 4)

    def test_composite_over_math(self):
        fg = _solid(1, 1, (255, 0, 0, 128))
        bg = _solid(1, 1, (0, 0, 255, 255))
        out = imaging.composite_over(fg, bg)
        assert out[0, 0, 3] == 255
        assert 126 <= out[0, 0, 0] <= 130  # ~50% red
        assert 125 <= out[0, 0, 2] <= 129  # ~50% blue

    def test_place_on_canvas_transparent_bg(self):
        content = _solid(10, 10, (200, 0, 0, 255))
        out = imaging.place_on_canvas(content, 30, 30, (0, 0, 0, 0))
        assert out.shape == (30, 30, 4)
        assert out[0, 0, 3] == 0
        assert tuple(out[15, 15]) == (200, 0, 0, 255)

    def test_resize_rgba_premultiplied_no_halo(self):
        # transparent pixels carry garbage RGB; downscale must not bleed it in
        px = _solid(100, 100, (0, 255, 0, 0))  # transparent green garbage
        px[40:60, 40:60] = (255, 0, 0, 255)  # opaque red square
        out = imaging.resize_rgba(px, 50, 50)
        center = out[25, 25]
        assert center[3] == 255
        assert center[0] > 200 and center[1] < 40  # stays red, no green halo
