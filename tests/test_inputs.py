"""Input discovery and upload-name rules shared by API, CLI and watch folders."""

import pytest

from batchroom.core.inputs import safe_upload_name, walk_files


class TestSafeUploadName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # non-Latin names used to lose their stem *and* extension ("jpg"),
            # so the file was skipped as unsupported
            ("商品.jpg", "商品.jpg"),
            ("сумка.jpg", "сумка.jpg"),
            ("çanta kırmızı.jpg", "çanta kırmızı.jpg"),
            ("منتج.png", "منتج.png"),
            ("u\u0308ber.png", "über.png"),  # macOS NFD -> NFC
            ("50% off #1.png", "50% off #1.png"),
            ("..\\..\\etc\\passwd", "passwd"),
            ("C:\\Users\\me\\photo.JPG", "photo.JPG"),
            ("a<b>c?.png", "a-b-c.png"),
            ("evil\u202egnp.exe", "evil-gnp.exe"),  # RTL override spoofing
            (".hidden.png", "hidden.png"),
            (".jpg", "upload-0.jpg"),
            ("", "upload-0"),
            ("...", "upload-0"),
        ],
    )
    def test_names(self, raw, expected):
        assert safe_upload_name(raw, "upload-0") == expected

    def test_long_names_keep_extension(self):
        name = safe_upload_name("ü" * 300 + ".jpeg", "x")
        assert name.endswith(".jpeg") and len(name.encode("utf-8")) <= 200


class TestWalkFiles:
    def test_prunes_hidden_nas_and_excluded_dirs(self, tmp_path):
        for rel in [
            "a.jpg",
            ".DS_Store",
            "._a.jpg",
            "sub/b.png",
            "sub/@eaDir/b.png/SYNOPHOTO_THUMB_XL.jpg",
            "#recycle/old.jpg",
            ".Trash-1000/files/gone.jpg",
            "$RECYCLE.BIN/x.jpg",
            "out/result.png",
            "@work-notes.jpg",  # an @-file is fine; only @-folders are system ones
        ]:
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        found = [p.relative_to(tmp_path).as_posix()
                 for p in walk_files(tmp_path, exclude_dirs=[tmp_path / "out"])]
        assert found == ["@work-notes.jpg", "a.jpg", "sub/b.png"]
