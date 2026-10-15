"""CLI behavior that isn't covered through the server."""

from batchroom.cli import main
from batchroom.core.image_io import save_image

from .conftest import gradient_image


def test_config_error_is_a_clean_message(monkeypatch, capsys):
    monkeypatch.setenv("BATCHROOM_PORT", "abc")
    assert main(["recipes", "list"]) == 1
    err = capsys.readouterr().err
    assert "BATCHROOM_PORT must be an integer" in err
    assert "Traceback" not in err


def test_run_never_reingests_its_own_output_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BATCHROOM_DATA_DIR", str(tmp_path / "data"))
    photos = tmp_path / "photos"
    save_image(gradient_image(32, 32), photos / "a.png", "png")
    recipe = tmp_path / "to-png.yaml"
    recipe.write_text(
        "name: to-png\nsteps:\n  - op: convert\n    format: png\noutput: { structure: flat }\n",
        encoding="utf-8",
    )
    out = photos / "out"  # output inside the input folder
    for _ in range(2):  # the second run must not process out/a.png as an input
        assert main(["run", str(recipe), str(photos), "-o", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["a.png", "manifest.json"]
