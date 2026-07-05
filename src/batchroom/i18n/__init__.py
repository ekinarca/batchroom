"""UI string catalogs — 10 languages from day one (plan §8.1).

``strings/en.json`` is the source of truth; every other locale must carry
exactly the same key set (enforced by scripts/verify_i18n.py and the test
suite). Keys are flat dot-paths; values may contain ``{placeholders}``
substituted client-side.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

STRINGS_DIR = Path(__file__).resolve().parent / "strings"

# code -> (native name, rtl)
LANGUAGES: dict[str, tuple[str, bool]] = {
    "en": ("English", False),
    "tr": ("Türkçe", False),
    "fr": ("Français", False),
    "it": ("Italiano", False),
    "ja": ("日本語", False),
    "zh": ("中文", False),
    "es": ("Español", False),
    "ar": ("العربية", True),
    "pl": ("Polski", False),
    "ru": ("Русский", False),
}

DEFAULT_LANGUAGE = "en"


def available_languages() -> list[dict]:
    return [
        {"code": code, "name": name, "rtl": rtl}
        for code, (name, rtl) in LANGUAGES.items()
    ]


@cache
def load_strings(lang: str) -> dict[str, str]:
    if lang not in LANGUAGES:
        lang = DEFAULT_LANGUAGE
    path = STRINGS_DIR / f"{lang}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if lang != DEFAULT_LANGUAGE:
        # missing keys fall back to English so a partial locale never breaks the UI
        base = load_strings(DEFAULT_LANGUAGE)
        return {**base, **data}
    return data
