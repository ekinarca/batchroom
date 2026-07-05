"""Job queue lifecycle: submit → workers → finalize; cancel; crash resume."""

import asyncio
import json

import pytest

from batchroom.core.image_io import save_image
from batchroom.core.recipe import discover_recipes, recipe_dirs
from batchroom.models.manager import ModelManager
from batchroom.queue.db import Database
from batchroom.queue.events import EventBus
from batchroom.queue.jobs import JobQueue

from .conftest import gradient_image

RECIPE = """
name: tiny-pack
steps:
  - op: preset-pack
    presets:
      - { name: s, size: 32x32, format: png }
"""


@pytest.fixture()
def recipes_provider(settings):
    (settings.recipes_dir / "tiny-pack.yaml").write_text(RECIPE, encoding="utf-8")

    def provider():
        recipes, _ = discover_recipes(recipe_dirs(settings))
        return recipes

    return provider


@pytest.fixture()
async def queue(settings, recipes_provider):
    q = JobQueue(
        settings,
        Database(settings.db_path),
        ModelManager(settings.models_dir),
        EventBus(),
        recipes_provider,
    )
    await q.start()
    yield q
    await q.stop()


def make_inputs(tmp_path, n=3):
    files = []
    for i in range(n):
        path = tmp_path / "src" / f"img-{i}.png"
        save_image(gradient_image(64, 48), path, "png")
        files.append(path)
    return files


async def wait_status(queue, job_id, statuses=("done", "cancelled"), timeout=15.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        job = queue.get_job(job_id)
        if job["status"] in statuses:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job never reached {statuses}: {queue.get_job(job_id)}")


class TestLifecycle:
    async def test_submit_to_done(self, queue, settings, tmp_path, recipes_provider):
        recipe = recipes_provider()["tiny-pack"]
        files = make_inputs(tmp_path)
        job = queue.submit(recipe, files)
        assert job["status"] in ("queued", "running")
        job = await wait_status(queue, job["id"])
        assert (job["done"], job["failed"]) == (3, 0)
        manifest = json.loads(
            (settings.jobs_dir / job["id"] / "out" / "manifest.json").read_text()
        )
        assert manifest["counts"]["done"] == 3
        items = queue.get_items(job["id"])
        assert all(i["status"] == "done" for i in items)
        assert items[0]["result"]["outputs"][0]["path"] == "s/img-0.png"

    async def test_failed_item_isolated(self, queue, tmp_path, recipes_provider):
        recipe = recipes_provider()["tiny-pack"]
        files = make_inputs(tmp_path, 2)
        bad = tmp_path / "src" / "broken.png"
        bad.write_bytes(b"nope")
        job = queue.submit(recipe, [*files, bad])
        job = await wait_status(queue, job["id"])
        assert job["status"] == "done"
        assert job["done"] == 2 and job["failed"] == 1

    async def test_cancel_pending_items(self, queue, tmp_path, recipes_provider):
        recipe = recipes_provider()["tiny-pack"]
        files = make_inputs(tmp_path, 20)
        job = queue.submit(recipe, files)
        cancelled = queue.cancel(job["id"])
        assert "cancel_requested" not in cancelled  # internal flag never leaks to the API
        job = await wait_status(queue, job["id"])
        assert job["status"] == "cancelled"
        items = queue.get_items(job["id"])
        assert any(i["status"] == "cancelled" for i in items)

    async def test_delete_running_forbidden(self, queue, tmp_path, recipes_provider):
        recipe = recipes_provider()["tiny-pack"]
        job = queue.submit(recipe, make_inputs(tmp_path, 1))
        from batchroom.core.errors import JobError

        try:
            queue.delete(job["id"])
            deleted_early = True
        except JobError:
            deleted_early = False
        await wait_status(queue, job["id"])
        if not deleted_early:
            queue.delete(job["id"])
        with pytest.raises(JobError, match="not found"):
            queue.get_job(job["id"])


class TestResume:
    async def test_crashed_running_items_requeued(self, settings, recipes_provider, tmp_path):
        db = Database(settings.db_path)
        files = make_inputs(tmp_path, 2)
        # simulate a crashed previous run: job with one item stuck 'running'
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, recipe, status, source, output_dir, created_at, total)"
                " VALUES ('j-old', 'tiny-pack', 'running', 'api', ?, '2026-07-05T00:00:00+00:00', 2)",
                (str(tmp_path / "out"),),
            )
            conn.execute(
                "INSERT INTO items (job_id, idx, source_path, source_name, status, started_at)"
                " VALUES ('j-old', 0, ?, 'img-0.png', 'running', 'x')",
                (str(files[0]),),
            )
            conn.execute(
                "INSERT INTO items (job_id, idx, source_path, source_name, status)"
                " VALUES ('j-old', 1, ?, 'img-1.png', 'pending')",
                (str(files[1]),),
            )
        q = JobQueue(settings, db, ModelManager(settings.models_dir), EventBus(), recipes_provider)
        await q.start()
        try:
            job = await wait_status(q, "j-old")
            assert job["status"] == "done"
            assert job["done"] == 2
            assert (tmp_path / "out" / "manifest.json").is_file()
        finally:
            await q.stop()
