#!/usr/bin/env python3
"""Verify i18n catalogs: every locale must mirror en.json exactly.

Checks: identical key sets, no empty values, matching {placeholder} sets,
valid JSON. Exit 1 on any problem — wired into CI and the test suite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

STRINGS_DIR = Path(__file__).resolve().parent.parent / "src" / "batchroom" / "i18n" / "strings"
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def main() -> int:
    source = STRINGS_DIR / "en.json"
    base = json.loads(source.read_text(encoding="utf-8"))
    base_keys = set(base)
    problems: list[str] = []

    locales = sorted(STRINGS_DIR.glob("*.json"))
    if len(locales) < 2:
        problems.append("expected multiple locale files")
    for path in locales:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"{path.name}: invalid JSON: {exc}")
            continue
        keys = set(data)
        missing = base_keys - keys
        extra = keys - base_keys
        for key in sorted(missing):
            problems.append(f"{path.name}: missing key {key}")
        for key in sorted(extra):
            problems.append(f"{path.name}: extra key {key}")
        for key, value in data.items():
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{path.name}: empty value for {key}")
                continue
            if key in base:
                want = set(PLACEHOLDER_RE.findall(base[key]))
                got = set(PLACEHOLDER_RE.findall(value))
                if want != got:
                    problems.append(
                        f"{path.name}: placeholder mismatch in {key}: "
                        f"expected {sorted(want)}, got {sorted(got)}"
                    )

    if problems:
        print(f"i18n: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"i18n OK: {len(locales)} locales x {len(base_keys)} keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
