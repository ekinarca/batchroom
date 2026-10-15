import pytest

from batchroom.core.errors import RecipeError
from batchroom.core.recipe import BUILTIN_RECIPES_DIR, discover_recipes, load_recipe_text

VALID = """
name: my-pack
description: test
input: { formats: [jpg, png] }
steps:
  - op: remove-background
    model: u2netp
    alpha_matting: true
  - op: composite
    background: "#FFFFFF"
    padding: 8%
  - op: preset-pack
    presets:
      - { name: a, size: 100x100, format: jpg }
      - { name: b, size: 50x40, format: png, background: transparent }
output: { structure: per-preset-folder, manifest: true }
"""


class TestParsing:
    def test_valid_recipe(self):
        recipe = load_recipe_text(VALID)
        assert recipe.name == "my-pack"
        assert [s.op.name for s in recipe.steps] == [
            "remove-background", "composite", "preset-pack",
        ]
        assert recipe.models_used() == ["u2netp"]
        assert recipe.accepts_extension(".JPG")
        assert recipe.accepts_extension("jpeg")
        assert not recipe.accepts_extension("webp")

    def test_missing_name(self):
        with pytest.raises(RecipeError, match="name"):
            load_recipe_text("steps:\n  - op: trim\n")

    def test_bad_name_slug(self):
        with pytest.raises(RecipeError, match="name"):
            load_recipe_text("name: Bad Name!\nsteps:\n  - op: trim\n")

    def test_unknown_top_key(self):
        with pytest.raises(RecipeError, match="unknown key"):
            load_recipe_text("name: x-y\npipeline: []\nsteps:\n  - op: trim\n")

    def test_unknown_op(self):
        with pytest.raises(RecipeError, match="unknown op"):
            load_recipe_text("name: x-y\nsteps:\n  - op: sharpen\n")

    def test_unknown_param_rejected(self):
        with pytest.raises(RecipeError, match="trim"):
            load_recipe_text("name: x-y\nsteps:\n  - op: trim\n    fuzz: 3\n")

    def test_bad_param_value(self):
        with pytest.raises(RecipeError, match="size"):
            load_recipe_text(
                "name: x-y\nsteps:\n  - op: preset-pack\n    presets:\n"
                "      - { name: a, size: huge, format: jpg }\n"
            )

    def test_duplicate_preset_names(self):
        with pytest.raises(RecipeError, match="duplicate"):
            load_recipe_text(
                "name: x-y\nsteps:\n  - op: preset-pack\n    presets:\n"
                "      - { name: a, size: 10x10 }\n"
                "      - { name: a, size: 20x20 }\n"
            )

    def test_transparent_bg_needs_alpha_format(self):
        with pytest.raises(RecipeError, match="alpha"):
            load_recipe_text(
                "name: x-y\nsteps:\n  - op: preset-pack\n    presets:\n"
                "      - { name: a, size: 10x10, format: jpg, background: transparent }\n"
            )

    def test_degenerate_geometry_rejected_at_load(self):
        for step in (
            "  - op: resize\n    size: 100000x100000\n",
            "  - op: composite\n    padding: -20\n",
            "  - op: preset-pack\n    presets:\n      - { name: a, size: [null, 3] }\n",
            "  - op: preset-pack\n    presets:\n      - { name: a, size: 100x100, padding: 60% }\n",
        ):
            with pytest.raises(RecipeError, match="step 1"):
                load_recipe_text(f"name: geo\nsteps:\n{step}")

    def test_empty_steps(self):
        with pytest.raises(RecipeError, match="steps"):
            load_recipe_text("name: x-y\nsteps: []\n")

    def test_invalid_yaml(self):
        with pytest.raises(RecipeError, match="YAML"):
            load_recipe_text("name: [unclosed")

    def test_bad_output_structure(self):
        with pytest.raises(RecipeError, match="structure"):
            load_recipe_text("name: x-y\nsteps:\n  - op: trim\noutput: { structure: nested }\n")

    def test_bad_input_format(self):
        with pytest.raises(RecipeError, match="format"):
            load_recipe_text("name: x-y\ninput: { formats: [raw] }\nsteps:\n  - op: trim\n")

    def test_rename_pattern_validation(self):
        with pytest.raises(RecipeError, match="rename"):
            load_recipe_text("name: x-y\nsteps:\n  - op: rename\n    pattern: '{unknown_var}'\n")


class TestDiscovery:
    def test_builtin_recipes_all_valid(self):
        recipes, problems = discover_recipes([BUILTIN_RECIPES_DIR])
        assert problems == []
        assert {"ecom-packshot", "marketplace-pack", "steam-capsule-pack"} <= set(recipes)
        for recipe in recipes.values():
            assert recipe.builtin

    def test_user_recipe_overrides_builtin(self, tmp_path):
        override = tmp_path / "ecom-packshot.yaml"
        override.write_text(
            "name: ecom-packshot\ndescription: mine\nsteps:\n  - op: trim\n",
            encoding="utf-8",
        )
        recipes, problems = discover_recipes([BUILTIN_RECIPES_DIR, tmp_path])
        assert problems == []
        assert recipes["ecom-packshot"].description == "mine"
        assert not recipes["ecom-packshot"].builtin

    def test_invalid_file_reported_not_fatal(self, tmp_path):
        (tmp_path / "broken.yaml").write_text("name: broken\nsteps: []\n", encoding="utf-8")
        (tmp_path / "fine.yaml").write_text(
            "name: fine\nsteps:\n  - op: trim\n", encoding="utf-8"
        )
        recipes, problems = discover_recipes([tmp_path])
        assert "fine" in recipes
        assert len(problems) == 1
        assert "broken" in problems[0]
