"""Input discovery shared by the API, the CLI and watch folders.

One rule set for "which files under this directory are inputs": hidden
files and folders, NAS/OS system folders (Synology ``@eaDir`` thumbnails,
``#recycle`` bins, QNAP ``@Recycle``, Windows ``$RECYCLE.BIN`` …) and any
caller-excluded directory (e.g. our own output folder) are never walked.
Without this, a watch folder on a Synology share would ingest — and move —
the indexer's thumbnail files.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterable, Iterator
from pathlib import Path

# Matched case-insensitively against directory names.
SYSTEM_DIR_NAMES = {
    "#recycle",
    "#snapshot",
    "$recycle.bin",
    "system volume information",
    "lost+found",
}


def is_skipped_dir_name(name: str) -> bool:
    """Hidden (``.``) or NAS system (``@``) folders, plus known system names."""
    return name.startswith((".", "@")) or name.lower() in SYSTEM_DIR_NAMES


def is_hidden_file_name(name: str) -> bool:
    # also covers macOS AppleDouble companions ("._photo.jpg")
    return name.startswith(".")


def walk_files(root: Path, *, exclude_dirs: Iterable[Path] = ()) -> Iterator[Path]:
    """Yield files under ``root`` in sorted order, pruning skipped folders.

    ``exclude_dirs`` are compared after resolving, so an output folder nested
    inside the input tree is never descended into. Symlinked folders are not
    followed (no loops on NAS shares).
    """
    excluded = set()
    for path in exclude_dirs:
        try:
            excluded.add(Path(path).resolve())
        except OSError:
            continue
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for name in sorted(dirnames):
            if is_skipped_dir_name(name):
                continue
            if excluded:
                try:
                    if Path(dirpath, name).resolve() in excluded:
                        continue
                except OSError:
                    continue
            keep.append(name)
        dirnames[:] = keep  # prune in place: os.walk honors this (topdown)
        for name in sorted(filenames):
            if not is_hidden_file_name(name):
                yield Path(dirpath, name)


def safe_upload_name(raw: str, fallback: str, *, max_bytes: int = 200) -> str:
    """Make a client-supplied filename safe to store, keeping it recognizable.

    Unicode letters survive (``商品.jpg``, ``çanta.jpg`` stay readable — they
    become the ``{original}`` part of output names); path separators, control
    and Windows-reserved characters are replaced; leading dots are dropped so
    an upload can never become a hidden file. The extension is preserved even
    when the stem has to be shortened to fit filesystem limits.
    """
    name = unicodedata.normalize("NFC", Path(raw.replace("\\", "/")).name)
    # control/format characters (incl. RTL overrides used for name spoofing) go too
    name = "".join(
        "-" if (ch in '<>:"/\\|?*' or unicodedata.category(ch)[0] == "C") else ch
        for ch in name
    ).strip()
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = stem.strip(" .-")  # no leading dot: an upload never becomes a hidden file
    ext = ext.strip(" .")
    if not stem:
        stem = fallback
    ext = f".{ext}" if ext else ""
    while len((stem + ext).encode("utf-8")) > max_bytes and len(stem) > 1:
        stem = stem[:-1]
    return stem + ext
