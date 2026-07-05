"""Batch manifest: the machine-readable record of what a job produced.

Written atomically to ``manifest.json`` in the job output directory. Everything
an integration needs to consume the results without scraping the filesystem:
inputs, outputs (with sizes + SHA-256), statuses, timings, recipe identity.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .. import __version__
from .pipeline import ItemResult

MANIFEST_NAME = "manifest.json"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def build_manifest(
    *,
    job_id: str,
    recipe_name: str,
    recipe_version: int,
    output_structure: str,
    started_at: str,
    finished_at: str | None,
    results: list[ItemResult],
) -> dict:
    ok = sum(1 for r in results if r.status == "done")
    failed = sum(1 for r in results if r.status == "failed")
    cancelled = sum(1 for r in results if r.status == "cancelled")
    return {
        "app": "batchroom",
        "app_version": __version__,
        "manifest_version": 1,
        "job_id": job_id,
        "recipe": {"name": recipe_name, "version": recipe_version},
        "output_structure": output_structure,
        "started_at": started_at,
        "finished_at": finished_at or utc_now_iso(),
        "counts": {
            "total": len(results),
            "done": ok,
            "failed": failed,
            "cancelled": cancelled,
        },
        "items": [r.to_dict() for r in results],
    }


def write_manifest(out_dir: Path, manifest: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / MANIFEST_NAME
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path
