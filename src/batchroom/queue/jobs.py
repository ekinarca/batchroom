"""The job queue: submission, dispatch, cancellation, resume, finalize.

Design (plan §8.1): an in-process asyncio dispatcher feeds a thread pool —
onnxruntime and Pillow release the GIL for the heavy parts, so threads give
real parallelism without per-process model copies. Item state persists in
SQLite after every transition; a crash mid-batch resumes with only the
unfinished items re-running.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from ..config import Settings
from ..core.errors import CancelledError, JobError
from ..core.image_io import READ_EXTENSIONS
from ..core.inputs import is_hidden_file_name, walk_files
from ..core.manifest import MANIFEST_NAME, build_manifest, utc_now_iso, write_manifest
from ..core.pipeline import ItemResult, JobNames, OutputRecord, process_item
from ..core.recipe import Recipe
from ..models.manager import ModelManager
from ..ops.base import PipelineContext
from .db import COUNTERS_FROM_ITEMS, Database, row_to_item, row_to_job
from .events import EventBus

log = logging.getLogger("batchroom.queue")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_job_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    # 40 random bits: a busy watch folder (thousands of jobs a day) never
    # collides with an existing id — 24 bits did, at ~3% a day per 1000 jobs
    return f"j-{stamp}-{secrets.token_hex(5)}"


class JobQueue:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        models: ModelManager,
        events: EventBus,
        recipes_provider,
        notify=None,  # callable(job: dict, manifest: dict) — e.g. email (Pro)
    ):
        self.settings = settings
        self.db = db
        self.models = models
        self.events = events
        self.recipes_provider = recipes_provider  # () -> dict[str, Recipe]
        self.notify = notify
        self._pool = ThreadPoolExecutor(
            max_workers=settings.workers, thread_name_prefix="batchroom-worker"
        )
        self._sem: asyncio.Semaphore | None = None
        self._wake: asyncio.Event | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._cancelled: set[str] = set()
        self._names: dict[str, JobNames] = {}
        self._names_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._webhook_tasks: set[asyncio.Task] = set()  # strong refs: no mid-flight GC

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.events.bind_loop(self._loop)
        self._sem = asyncio.Semaphore(self.settings.workers)
        self._wake = asyncio.Event()
        self._running = True
        self._recover()
        self._task = asyncio.create_task(self._dispatch_loop(), name="batchroom-dispatcher")

    async def stop(self) -> None:
        self._running = False
        if self._wake:
            self._wake.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._webhook_tasks:  # let deliveries already in flight land
            await asyncio.wait(set(self._webhook_tasks), timeout=3.0)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _recover(self) -> None:
        """Called on boot: re-queue items that were mid-flight when we died,
        and settle jobs the crash caught between two states."""
        now = _now()
        with self.db.connect() as conn:
            crashed = conn.execute(
                "UPDATE items SET status='pending', started_at=NULL WHERE status='running' "
                "RETURNING job_id"
            ).fetchall()
            # A cancel requested before the crash still stands: the claim query
            # skips these jobs, so their pending items would wait forever.
            for row in conn.execute(
                "SELECT id FROM jobs WHERE cancel_requested=1 AND finished_at IS NULL"
            ).fetchall():
                self._cancel_pending(conn, row["id"], now)
            # Every item settled but the job never flipped (crash between the
            # last item and finalize): finalize it now, or it shows 'running' forever.
            unsettled = conn.execute(
                "SELECT j.id FROM jobs j WHERE j.finished_at IS NULL AND NOT EXISTS ("
                " SELECT 1 FROM items i WHERE i.job_id = j.id"
                " AND i.status IN ('pending', 'running'))"
            ).fetchall()
        if crashed:
            log.info("recovered %d in-flight item(s) from a previous run", len(crashed))
        for row in unsettled:
            log.info("finalizing job %s left unsettled by a previous run", row["id"])
            self._maybe_finalize(row["id"])

    # ------------------------------------------------------------------
    # submission
    # ------------------------------------------------------------------
    def collect_input_files(
        self, paths: list[Path], recipe: Recipe, *, exclude_dirs: tuple[Path, ...] = ()
    ) -> tuple[list[Path], int]:
        """Expand dirs, filter to files the recipe accepts. Returns (files, skipped).

        Hidden and NAS system folders are never walked, nor ``exclude_dirs``
        (an output folder inside the input tree must not be re-ingested)."""
        files: list[Path] = []
        skipped = 0
        for path in paths:
            if path.is_dir():
                candidates = list(walk_files(path, exclude_dirs=exclude_dirs))
            else:
                candidates = [path]
            for candidate in candidates:
                if is_hidden_file_name(candidate.name):
                    continue
                ext = candidate.suffix.lower()
                if ext not in READ_EXTENSIONS:
                    skipped += 1
                    continue
                if not recipe.accepts_extension(ext):
                    skipped += 1
                    continue
                files.append(candidate)
        return files, skipped

    def submit(
        self,
        recipe: Recipe,
        files: list[Path],
        *,
        source: str = "api",
        output_dir: Path | None = None,
        webhook_url: str | None = None,
        skipped: int = 0,
        user_id: int | None = None,
        priority: int = 0,
    ) -> dict:
        return self.submit_with_id(
            new_job_id(), recipe, files, source=source, output_dir=output_dir,
            webhook_url=webhook_url, skipped=skipped, user_id=user_id, priority=priority,
        )

    def submit_with_id(
        self,
        job_id: str,
        recipe: Recipe,
        files: list[Path],
        *,
        source: str = "api",
        output_dir: Path | None = None,
        webhook_url: str | None = None,
        skipped: int = 0,
        user_id: int | None = None,
        priority: int = 0,
    ) -> dict:
        if not files:
            raise JobError(
                "no processable input files",
                hint="check the recipe's input.formats against your files",
            )
        out_dir = output_dir or (self.settings.jobs_dir / job_id / "out")
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # unwritable/unmounted share: a clean error, not a crash
            raise JobError(
                f"cannot create output folder {out_dir}: {exc.strerror or exc}",
                hint="check that the output path exists and is writable",
            ) from exc
        now = _now()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, recipe, status, source, output_dir, webhook_url,"
                " created_at, total, skipped, user_id, priority)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, recipe.name, "queued", source, str(out_dir), webhook_url, now,
                 len(files), skipped, user_id, priority),
            )
            conn.executemany(
                "INSERT INTO items (job_id, idx, source_path, source_name) VALUES (?,?,?,?)",
                [(job_id, i, str(p), p.name) for i, p in enumerate(files)],
            )
        log.info("job %s submitted: recipe=%s files=%d", job_id, recipe.name, len(files))
        self._publish_job(job_id)
        self._nudge()
        return self.get_job(job_id)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def get_job(self, job_id: str) -> dict:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobError(f"job '{job_id}' not found")
        return row_to_job(row)

    def list_jobs(
        self, *, limit: int = 50, offset: int = 0, user_id: int | None = None
    ) -> list[dict]:
        """List jobs, newest first. ``user_id`` filters to one owner (members)."""
        with self.db.connect() as conn:
            if user_id is None:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE user_id = ?"
                    " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                    (user_id, limit, offset),
                ).fetchall()
        return [row_to_job(r) for r in rows]

    def get_items(self, job_id: str) -> list[dict]:
        self.get_job(job_id)  # 404 if missing
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM items WHERE job_id=? ORDER BY idx", (job_id,)
            ).fetchall()
        return [row_to_item(r) for r in rows]

    def queue_depth(self) -> dict:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT sum(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,"
                " sum(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running FROM items"
            ).fetchone()
        return {"pending": row["pending"] or 0, "running": row["running"] or 0}

    # ------------------------------------------------------------------
    # cancel / delete
    # ------------------------------------------------------------------
    def cancel(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if job["status"] in ("done", "cancelled"):
            return job
        self._cancelled.add(job_id)
        with self.db.connect() as conn:
            conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
            self._cancel_pending(conn, job_id, _now())
        self._maybe_finalize(job_id)
        self._publish_job(job_id)
        return self.get_job(job_id)

    @staticmethod
    def _cancel_pending(conn, job_id: str, now: str) -> int:
        """Cancel a job's pending items and count them on the job row, so
        done+failed+cancelled always adds up to total."""
        count = conn.execute(
            "UPDATE items SET status='cancelled', error='cancelled', finished_at=? "
            "WHERE job_id=? AND status='pending'",
            (now, job_id),
        ).rowcount
        if count:
            conn.execute(
                "UPDATE jobs SET cancelled = cancelled + ? WHERE id=?", (count, job_id)
            )
        return count

    def retry(self, job_id: str) -> dict:
        """Re-queue a finished job's failed and cancelled items.

        Only items whose source file still exists come back; done items and
        their outputs are untouched. Watch-folder sources have already been
        moved to failed/, so those jobs retry by dropping the files again."""
        job = self.get_job(job_id)
        if job["status"] not in ("done", "cancelled"):
            raise JobError("job is still running", hint="wait for it to finish first")
        if job["source"] == "watch":
            raise JobError(
                "watch-folder jobs cannot be retried here",
                hint="move the files from failed/ back into the watched folder",
            )
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT idx, status, source_path FROM items"
                " WHERE job_id=? AND status IN ('failed', 'cancelled')",
                (job_id,),
            ).fetchall()
            again = [r for r in rows if Path(r["source_path"]).is_file()]
            if not again:
                raise JobError(
                    "nothing to retry",
                    hint="no failed or cancelled items whose source file still exists",
                )
            failed = sum(1 for r in again if r["status"] == "failed")
            conn.executemany(
                "UPDATE items SET status='pending', error=NULL, result_json=NULL,"
                " started_at=NULL, finished_at=NULL WHERE job_id=? AND idx=?",
                [(job_id, r["idx"]) for r in again],
            )
            conn.execute(
                "UPDATE jobs SET status='queued', finished_at=NULL, cancel_requested=0,"
                " error=NULL, failed=max(0, failed - ?), cancelled=max(0, cancelled - ?)"
                " WHERE id=?",
                (failed, len(again) - failed, job_id),
            )
        self._cancelled.discard(job_id)
        with self._names_lock:
            self._names.pop(job_id, None)  # rebuilt from the done items' outputs
        log.info("job %s: %d item(s) re-queued", job_id, len(again))
        self._publish_job(job_id)
        self._nudge()
        return self.get_job(job_id)

    def delete(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job["status"] in ("queued", "running"):
            raise JobError("cannot delete a running job", hint="cancel it first")
        with self.db.connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        # Only remove files we created: the job's own directory under jobs_dir.
        job_dir = (self.settings.jobs_dir / job_id).resolve()
        jobs_root = self.settings.jobs_dir.resolve()
        if job_dir.is_dir() and job_dir != jobs_root and job_dir.is_relative_to(jobs_root):
            shutil.rmtree(job_dir, ignore_errors=True)
        # API routes call this from a worker thread: never touch the loop's queues directly
        self.events.publish_from_thread("job.deleted", {"job_id": job_id})

    # ------------------------------------------------------------------
    # dispatcher
    # ------------------------------------------------------------------
    def _nudge(self) -> None:
        if self._wake is not None and self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._wake.set)

    async def _dispatch_loop(self) -> None:
        assert self._wake is not None
        while self._running:
            try:
                progressed = await self._dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient failure (database locked, disk full...) must not
                # stop the queue for good: log it and try again shortly.
                log.exception("dispatcher error; retrying")
                progressed = False
                await asyncio.sleep(1.0)
            if not progressed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                self._wake.clear()

    async def _dispatch_once(self) -> bool:
        assert self._sem is not None and self._loop is not None
        await self._sem.acquire()
        try:
            claimed = self._claim_next()
            if claimed is None:
                self._sem.release()
                return False
            future = self._loop.run_in_executor(self._pool, self._run_item, claimed)
        except BaseException:
            self._sem.release()
            raise

        def _done(_f) -> None:  # runs in the loop thread (asyncio future callback)
            self._sem.release()
            if self._wake is not None:
                self._wake.set()

        future.add_done_callback(_done)
        return True

    def _claim_next(self) -> dict | None:
        with self.db.connect() as conn:
            # Select + mark atomically: a cancel landing in between must not
            # see its pending item flipped back to 'running'.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT i.job_id, i.idx, i.source_path, i.source_name, j.recipe, j.output_dir,"
                " j.started_at AS job_started "
                "FROM items i JOIN jobs j ON j.id = i.job_id "
                "WHERE i.status='pending' AND j.cancel_requested=0 "
                "ORDER BY j.priority DESC, j.created_at, i.idx LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = _now()
            conn.execute(
                "UPDATE items SET status='running', started_at=?"
                " WHERE job_id=? AND idx=? AND status='pending'",
                (now, row["job_id"], row["idx"]),
            )
            conn.execute(
                "UPDATE jobs SET status='running', started_at=COALESCE(started_at, ?) WHERE id=?",
                (now, row["job_id"]),
            )
        return dict(row)

    def _job_names(self, job_id: str) -> JobNames:
        with self._names_lock:
            names = self._names.get(job_id)
            if names is None:
                taken: set[str] = set()
                with self.db.connect() as conn:
                    rows = conn.execute(
                        "SELECT result_json FROM items WHERE job_id=? AND result_json IS NOT NULL",
                        (job_id,),
                    ).fetchall()
                for row in rows:
                    try:
                        for output in json.loads(row["result_json"]).get("outputs", []):
                            taken.add(output["path"])
                    except (ValueError, KeyError):
                        continue
                names = JobNames(taken)
                self._names[job_id] = names
            return names

    # ------------------------------------------------------------------
    # worker body (runs in thread pool)
    # ------------------------------------------------------------------
    def _run_item(self, claimed: dict) -> None:
        job_id, idx = claimed["job_id"], claimed["idx"]
        try:
            result = self._execute_item(claimed)
        except Exception as exc:  # defensive: never lose an item silently
            log.exception("worker crashed on %s[%d]", job_id, idx)
            result = ItemResult(
                source=claimed["source_name"], status="failed", error=f"worker crash: {exc}"
            )
        try:
            self._record_result(job_id, idx, result)
        except Exception:  # e.g. disk full; the item re-runs after a restart
            log.exception("cannot record the result of %s[%d]", job_id, idx)

    def _execute_item(self, claimed: dict) -> ItemResult:
        job_id, idx = claimed["job_id"], claimed["idx"]
        recipes = self.recipes_provider()
        recipe = recipes.get(claimed["recipe"])
        if recipe is None:
            return ItemResult(
                source=claimed["source_name"],
                status="failed",
                error=f"recipe '{claimed['recipe']}' no longer exists",
            )

        def cancel_check() -> None:
            if job_id in self._cancelled:
                raise CancelledError()

        def progress(message: str) -> None:
            self.events.publish_from_thread(
                "item.progress", {"job_id": job_id, "idx": idx, "message": message}
            )

        # {date} is the job's start date for every item, even across midnight
        started = claimed.get("job_started") or _now()
        ctx = PipelineContext(
            recipe_name=recipe.name,
            job_id=job_id,
            date=started[:10].replace("-", ""),
            settings=self.settings,
            models=self.models,
            cancel_check=cancel_check,
            progress=progress,
        )
        thumbs_dir = self.settings.jobs_dir / job_id / "thumbs"
        return process_item(
            Path(claimed["source_path"]),
            recipe,
            Path(claimed["output_dir"]),
            ctx,
            index=idx,
            names=self._job_names(job_id),
            thumbs_dir=thumbs_dir,
        )

    def _record_result(self, job_id: str, idx: int, result: ItemResult) -> None:
        column = {"done": "done", "failed": "failed", "cancelled": "cancelled"}[result.status]
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE items SET status=?, error=?, result_json=?, finished_at=? "
                "WHERE job_id=? AND idx=?",
                (result.status, result.error, json.dumps(result.to_dict(), ensure_ascii=False),
                 _now(), job_id, idx),
            )
            conn.execute(
                f"UPDATE jobs SET {column} = {column} + 1 WHERE id=?", (job_id,)
            )
        self.events.publish_from_thread(
            "item.finished",
            {"job_id": job_id, "idx": idx, "status": result.status, "error": result.error},
        )
        self._maybe_finalize(job_id)

    # ------------------------------------------------------------------
    # finalize
    # ------------------------------------------------------------------
    def _maybe_finalize(self, job_id: str) -> None:
        with self.db.connect() as conn:
            remaining = conn.execute(
                "SELECT count(*) AS n FROM items WHERE job_id=? AND status IN"
                " ('pending','running')",
                (job_id,),
            ).fetchone()["n"]
            if remaining:
                return
            job_row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job_row is None or job_row["finished_at"] is not None:
                return
            final_status = "cancelled" if job_row["cancel_requested"] else "done"
            item_rows = conn.execute(
                "SELECT * FROM items WHERE job_id=? ORDER BY idx", (job_id,)
            ).fetchall()

        # Write the manifest BEFORE flipping status: a 'done' job always has
        # its manifest on disk (unless the recipe opted out with
        # output.manifest: false). Concurrent workers may both reach this
        # point; the guarded UPDATE below lets exactly one of them finalize.
        finished_at = utc_now_iso()
        recipe = self.recipes_provider().get(job_row["recipe"])
        manifest = self._manifest_from_rows(job_row, item_rows, finished_at, recipe)
        if recipe is None or recipe.output.manifest:
            try:
                write_manifest(Path(job_row["output_dir"]), manifest)
            except OSError as exc:
                log.error("cannot write manifest for %s: %s", job_id, exc)

        with self.db.connect() as conn:
            # Counters are re-derived from the items: the final record is exact
            # whatever path each item took (worker, cancel, crash recovery).
            cursor = conn.execute(
                f"UPDATE jobs SET status=?, finished_at=?, {COUNTERS_FROM_ITEMS}"
                " WHERE id=? AND finished_at IS NULL",
                (final_status, finished_at, job_id),
            )
            if cursor.rowcount == 0:
                return  # another worker finalized first

        job = self.get_job(job_id)
        with self._names_lock:
            self._names.pop(job_id, None)
        self._cancelled.discard(job_id)
        log.info(
            "job %s finished: %s (done=%d failed=%d cancelled=%d)",
            job_id, job["status"], manifest["counts"]["done"],
            manifest["counts"]["failed"], manifest["counts"]["cancelled"],
        )
        self._publish_job(job_id)
        if job.get("webhook_url"):
            self._fire_webhook(job["webhook_url"], manifest)
        if self.notify is not None:
            try:
                self.notify(self.get_job(job_id), manifest)
            except Exception:  # notifications must never affect jobs
                log.exception("completion notification failed for %s", job_id)

    def _manifest_from_rows(self, job_row, item_rows, finished_at, recipe=None) -> dict:
        if recipe is None:
            recipe = self.recipes_provider().get(job_row["recipe"])
        return build_manifest(
            job_id=job_row["id"],
            recipe_name=job_row["recipe"],
            recipe_version=recipe.version if recipe else 1,
            output_structure=recipe.output.structure if recipe else "per-preset-folder",
            started_at=job_row["started_at"] or job_row["created_at"],
            finished_at=finished_at,
            results=[self._result_from_row(row) for row in item_rows],
        )

    def manifest_for(self, job_id: str) -> dict:
        """The job's manifest rebuilt from the database — always this job's
        own record, even when several jobs share one output folder."""
        with self.db.connect() as conn:
            job_row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job_row is None:
                raise JobError(f"job '{job_id}' not found")
            item_rows = conn.execute(
                "SELECT * FROM items WHERE job_id=? ORDER BY idx", (job_id,)
            ).fetchall()
        manifest = self._manifest_from_rows(job_row, item_rows, job_row["finished_at"])
        manifest["finished_at"] = job_row["finished_at"]  # None while still running
        return manifest

    def wants_manifest_file(self, recipe_name: str) -> bool:
        recipe = self.recipes_provider().get(recipe_name)
        return recipe is None or recipe.output.manifest

    @staticmethod
    def _result_from_row(row) -> ItemResult:
        if row["result_json"]:
            try:
                data = json.loads(row["result_json"])
                return ItemResult(
                    source=data.get("source", row["source_name"]),
                    status=data.get("status", row["status"]),
                    error=data.get("error"),
                    outputs=[OutputRecord(**o) for o in data.get("outputs", [])],
                    duration_ms=data.get("duration_ms", 0),
                    steps=data.get("steps", []),
                )
            except (ValueError, TypeError, KeyError):
                pass
        return ItemResult(
            source=row["source_name"], status=row["status"] or "failed", error=row["error"]
        )

    def _fire_webhook(self, url: str, manifest: dict) -> None:
        from .webhooks import deliver_webhook  # local import avoids cycle

        loop = self._loop
        if loop is None or not loop.is_running():
            return

        def _start() -> None:  # runs in the loop thread
            task = loop.create_task(deliver_webhook(url, manifest))
            self._webhook_tasks.add(task)
            task.add_done_callback(self._webhook_tasks.discard)

        with contextlib.suppress(RuntimeError):  # loop closing during shutdown
            loop.call_soon_threadsafe(_start)

    # ------------------------------------------------------------------
    def _publish_job(self, job_id: str) -> None:
        try:
            job = self.get_job(job_id)
        except JobError:
            return
        if self._loop is not None and self._loop.is_running():
            self.events.publish_from_thread("job.updated", {"job": job})

    @property
    def manifest_name(self) -> str:
        return MANIFEST_NAME
