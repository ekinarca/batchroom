import io

import numpy as np
import pytest
from PIL import Image

from batchroom.core import image_io
from batchroom.core.errors import ImageIOError

from .conftest import alpha_ring_image, gradient_image


class TestFormats:
    def test_normalize(self):
        assert image_io.normalize_format("JPEG") == "jpg"
        assert image_io.normalize_format(".png") == "png"
        with pytest.raises(ImageIOError):
            image_io.normalize_format("exr")

    @pytest.mark.parametrize("fmt", ["jpg", "png", "webp"])
    def test_roundtrip_core_formats(self, tmp_path, fmt):
        img = gradient_image(64, 48)
        path = image_io.save_image(img, tmp_path / f"t{image_io.extension_for(fmt)}", fmt)
        back = image_io.load_image(path)
        assert (back.width, back.height) == (64, 48)
        assert back.source_format == fmt

    def test_png_preserves_alpha_exactly(self, tmp_path):
        img = alpha_ring_image(64)
        path = image_io.save_image(img, tmp_path / "ring.png", "png")
        back = image_io.load_image(path)
        assert np.array_equal(back.pixels, img.pixels)

    def test_jpg_flattens_alpha_onto_background(self, tmp_path):
        img = alpha_ring_image(64)
        path = image_io.save_image(img, tmp_path / "ring.jpg", "jpg", background=(0, 255, 0))
        back = image_io.load_image(path)
        assert not back.has_alpha
        corner = back.pixels[2, 2]
        assert corner[1] > 200 and corner[0] < 80  # green shows through

    def test_flatten_alpha_math(self):
        px = np.zeros((1, 1, 4), np.uint8)
        px[0, 0] = (255, 255, 255, 128)
        out = image_io.flatten_alpha(px, (0, 0, 0))
        assert out[0, 0, 3] == 255
        assert 127 <= out[0, 0, 0] <= 129

    def test_exif_orientation_applied(self, tmp_path):
        # 30x10 image saved with orientation=6 (rotate 90 CW on read) -> 10x30
        pil = Image.new("RGB", (30, 10), (200, 10, 10))
        exif = Image.Exif()
        exif[274] = 6
        path = tmp_path / "oriented.jpg"
        pil.save(path, exif=exif)
        img = image_io.load_image(path)
        assert (img.width, img.height) == (10, 30)

    def test_decompression_bomb_refused_at_header(self, tmp_path):
        # Hand-crafted PNG header claiming 20000x15010 (~300.2MP) — the guard
        # must refuse from the header alone, before any pixel decode.
        import struct
        import zlib

        def chunk(kind: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", 20000, 15010, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(b"\x00"))
               + chunk(b"IEND", b""))
        bomb = tmp_path / "bomb.png"
        bomb.write_bytes(png)
        with pytest.raises(ImageIOError, match="too large"):
            image_io.load_image(bomb)

    def test_decode_error_is_friendly(self, tmp_path):
        bad = tmp_path / "bad.jpg"
        bad.write_bytes(b"not an image at all")
        with pytest.raises(ImageIOError) as err:
            image_io.load_image(bad)
        assert "bad.jpg" in str(err.value)

    def test_missing_file(self, tmp_path):
        with pytest.raises(ImageIOError):
            image_io.load_image(tmp_path / "nope.png")

    def test_load_bytes(self):
        img = gradient_image(32, 32)
        data = image_io.encode_image(img, "png")
        back = image_io.load_image_bytes(data, name="mem.png")
        assert back.width == 32

    def test_thumbnail_small(self):
        data = image_io.make_thumbnail(gradient_image(640, 480), 100)
        thumb = Image.open(io.BytesIO(data))
        assert max(thumb.size) <= 100
        assert thumb.format == "WEBP"

    def test_atomic_save_no_partial(self, tmp_path):
        img = gradient_image(32, 32)
        path = image_io.save_image(img, tmp_path / "atomic.png", "png")
        assert path.is_file()
        assert not list(tmp_path.glob("*.part"))
