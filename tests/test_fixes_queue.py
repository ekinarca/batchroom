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


FLAT_RECIPE = """
name: flat-png
steps:
  - op: resize
    size: 16x16
  - op: convert
    format: png
output:
  structure: flat
  manifest: false
"""


class TestOutputFolderIsInputFolder:
    def test_cli_never_overwrites_a_source(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BATCHROOM_DATA_DIR", str(tmp_path / "data"))
        recipe = tmp_path / "flat.yaml"
        recipe.write_text(FLAT_RECIPE, encoding="utf-8")
        photos = tmp_path / "photos"
        save_image(gradient_image(32, 32), photos / "a.png", "png")
        original = (photos / "a.png").read_bytes()
        assert main(["run", str(recipe), str(photos), "-o", str(photos)]) == 0
        assert (photos / "a.png").read_bytes() == original
        assert (photos / "a-2.png").is_file()

    async def test_path_job_never_overwrites_a_source(self, queue, tmp_path, settings):
        (settings.recipes_dir / "flat-png.yaml").write_text(FLAT_RECIPE, encoding="utf-8")
        recipe = discover_recipes(recipe_dirs(settings))[0]["flat-png"]
        photos = tmp_path / "photos"
        save_image(gradient_image(32, 32), photos / "a.png", "png")
        original = (photos / "a.png").read_bytes()
        job = queue.submit(recipe, [photos / "a.png"], output_dir=photos)
        await wait_status(queue, job["id"], ("done",))
        assert (photos / "a.png").read_bytes() == original
        assert (photos / "a-2.png").is_file()


class TestCliUnwritableFolders:
    def test_output_folder_error_is_a_message_not_a_traceback(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BATCHROOM_DATA_DIR", str(tmp_path / "data"))
        src = tmp_path / "a.png"
        save_image(gradient_image(16, 16), src, "png")
        blocker = tmp_path / "blocker"
        blocker.write_text("a file")
        code = main(["run", "steam-capsule-pack", str(src), "-o", str(blocker / "out")])
        assert code == 1
        assert capsys.readouterr().err.startswith("error: ")

    def test_data_dir_that_is_a_file(self, tmp_path, capsys):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file")
        assert main(["--data-dir", str(blocker), "recipes", "list"]) == 1
        assert "error: " in capsys.readouterr().err


class TestWatchJobDeletedEarly:
    async def test_sources_settle_instead_of_rerunning(self, settings, tmp_path, queue):
        hot = tmp_path / "hot"
        hot.mkdir()
        settings.watch_config_path.write_text(
            f"watch:\n  - folder: {hot}\n    recipe: tiny-pack\n    output: {tmp_path / 'out'}\n",
            encoding="utf-8",
        )
        watcher = Watcher(settings, queue)
        watcher.reload_config()
        save_image(gradient_image(24, 24), hot / "drop.png", "png")
        watcher._scan_all()
        await asyncio.sleep(0.12)
        watcher._scan_all()
        (job_id,) = watcher._active_jobs
        await wait_status(queue, job_id, ("done",))
        queue.delete(job_id)  # before the watcher's next tick saw it finish
        watcher._check_active_jobs()
        assert not any((hot / INTAKE_DIR).glob("*/*"))  # nothing left to resubmit
        assert (hot / "failed" / "drop.png").is_file()


# ---------------------------------------------------------------------------
# 0.5.0
# ---------------------------------------------------------------------------

SKIPPING_RECIPE = """
name: jpg-to-png
input:
  formats: [jpg]
steps:
  - op: resize
    size: 16x16
  - op: convert
    format: png
output:
  structure: flat
  manifest: false
"""


class TestSkippedOriginalsAreSafe:
    async def test_path_job_never_replaces_a_file_the_recipe_skipped(
        self, queue, tmp_path, settings
    ):
        (settings.recipes_dir / "jpg-to-png.yaml").write_text(SKIPPING_RECIPE, encoding="utf-8")
        recipe = discover_recipes(recipe_dirs(settings))[0]["jpg-to-png"]
        photos = tmp_path / "photos"
        save_image(gradient_image(32, 32), photos / "logo.jpg", "jpg")
        save_image(gradient_image(40, 40), photos / "logo.png", "png")  # skipped: not a jpg
        original = (photos / "logo.png").read_bytes()
        files, _ = queue.collect_input_files([photos], recipe)
        job = queue.submit(recipe, files, output_dir=photos)
        await wait_status(queue, job["id"], ("done",))
        assert (photos / "logo.png").read_bytes() == original
        assert (photos / "logo-2.png").is_file()


class TestQueueOrder:
    async def test_jobs_from_the_same_second_run_first_in_first_out(
        self, settings, recipes_provider, tmp_path, monkeypatch
    ):
        import batchroom.queue.jobs as jobs_mod

        # random ids in the "wrong" order, as they are half of the time
        ids = iter(["j-20260101-ffffffffff", "j-20260101-0000000000"])
        monkeypatch.setattr(jobs_mod, "new_job_id", lambda: next(ids))
        # no workers: claim by hand to see the order
        q = JobQueue(settings, Database(settings.db_path), ModelManager(settings.models_dir),
                     EventBus(), recipes_provider)
        recipe = recipes_provider()["tiny-pack"]
        first = q.submit(recipe, make_inputs(tmp_path / "a", 3))["id"]
        second = q.submit(recipe, make_inputs(tmp_path / "b", 3))["id"]
        with q.db.connect() as conn:  # both in the same created_at second
            conn.execute("UPDATE jobs SET created_at='2026-01-01T00:00:00+00:00'")
        order = [q._claim_next()["job_id"] for _ in range(6)]
        assert order == [first] * 3 + [second] * 3
        assert [j["id"] for j in q.list_jobs()] == [second, first]

    async def test_delete_refuses_a_job_that_was_requeued_meanwhile(
        self, settings, recipes_provider, tmp_path
    ):
        q = JobQueue(settings, Database(settings.db_path), ModelManager(settings.models_dir),
                     EventBus(), recipes_provider)
        job = q.submit(recipe := recipes_provider()["tiny-pack"], make_inputs(tmp_path, 1))
        assert recipe
        real_get = q.get_job
        # delete() saw the job finished, then a retry re-queued it
        q.get_job = lambda job_id: {**real_get(job_id), "status": "done"}
        with pytest.raises(JobError, match="running"):
            q.delete(job["id"])
        q.get_job = real_get
        assert q.get_job(job["id"])["status"] == "queued"


class TestWatchConfigEncoding:
    def test_non_utf8_watch_yaml_is_a_problem_not_a_crash(self, tmp_path):
        cfg = tmp_path / "watch.yaml"
        cfg.write_bytes("watch:\n  - folder: /r\xe9pertoire\n    recipe: r\n".encode("latin-1"))
        result = load_watch_config(cfg)
        assert result.entries == []
        assert any("unreadable" in p for p in result.problems)


class TestNestedWatchFolders:
    async def test_parent_folder_leaves_a_nested_watch_folder_alone(
        self, settings, tmp_path, queue
    ):
        parent = tmp_path / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        settings.watch_config_path.write_text(
            f"watch:\n  - folder: {parent}\n    recipe: tiny-pack\n"
            f"  - folder: {child}\n    recipe: tiny-pack\n",
            encoding="utf-8",
        )
        watcher = Watcher(settings, queue)
        watcher.reload_config()
        save_image(gradient_image(16, 16), parent / "top.png", "png")
        save_image(gradient_image(16, 16), child / "inner.png", "png")
        (child / "done").mkdir()
        save_image(gradient_image(16, 16), child / "done" / "old.png", "png")
        by_folder = {e.folder: {p.name for p in watcher._candidates(e)} for e in watcher.entries}
        assert by_folder[parent] == {"top.png"}
        assert by_folder[child] == {"inner.png"}

    async def test_the_same_folder_twice_is_a_problem(self, settings, tmp_path, queue):
        hot = tmp_path / "hot"
        hot.mkdir()
        settings.watch_config_path.write_text(
            f"watch:\n  - folder: {hot}\n    recipe: tiny-pack\n"
            f"  - folder: {hot}/\n    recipe: tiny-pack\n",
            encoding="utf-8",
        )
        watcher = Watcher(settings, queue)
        watcher.reload_config()
        assert len(watcher.entries) == 1
        assert any("more than once" in p for p in watcher.problems)


def test_cli_models_rm_unknown_id_fails(tmp_path, capsys):
    assert main(["--data-dir", str(tmp_path / "data"), "models", "rm", "nope"]) == 1
    assert "unknown model" in capsys.readouterr().err
