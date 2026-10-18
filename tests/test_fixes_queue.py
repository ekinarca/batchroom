"""Regression tests for queue, watch-folder, webhook and CLI fixes."""

import asyncio
import threading
import time

import pytest

from batchroom.cli import main
from batchroom.core.errors import JobError
from batchroom.core.image_io import save_image
from batchroom.core.recipe import discover_recipes, recipe_dirs
from batchroom.models.manager import ModelManager
from batchroom.queue.db import Database
from batchroom.queue.events import EventBus
from batchroom.queue.jobs import JobQueue
from batchroom.queue.webhooks import deliver_webhook
from batchroom.watch.watcher import INTAKE_DIR, Watcher, load_watch_config

from .conftest import gradient_image
from .test_queue import make_inputs, wait_status

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


class TestDeleteEvent:
    async def test_delete_from_a_worker_thread_publishes_on_the_loop(
        self, queue, tmp_path, recipes_provider, monkeypatch
    ):
        recipe = recipes_provider()["tiny-pack"]
        job = await wait_status(queue, queue.submit(recipe, make_inputs(tmp_path, 1))["id"])
        await asyncio.sleep(0.1)  # let the finalize events land first
        sub = queue.events.subscribe()
        threads: list[tuple[str, int]] = []
        real = queue.events.publish

        def spy(event_type, **data):
            threads.append((event_type, threading.get_ident()))
            real(event_type, **data)

        monkeypatch.setattr(queue.events, "publish", spy)
        # the DELETE route is a sync endpoint: FastAPI runs it in a worker thread,
        # and asyncio queues must only be touched from the loop's own thread
        await asyncio.to_thread(queue.delete, job["id"])
        event = await asyncio.wait_for(sub.get(), timeout=2.0)
        assert event["type"] == "job.deleted" and event["job_id"] == job["id"]
        assert threads == [("job.deleted", threading.get_ident())]


class TestUnwritableOutput:
    async def test_submit_reports_a_clean_job_error(self, queue, tmp_path, recipes_provider):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a folder")
        recipe = recipes_provider()["tiny-pack"]
        with pytest.raises(JobError, match="cannot create output folder"):
            queue.submit(recipe, make_inputs(tmp_path, 1), output_dir=blocker / "out")

    @pytest.fixture()
    async def blocked_watch(self, settings, tmp_path, queue):
        (settings.recipes_dir / "tiny-pack.yaml").write_text(RECIPE, encoding="utf-8")
        hot = tmp_path / "hot"
        hot.mkdir()
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a folder")
        settings.watch_config_path.write_text(
            f"watch:\n  - folder: {hot}\n    recipe: tiny-pack\n    output: {blocker / 'out'}\n",
            encoding="utf-8",
        )
        watcher = Watcher(settings, queue)
        watcher.reload_config()
        assert watcher.entries
        return watcher, hot

    async def test_watch_pickup_moves_sources_to_failed(self, blocked_watch):
        watcher, hot = blocked_watch
        save_image(gradient_image(24, 24), hot / "drop.png", "png")
        watcher._scan_all()
        await asyncio.sleep(0.12)
        watcher._scan_all()  # used to raise, stranding the file in hidden .intake/
        assert (hot / "failed" / "drop.png").is_file()
        assert not watcher._active_jobs
        assert not any((hot / INTAKE_DIR).glob("*/*"))

    async def test_orphan_recovery_does_not_crash_boot(self, blocked_watch):
        watcher, hot = blocked_watch
        orphan = hot / INTAKE_DIR / "j-20260101-abcdef"
        save_image(gradient_image(24, 24), orphan / "drop.png", "png")
        watcher._recover_orphans()  # runs in Watcher.start(): used to abort server startup
        assert (hot / "failed" / "drop.png").is_file()
        assert not orphan.exists()


class TestWatchConfigShape:
    @pytest.mark.parametrize("text", ["- folder: /a\n  recipe: r\n", "just a string\n"])
    def test_non_mapping_top_level_is_a_problem_not_a_crash(self, tmp_path, text):
        cfg = tmp_path / "watch.yaml"
        cfg.write_text(text, encoding="utf-8")
        result = load_watch_config(cfg)
        assert result.entries == []
        assert any("top-level 'watch:'" in p for p in result.problems)


class TestWebhookInvalidUrl:
    async def test_malformed_url_is_logged_not_raised(self, caplog):
        started = time.monotonic()
        # passes the API's scheme+host check, but httpx raises InvalidURL (not an HTTPError)
        ok = await deliver_webhook("http://hooks.local:port/x", {"job_id": "j-1"})
        assert ok is False
        assert time.monotonic() - started < 1.0  # no pointless retry backoff
        assert any("invalid" in r.getMessage() for r in caplog.records)


class TestCliWorkers:
    @pytest.mark.parametrize("workers", ["0", "-1"])
    def test_non_positive_workers_rejected(self, tmp_path, monkeypatch, capsys, workers):
        monkeypatch.setenv("BATCHROOM_DATA_DIR", str(tmp_path / "data"))
        src = tmp_path / "a.png"
        save_image(gradient_image(16, 16), src, "png")
        with pytest.raises(SystemExit) as exc:
            main(["run", "tiny", str(src), "-o", str(tmp_path / "out"), "--workers", workers])
        assert exc.value.code == 2
        assert "must be >= 1" in capsys.readouterr().err


def test_job_ids_have_room_for_busy_days():
    from batchroom.queue.jobs import new_job_id

    ids = {new_job_id() for _ in range(5000)}
    assert len(ids) == 5000
    assert all(len(i.rsplit("-", 1)[1]) == 10 for i in ids)
