"""Locale catalogs stay in lockstep with en.json (the shipping gate)."""

import json
import re
import subprocess
import sys
from pathlib import Path

from batchroom.i18n import LANGUAGES, STRINGS_DIR, available_languages, load_strings

PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
REPO_ROOT = Path(__file__).resolve().parent.parent


def test_ten_languages_with_arabic_rtl():
    langs = available_languages()
    assert len(langs) == 10
    assert {lang["code"] for lang in langs} == set(LANGUAGES)
    assert next(lang for lang in langs if lang["code"] == "ar")["rtl"] is True


def test_every_locale_mirrors_english():
    base = json.loads((STRINGS_DIR / "en.json").read_text(encoding="utf-8"))
    for code in LANGUAGES:
        data = json.loads((STRINGS_DIR / f"{code}.json").read_text(encoding="utf-8"))
        assert set(data) == set(base), f"{code}: key set differs from en"
        for key, value in data.items():
            assert isinstance(value, str) and value.strip(), f"{code}:{key} empty"
            assert set(PLACEHOLDER_RE.findall(value)) == set(
                PLACEHOLDER_RE.findall(base[key])
            ), f"{code}:{key} placeholder mismatch"


def test_loader_falls_back_to_english():
    assert load_strings("xx") == load_strings("en")


def test_verify_script_green():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "verify_i18n.py")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
