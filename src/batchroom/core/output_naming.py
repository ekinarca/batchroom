"""Output path planning: rename patterns + output structure → relative path."""

from __future__ import annotations

from ..ops.output import render_pattern

__all__ = ["plan_relpath"]


def plan_relpath(
    *,
    pattern: str | None,
    structure: str,
    preset: str | None,
    ext: str,
    values: dict[str, str],
) -> str:
    """Relative POSIX path for one output file.

    Default pattern: ``{original}`` for single outputs, ``{original}-{preset}``
    when a preset pack fans out. With ``per-preset-folder`` structure the
    preset name becomes a directory and is dropped from the default filename
    (folder already disambiguates).
    """
    per_folder = structure == "per-preset-folder" and preset
    if pattern is None:
        pattern = "{original}" if (preset is None or per_folder) else "{original}-{preset}"
    name = render_pattern(pattern, values) + ext
    if per_folder:
        return f"{preset}/{name}"
    return name
