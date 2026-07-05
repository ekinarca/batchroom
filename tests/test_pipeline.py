import json

from batchroom.core.image_io import save_image
from batchroom.core.manifest import build_manifest, utc_now_iso, write_manifest
from batchroom.core.pipeline import JobNames, process_item
from batchroom.core.recipe import load_recipe_text
from batchroom.ops.base import PipelineContext

from .conftest import bright_disc_image, gradient_image

SIMPLE = """
name: simple-pack
steps:
  - op: preset-pack
    presets:
      - { name: big, size: 100x100, format: jpg }
      - { name: small, size: 40x40, format: png, background: transparent }
"""

CONVERT_ONLY = """
name: convert-only
steps:
  - op: convert
    format: webp
    quality: 80
output: { structure: flat }
"""


def ctx_for(settings, models=None) -> PipelineContext:
    return PipelineContext(recipe_name="t", job_id="j-test", date="20260705", models=models)


class TestProcessItem:
    def test_preset_outputs_and_structure(self, tmp_path, settings):
        recipe = load_recipe_text(SIMPLE)
        src = tmp_path / "in" / "photo.jpg"
        save_image(gradient_image(), src, "jpg")
        out_dir = tmp_path / "out"
        result = process_item(src, recipe, out_dir, ctx_for(settings), names=JobNames())
        assert result.status == "done"
        paths = [o.path for o in result.outputs]
        assert paths == ["big/photo.jpg", "small/photo.png"]
        for o in result.outputs:
            f = out_dir / o.path
            assert f.is_file()
            assert f.stat().st_size == o.bytes

    def test_flat_structure_and_convert(self, tmp_path, settings):
        recipe = load_recipe_text(CONVERT_ONLY)
        src = tmp_path / "photo.jpg"
        save_image(gradient_image(), src, "jpg")
        out_dir = tmp_path / "out"
        result = process_item(src, recipe, out_dir, ctx_for(settings), names=JobNames())
        assert result.status == "done"
        assert result.outputs[0].path == "photo.webp"
        assert result.outputs[0].format == "webp"

    def test_failure_isolated_not_raised(self, tmp_path, settings):
        recipe = load_recipe_text(SIMPLE)
        bad = tmp_path / "corrupt.jpg"
        bad.write_bytes(b"garbage")
        result = process_item(bad, recipe, tmp_path / "out", ctx_for(settings), names=JobNames())
        assert result.status == "failed"
        assert "corrupt.jpg" in (result.error or "")

    def test_source_never_modified(self, tmp_path, settings):
        recipe = load_recipe_text(SIMPLE)
        src = tmp_path / "keep.png"
        save_image(bright_disc_image(), src, "png")
        before = src.read_bytes()
        process_item(src, recipe, tmp_path / "out", ctx_for(settings), names=JobNames())
        assert src.read_bytes() == before

    def test_thumbs_written(self, tmp_path, settings):
        recipe = load_recipe_text(SIMPLE)
        src = tmp_path / "photo.jpg"
        save_image(gradient_image(), src, "jpg")
        thumbs = tmp_path / "thumbs"
        process_item(
            src, recipe, tmp_path / "out", ctx_for(settings),
            index=3, names=JobNames(), thumbs_dir=thumbs,
        )
        assert (thumbs / "00003-in.webp").is_file()
        assert (thumbs / "00003-out.webp").is_file()

    def test_collision_between_items(self, tmp_path, settings):
        recipe = load_recipe_text(SIMPLE)
        names = JobNames()
        a = tmp_path / "a" / "photo.jpg"
        b = tmp_path / "b" / "photo.jpg"
        save_image(gradient_image(), a, "jpg")
        save_image(gradient_image(), b, "jpg")
        r1 = process_item(a, recipe, tmp_path / "out", ctx_for(settings), index=0, names=names)
        r2 = process_item(b, recipe, tmp_path / "out", ctx_for(settings), index=1, names=names)
        assert r1.outputs[0].path == "big/photo.jpg"
        assert r2.outputs[0].path == "big/photo-2.jpg"


class TestManifest:
    def test_build_and_write(self, tmp_path, settings):
        recipe = load_recipe_text(SIMPLE)
        src = tmp_path / "photo.jpg"
        save_image(gradient_image(), src, "jpg")
        result = process_item(src, recipe, tmp_path / "out", ctx_for(settings), names=JobNames())
        manifest = build_manifest(
            job_id="j-1", recipe_name=recipe.name, recipe_version=1,
            output_structure=recipe.output.structure,
            started_at=utc_now_iso(), finished_at=None, results=[result],
        )
        path = write_manifest(tmp_path / "out", manifest)
        data = json.loads(path.read_text())
        assert data["counts"] == {"total": 1, "done": 1, "failed": 0, "cancelled": 0}
        assert data["items"][0]["outputs"][0]["sha256"]
        assert data["manifest_version"] == 1
