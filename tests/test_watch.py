"""Watch-folder mechanics: config, candidate filtering, stability, pickup."""

import asyncio
import time

import pytest

from batchroom.core.image_io import save_image
from batchroom.core.recipe import discover_recipes, recipe_dirs
from batchroom.models.manager import ModelManager
from batchroom.queue.db import Database
from batchroom.queue.events import EventBus
from batchroom.queue.jobs import JobQueue
from batchroom.watch.watcher import Watcher, load_watch_config

from .conftest import gradient_image

RECIPE = """
name: watch-pack
steps:
  - op: preset-pack
    presets:
      - { name: s, size: 24x24, format: png }
"""


class TestConfig:
    def test_missing_file_is_empty(self, tmp_path):
        result = load_watch_config(tmp_path / "none.yaml")
        assert result.entries == [] and result.problems == []

    def test_valid_and_invalid_entries(self, tmp_path):
        cfg = tmp_path / "watch.yaml"
        cfg.write_text(
            "watch:\n"
            "  - folder: /a\n    recipe: r1\n"
            "  - folder: /b\n    recipe: r2\n    output: /out\n    webhook: http://x\n"
            "  - recipe: missing-folder\n"
            "  - folder: /c\n    recipe: r3\n    bogus: 1\n",
            encoding="utf-8",
        )
        result = load_watch_config(cfg)
        assert len(result.entries) == 2
        assert len(result.problems) == 2
        assert str(result.entries[0].output).endswith("processed")
        assert result.entries[1].webhook == "http://x"


@pytest.fixture()
async def watch_env(settings, tmp_path):
    (settings.recipes_dir / "watch-pack.yaml").write_text(RECIPE, encoding="utf-8")
    hot = tmp_path / "hot"
    hot.mkdir()
    out = tmp_path / "hot-out"
    settings.watch_config_path.write_text(
        f"watch:\n  - folder: {hot}\n    recipe: watch-pack\n    output: {out}\n",
        encoding="utf-8",
    )

    def provider():
        recipes, _ = discover_recipes(recipe_dirs(settings))
        return recipes

    queue = JobQueue(
        settings, Database(settings.db_path), ModelManager(settings.models_dir),
        EventBus(), provider,
    )
    await queue.start()
    watcher = Watcher(settings, queue)
    watcher.reload_config()
    yield watcher, queue, hot, out
    await queue.stop()


class TestPickup:
    async def test_stable_file_processed_and_dispositioned(self, watch_env):
        watcher, queue, hot, out = watch_env
        save_image(gradient_image(48, 48), hot / "drop.png", "png")

        # scan 1 records the snapshot; wait past the stable window; scan 2 picks up
        watcher._scan_all()
        await asyncio.sleep(0.12)
        watcher._scan_all()
        assert watcher._active_jobs, "file was not picked up"
        assert not (hot / "drop.png").exists(), "source should move to intake"

        deadline = time.time() + 15
        job_id = next(iter(watcher._active_jobs))
        while time.time() < deadline:
            if queue.get_job(job_id)["status"] in ("done", "cancelled"):
                break
            await asyncio.sleep(0.05)
        watcher._check_active_jobs()

        assert (hot / "done" / "drop.png").is_file()
        assert (out / "s" / "drop.png").is_file()
        assert (out / "manifest.json").is_file()

    async def test_growing_file_not_picked_up(self, watch_env):
        watcher, queue, hot, out = watch_env
        target = hot / "copying.png"
        target.write_bytes(b"partial")
        watcher._scan_all()
        target.write_bytes(b"partial-more")  # size changed -> snapshot resets
        await asyncio.sleep(0.12)
        watcher._scan_all()
        # second scan re-recorded; only a third scan after stability may pick it up
        assert not watcher._active_jobs

    async def test_outputs_and_reserved_dirs_ignored(self, watch_env):
        watcher, queue, hot, out = watch_env
        (hot / "done").mkdir()
        save_image(gradient_image(24, 24), hot / "done" / "old.png", "png")
        out.mkdir(parents=True, exist_ok=True)
        save_image(gradient_image(24, 24), out / "result.png", "png")
        candidates = list(watcher._candidates(watcher.entries[0]))
        assert candidates == []

    async def test_failed_file_goes_to_failed(self, watch_env):
        watcher, queue, hot, out = watch_env
        (hot / "broken.png").write_bytes(b"not-a-png")
        watcher._scan_all()
        await asyncio.sleep(0.12)
        watcher._scan_all()
        job_id = next(iter(watcher._active_jobs))
        deadline = time.time() + 15
        while time.time() < deadline:
            if queue.get_job(job_id)["status"] in ("done", "cancelled"):
                break
            await asyncio.sleep(0.05)
        watcher._check_active_jobs()
        assert (hot / "failed" / "broken.png").is_file()
