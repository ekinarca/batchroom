"""Recipe schema, parsing and discovery.

A recipe is a YAML file describing a processing pipeline:

.. code-block:: yaml

    name: etsy-packshot
    description: Remove background, white canvas, Etsy + Instagram sizes
    input: { formats: [jpg, png, webp, heic] }
    steps:
      - op: remove-background
        model: isnet-general
      - op: composite
        background: "#FFFFFF"
        padding: 8%
      - op: preset-pack
        presets:
          - { name: etsy-primary, size: 2000x2000, format: jpg, quality: 90 }
    output: { structure: per-preset-folder, manifest: true }

Validation is strict and happens at load time: unknown ops, unknown params
and malformed values fail with file/step context before any image is touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..ops.base import Op, OpParams, get_op
from .errors import ImageIOError, RecipeError
from .image_io import FORMATS, normalize_format
from .imaging import parse_size

BUILTIN_RECIPES_DIR = Path(__file__).resolve().parent.parent / "recipes"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_ALLOWED_TOP_KEYS = {"name", "description", "version", "input", "steps", "output"}
_ALLOWED_OUTPUT_KEYS = {"structure", "manifest", "format", "quality", "lossless", "keep_metadata"}
_ALLOWED_INPUT_KEYS = {"formats"}
_STRUCTURES = {"per-preset-folder", "flat"}
# steps that still act after preset-pack (both are applied when files are written)
_AFTER_PRESET_PACK = {"rename", "strip-metadata"}


@dataclass
class OutputSpec:
    structure: str = "per-preset-folder"
    manifest: bool = True
    format: str | None = None  # None -> keep source format (unless a convert step runs)
    quality: int = 90
    lossless: bool = False
    keep_metadata: bool = False


@dataclass
class RecipeStep:
    op: Op
    params: OpParams
    raw: dict


@dataclass
class Recipe:
    name: str
    description: str = ""
    version: int = 1
    input_formats: set[str] = field(default_factory=set)  # empty = accept all readable
    steps: list[RecipeStep] = field(default_factory=list)
    output: OutputSpec = field(default_factory=OutputSpec)
    path: Path | None = None
    source_text: str = ""
    builtin: bool = False

    def models_used(self) -> list[str]:
        seen: list[str] = []
        for step in self.steps:
            for model_id in step.op.models_used(step.params):
                if model_id not in seen:
                    seen.append(model_id)
        return seen

    def accepts_extension(self, ext: str) -> bool:
        ext = ext.lower().lstrip(".")
        if ext == "jpeg":
            ext = "jpg"
        if ext in ("heif",):
            ext = "heic"
        if ext in ("tif",):
            ext = "tiff"
        if not self.input_formats:
            return True
        return ext in self.input_formats

    def summary(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "steps": [s.op.name for s in self.steps],
            "models": self.models_used(),
            "input_formats": sorted(self.input_formats) or ["*"],
            "builtin": self.builtin,
        }


def _require_mapping(value, what: str, recipe: str | None) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RecipeError(f"{what} must be a mapping, got {type(value).__name__}", recipe=recipe)
    return value


def parse_recipe(
    data: dict,
    *,
    path: Path | None = None,
    source_text: str = "",
    builtin: bool = False,
) -> Recipe:
    if not isinstance(data, dict):
        raise RecipeError("recipe root must be a mapping")
    name = data.get("name")
    recipe_label = str(name) if name else (path.stem if path else "<inline>")

    unknown = set(data) - _ALLOWED_TOP_KEYS
    if unknown:
        raise RecipeError(
            f"unknown key(s): {', '.join(sorted(str(k) for k in unknown))}",
            recipe=recipe_label,
            hint=f"allowed: {', '.join(sorted(_ALLOWED_TOP_KEYS))}",
        )
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise RecipeError(
            f"invalid or missing 'name' ({name!r})",
            recipe=recipe_label,
            hint="use a lowercase slug like 'etsy-packshot' (2-64 chars, a-z 0-9 -)",
        )

    # ---- input --------------------------------------------------------
    input_spec = _require_mapping(data.get("input"), "'input'", name)
    unknown = set(input_spec) - _ALLOWED_INPUT_KEYS
    if unknown:
        raise RecipeError(
            f"unknown input key(s): {', '.join(sorted(unknown))}", recipe=name
        )
    formats: set[str] = set()
    raw_formats = input_spec.get("formats", [])
    if raw_formats:
        if not isinstance(raw_formats, list):
            raise RecipeError("input.formats must be a list", recipe=name)
        for f in raw_formats:
            try:
                formats.add(normalize_format(str(f)))
            except Exception as exc:
                raise RecipeError(
                    f"input.formats: unsupported format '{f}'",
                    recipe=name,
                    hint=f"supported: {', '.join(sorted(set(FORMATS) - {'jpeg'}))}",
                ) from exc

    # ---- steps --------------------------------------------------------
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise RecipeError("'steps' must be a non-empty list", recipe=name)
    steps: list[RecipeStep] = []
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict) or "op" not in raw:
            raise RecipeError("each step must be a mapping with an 'op' key", recipe=name, step=i)
        op_name = str(raw["op"])
        try:
            op = get_op(op_name)
        except RecipeError as exc:
            raise RecipeError(exc.message, recipe=name, step=i, hint=exc.hint) from exc
        params_raw = {k: v for k, v in raw.items() if k != "op"}
        params = op.validate_params(params_raw, recipe=name, step=i)
        steps.append(RecipeStep(op=op, params=params, raw=raw))
    if sum(1 for s in steps if s.op.name == "preset-pack") > 1:
        # PresetPackOp refuses this per item; fail at load, not mid-batch
        raise RecipeError("only one preset-pack step is allowed per recipe", recipe=name)
    pack_at = next((i for i, s in enumerate(steps) if s.op.name == "preset-pack"), None)
    if pack_at is not None:
        for i, step in enumerate(steps[pack_at + 1 :], start=pack_at + 1):
            # the presets are already sized and encoded-to-be: a later pixel or
            # format step would be silently ignored
            if step.op.name not in _AFTER_PRESET_PACK:
                raise RecipeError(
                    f"'{step.op.name}' has no effect after preset-pack",
                    recipe=name,
                    step=i,
                    hint=f"move it before preset-pack (only {', '.join(sorted(_AFTER_PRESET_PACK))}"
                    " may follow it)",
                )

    # ---- output -------------------------------------------------------
    out_raw = _require_mapping(data.get("output"), "'output'", name)
    unknown = set(out_raw) - _ALLOWED_OUTPUT_KEYS
    if unknown:
        raise RecipeError(
            f"unknown output key(s): {', '.join(sorted(unknown))}",
            recipe=name,
            hint=f"allowed: {', '.join(sorted(_ALLOWED_OUTPUT_KEYS))}",
        )
    structure = str(out_raw.get("structure", "per-preset-folder"))
    if structure not in _STRUCTURES:
        raise RecipeError(
            f"output.structure must be one of {sorted(_STRUCTURES)}, got '{structure}'",
            recipe=name,
        )
    out_format = out_raw.get("format")
    if out_format is not None:
        try:
            out_format = normalize_format(str(out_format))
        except ImageIOError as exc:
            raise RecipeError(
                f"output.format: unsupported format '{out_format}'", recipe=name, hint=exc.hint
            ) from exc
    quality = out_raw.get("quality", 90)
    if isinstance(quality, bool) or not isinstance(quality, int) or not (1 <= quality <= 100):
        raise RecipeError("output.quality must be an integer 1-100", recipe=name)
    flags = {}
    for key, default in (("manifest", True), ("lossless", False), ("keep_metadata", False)):
        value = out_raw.get(key, default)
        if not isinstance(value, bool):  # "false" in quotes is a truthy string
            raise RecipeError(
                f"output.{key} must be true or false, got {value!r}", recipe=name
            )
        flags[key] = value
    output = OutputSpec(structure=structure, format=out_format, quality=quality, **flags)
    if pack_at is not None:
        _check_pack_formats(steps, pack_at, out_raw, name)
    else:
        _check_webp_size(steps, out_format, name)

    try:
        version = int(data.get("version", 1))
    except (TypeError, ValueError):
        raise RecipeError(
            f"'version' must be an integer, got {data.get('version')!r}", recipe=name
        ) from None

    return Recipe(
        name=name,
        description=str(data.get("description") or ""),
        version=version,
        input_formats=formats,
        steps=steps,
        output=output,
        path=path,
        source_text=source_text,
        builtin=builtin,
    )


def _check_pack_formats(steps: list[RecipeStep], pack_at: int, out_raw: dict, name: str) -> None:
    """Every preset is encoded with its own format/quality: a ``convert`` before
    the pack, or ``output.format``/``quality``/``lossless``, would be ignored."""
    for i, step in enumerate(steps[:pack_at]):
        if step.op.name == "convert":
            raise RecipeError(
                "'convert' has no effect before preset-pack",
                recipe=name,
                step=i,
                hint="set format/quality on each preset instead"
                " (output.keep_metadata keeps EXIF)",
            )
    ignored = sorted(k for k in ("format", "quality", "lossless") if k in out_raw)
    if ignored:
        raise RecipeError(
            f"output.{', output.'.join(ignored)} has no effect with preset-pack",
            recipe=name,
            hint="set format/quality/lossless on each preset instead",
        )


# steps that leave the image size as it is
_SIZE_KEEPING = {"convert", "rename", "strip-metadata", "remove-background"}


def _check_webp_size(steps: list[RecipeStep], out_format: str | None, name: str) -> None:
    """A fixed resize past libwebp's side limit, written as WebP, fails every
    item: refuse it at load like the same preset."""
    from ..ops.output import WEBP_MAX_SIDE

    size: tuple[int, int] | None = None
    fmt = out_format
    at = None
    for i, step in enumerate(steps):
        params = step.params
        if step.op.name == "convert":
            fmt = normalize_format(params.format)
        elif step.op.name == "resize" and params.size is not None:
            size, at = parse_size(params.size), i
        elif step.op.name == "resize" and params.width and params.height:
            size, at = (params.width, params.height), i
        elif step.op.name not in _SIZE_KEEPING:
            size = None
    if fmt == "webp" and size is not None and max(size) > WEBP_MAX_SIDE:
        raise RecipeError(
            f"WebP images are at most {WEBP_MAX_SIDE} px per side, the resize makes"
            f" {size[0]}x{size[1]}",
            recipe=name,
            step=at,
            hint="use a smaller size or another format",
        )


def load_recipe_text(text: str, *, path: Path | None = None, builtin: bool = False) -> Recipe:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        label = path.name if path else "<inline>"
        raise RecipeError(f"invalid YAML in {label}: {exc}") from exc
    return parse_recipe(data, path=path, source_text=text, builtin=builtin)


def load_recipe_file(path: str | Path, *, builtin: bool = False) -> Recipe:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:  # e.g. saved as UTF-16 by Notepad
        raise RecipeError(
            f"cannot read recipe file {path}: not UTF-8 text", hint="save it as UTF-8"
        ) from exc
    except OSError as exc:
        raise RecipeError(f"cannot read recipe file {path}: {exc}") from exc
    return load_recipe_text(text, path=path, builtin=builtin)


def discover_recipes(dirs: list[Path]) -> tuple[dict[str, Recipe], list[str]]:
    """Load recipes from a list of directories; later directories override
    earlier ones (user recipes shadow builtins). Returns (recipes, problems)."""
    recipes: dict[str, Recipe] = {}
    problems: list[str] = []
    for i, directory in enumerate(dirs):
        if not directory or not Path(directory).is_dir():
            continue
        builtin = i == 0 and Path(directory) == BUILTIN_RECIPES_DIR
        for path in sorted(Path(directory).glob("*.yaml")) + sorted(
            Path(directory).glob("*.yml")
        ):
            try:
                recipe = load_recipe_file(path, builtin=builtin)
            except RecipeError as exc:
                problems.append(str(exc))
                continue
            recipes[recipe.name] = recipe
    return recipes, problems


def recipe_dirs(settings) -> list[Path]:
    """Builtin dir first, then the user dir, then any extra dirs."""
    return [BUILTIN_RECIPES_DIR, settings.recipes_dir, *settings.extra_recipe_dirs]
