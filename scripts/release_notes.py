#!/usr/bin/env python3
"""Print a version's release notes: its CHANGELOG.md section + install lines.

    python scripts/release_notes.py v0.2.0 [IMAGE] > notes.md

Used by .github/workflows/release.yml to publish the GitHub release page.
Fails when CHANGELOG.md has no section for the version, so a tag pushed
without notes errors out instead of publishing an empty page.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
DEFAULT_IMAGE = "ghcr.io/ekinarca/batchroom"


def changelog_section(text: str, version: str) -> str:
    lines = text.splitlines()
    heading = re.compile(rf"^## \[{re.escape(version)}\]")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## ["):
            break
        body.append(line)
    notes = "\n".join(body).strip()
    if not notes:
        raise SystemExit(f"CHANGELOG.md section for {version} is empty")
    return notes


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    version = argv[1].removeprefix("v")
    image = argv[2] if len(argv) == 3 else DEFAULT_IMAGE
    print(changelog_section(CHANGELOG.read_text(encoding="utf-8"), version))
    print(
        "\n## Install\n\n```bash\n"
        f"docker run -d --name batchroom -p 5151:5151 -v batchroom-data:/data {image}:{version}\n"
        "```\n\nImages are published for linux/amd64 and linux/arm64."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
