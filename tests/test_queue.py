"""Job queue lifecycle: submit → workers → finalize; cancel; crash resume."""

import asyncio
import json
import sqlite3

import pytest

from batchroom.core.errors import JobError
from batchroom.core.image_io import save_image
from batchroom.core.recipe import discover_recipes, recipe_dirs
from batchroom.models.manager import ModelManager
from batchroom.queue.db import SCHEMA, Database
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


class TestCancelRetry:
    async def test_cancel_counts_every_item(self, queue, tmp_path, recipes_provider):
        recipe = recipes_provider()["tiny-pack"]
        job = queue.submit(recipe, make_inputs(tmp_path, 20))
        queue.cancel(job["id"])
        job = await wait_status(queue, job["id"])
        assert job["status"] == "cancelled"
        # pending items cancelled in bulk used to be missing from the counter ("2/20")
        assert job["done"] + job["failed"] + job["cancelled"] == job["total"] == 20

    async def test_retry_requeues_failed_items(self, queue, settings, tmp_path, recipes_provider):
        recipe = recipes_provider()["tiny-pack"]
        files = make_inputs(tmp_path, 2)
        late = tmp_path / "src" / "late.png"
        late.write_bytes(b"still copying")
        job = await wait_status(queue, queue.submit(recipe, [*files, late])["id"])
        assert (job["done"], job["failed"]) == (2, 1)

        save_image(gradient_image(64, 48), late, "png")  # fixed: run it again
        again = queue.retry(job["id"])
        assert again["status"] == "queued" and again["failed"] == 0
        job = await wait_status(queue, job["id"])
        assert (job["status"], job["done"], job["failed"]) == ("done", 3, 0)
        items = queue.get_items(job["id"])
        paths = sorted(o["path"] for i in items for o in i["result"]["outputs"])
        assert paths == ["s/img-0.png", "s/img-1.png", "s/late.png"]
        manifest = json.loads((settings.jobs_dir / job["id"] / "out" / "manifest.json").read_text())
        assert manifest["counts"] == {"total": 3, "done": 3, "failed": 0, "cancelled": 0}

    async def test_retry_refusals(self, queue, tmp_path, recipes_provider):
        recipe = recipes_provider()["tiny-pack"]
        job = await wait_status(queue, queue.submit(recipe, make_inputs(tmp_path, 1))["id"])
        with pytest.raises(JobError, match="nothing to retry"):
            queue.retry(job["id"])
        with queue.db.connect() as conn:
            conn.execute("UPDATE jobs SET source='watch' WHERE id=?", (job["id"],))
        with pytest.raises(JobError, match="watch-folder"):
            queue.retry(job["id"])


class TestManifest:
    async def test_manifest_opt_out_honored(self, queue, settings, tmp_path, recipes_provider):
        (settings.recipes_dir / "quiet.yaml").write_text(
            "name: quiet\nsteps:\n  - op: trim\noutput: { manifest: false }\n", encoding="utf-8"
        )
        recipe = recipes_provider()["quiet"]
        job = await wait_status(queue, queue.submit(recipe, make_inputs(tmp_path, 1))["id"])
        assert job["status"] == "done"
        assert not (settings.jobs_dir / job["id"] / "out" / "manifest.json").exists()
        manifest = queue.manifest_for(job["id"])  # still available from the database
        assert manifest["counts"]["done"] == 1 and manifest["finished_at"] == job["finished_at"]


class TestDispatcher:
    async def test_survives_a_transient_database_error(
        self, queue, tmp_path, recipes_provider, monkeypatch
    ):
        real = queue._claim_next
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real()

        monkeypatch.setattr(queue, "_claim_next", flaky)
        recipe = recipes_provider()["tiny-pack"]
        job = await wait_status(queue, queue.submit(recipe, make_inputs(tmp_path, 1))["id"])
        assert job["status"] == "done" and calls["n"] > 1


class TestCrashStates:
    """Jobs a crash caught between two states must settle on the next boot."""

    async def _boot(self, settings, recipes_provider, db, job_id):
        q = JobQueue(settings, db, ModelManager(settings.models_dir), EventBus(), recipes_provider)
        await q.start()
        try:
            return await wait_status(q, job_id, timeout=5)
        finally:
            await q.stop()

    async def test_cancel_requested_before_crash(self, settings, recipes_provider, tmp_path):
        db = Database(settings.db_path)
        files = make_inputs(tmp_path, 2)
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, recipe, status, source, output_dir, created_at, total,"
                " cancel_requested) VALUES ('j-cx', 'tiny-pack', 'running', 'api', ?,"
                " '2026-07-05T00:00:00+00:00', 2, 1)",
                (str(tmp_path / "out"),),
            )
            conn.execute(
                "INSERT INTO items (job_id, idx, source_path, source_name, status)"
                " VALUES ('j-cx', 0, ?, 'img-0.png', 'running'), ('j-cx', 1, ?, 'img-1.png', 'pending')",
                (str(files[0]), str(files[1])),
            )
        job = await self._boot(settings, recipes_provider, db, "j-cx")
        assert job["status"] == "cancelled" and job["cancelled"] == 2

    async def test_finished_items_but_never_finalized(self, settings, recipes_provider, tmp_path):
        db = Database(settings.db_path)
        files = make_inputs(tmp_path, 1)
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, recipe, status, source, output_dir, created_at, total, done)"
                " VALUES ('j-fx', 'tiny-pack', 'running', 'api', ?, '2026-07-05T00:00:00+00:00', 1, 1)",
                (str(tmp_path / "out"),),
            )
            conn.execute(
                "INSERT INTO items (job_id, idx, source_path, source_name, status, result_json)"
                " VALUES ('j-fx', 0, ?, 'img-0.png', 'done', '{}')",
                (str(files[0]),),
            )
        job = await self._boot(settings, recipes_provider, db, "j-fx")
        assert job["status"] == "done"
        assert (tmp_path / "out" / "manifest.json").is_file()


class TestMigration:
    def test_v2_counters_repaired(self, settings):
        conn = sqlite3.connect(settings.db_path)
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version = 2")
        conn.execute(
            "INSERT INTO jobs (id, recipe, status, source, output_dir, created_at, finished_at,"
            " total, cancelled) VALUES ('j-old', 'r', 'cancelled', 'api', '/x',"
            " '2026-07-05T00:00:00+00:00', '2026-07-05T00:01:00+00:00', 3, 1)"
        )
        conn.executemany(
            "INSERT INTO items (job_id, idx, source_path, source_name, status)"
            " VALUES ('j-old', ?, '/x', 'x', ?)",
            [(0, "done"), (1, "cancelled"), (2, "cancelled")],
        )
        conn.commit()
        conn.close()
        db = Database(settings.db_path)
        with db.connect() as c:
            row = c.execute("SELECT done, failed, cancelled FROM jobs WHERE id='j-old'").fetchone()
            assert tuple(row) == (1, 0, 2)
            assert c.execute("PRAGMA user_version").fetchone()[0] == 3
