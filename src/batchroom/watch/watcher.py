"""Watch-folder service — the NAS/homelab heart of Batchroom.

Deliberately polling-based: inotify/FSEvents do not work reliably on the
network mounts (NFS/SMB) this feature exists for. A file is picked up only
after its size+mtime have been stable across consecutive scans *and* it is
older than a settle window, so half-copied files are never processed.

Flow per watched folder::

    <folder>/                  dropped files appear here
    <folder>/.intake/<job>/    ready files move here (atomic rename), job submitted
    <folder>/done/             sources of successful items end up here
    <folder>/failed/           sources of failed items end up here
    <output>/                  recipe outputs + manifest.json (default <folder>/processed)

Configuration lives in ``<data_dir>/watch.yaml``:

.. code-block:: yaml

    watch:
      - folder: /watch/products
        recipe: ecom-packshot
        output: /output/products   # optional
        webhook: https://...       # optional
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import Settings
from ..core.errors import BatchroomError, JobError
from ..core.image_io import READ_EXTENSIONS
from ..queue.jobs import JobQueue

log = logging.getLogger("batchroom.watch")

INTAKE_DIR = ".intake"
DONE_DIR = "done"
FAILED_DIR = "failed"
_RESERVED = {INTAKE_DIR, DONE_DIR, FAILED_DIR}


@dataclass
class WatchEntry:
    folder: Path
    recipe: str
    output: Path
    webhook: str | None = None

    @property
    def intake_root(self) -> Path:
        return self.folder / INTAKE_DIR


@dataclass
class _Snapshot:
    size: int
    mtime: float
    seen_at: float


@dataclass
class WatchConfigResult:
    entries: list[WatchEntry] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def load_watch_config(path: Path) -> WatchConfigResult:
    result = WatchConfigResult()
    if not path.is_file():
        return result
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        result.problems.append(f"watch.yaml unreadable: {exc}")
        return result
    entries = data.get("watch")
    if entries is None:
        result.problems.append("watch.yaml has no top-level 'watch:' list")
        return result
    if not isinstance(entries, list):
        result.problems.append("'watch' must be a list")
        return result
    for i, raw in enumerate(entries):
        if not isinstance(raw, dict) or "folder" not in raw or "recipe" not in raw:
            result.problems.append(f"watch[{i}]: needs 'folder' and 'recipe'")
            continue
        unknown = set(raw) - {"folder", "recipe", "output", "webhook"}
        if unknown:
            result.problems.append(f"watch[{i}]: unknown key(s) {sorted(unknown)}")
            continue
        folder = Path(str(raw["folder"])).expanduser()
        output = Path(str(raw.get("output") or folder / "processed")).expanduser()
        result.entries.append(
            WatchEntry(
                folder=folder,
                recipe=str(raw["recipe"]),
                output=output,
                webhook=str(raw["webhook"]) if raw.get("webhook") else None,
            )
        )
    return result


class Watcher:
    def __init__(self, settings: Settings, queue: JobQueue):
        self.settings = settings
        self.queue = queue
        self._snapshots: dict[Path, _Snapshot] = {}
        self._active_jobs: dict[str, WatchEntry] = {}
        self._task: asyncio.Task | None = None
        self._running = False
        self.entries: list[WatchEntry] = []
        self.problems: list[str] = []

    # ------------------------------------------------------------------
    async def start(self) -> None:
        self.reload_config()
        self._running = True
        self._recover_orphans()
        self._task = asyncio.create_task(self._loop(), name="batchroom-watcher")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    def reload_config(self) -> None:
        result = load_watch_config(self.settings.watch_config_path)
        self.entries = result.entries
        self.problems = result.problems
        for problem in result.problems:
            log.warning("watch config: %s", problem)
        recipes = self.queue.recipes_provider()
        for entry in list(self.entries):
            if entry.recipe not in recipes:
                self.problems.append(
                    f"watch folder {entry.folder}: unknown recipe '{entry.recipe}'"
                )
                log.warning("watch folder %s: unknown recipe '%s'", entry.folder, entry.recipe)
                self.entries.remove(entry)

    def status(self) -> dict:
        return {
            "enabled": self.settings.watch_enabled,
            "folders": [
                {
                    "folder": str(e.folder),
                    "recipe": e.recipe,
                    "output": str(e.output),
                    "exists": e.folder.is_dir(),
                }
                for e in self.entries
            ],
            "problems": self.problems,
            "active_jobs": len(self._active_jobs),
        }

    # ------------------------------------------------------------------
    def _recover_orphans(self) -> None:
        """Intake dirs left behind by a crash: resubmit those with no live job."""
        for entry in self.entries:
            intake_root = entry.intake_root
            if not intake_root.is_dir():
                continue
            for job_dir in sorted(p for p in intake_root.iterdir() if p.is_dir()):
                try:
                    job = self.queue.get_job(job_dir.name)
                except JobError:
                    job = None
                if job is not None and job["status"] in ("queued", "running"):
                    self._active_jobs[job_dir.name] = entry
                    continue
                if job is not None and job["status"] in ("done", "cancelled"):
                    self._disposition(job_dir.name, entry)
                    continue
                files = [p for p in job_dir.iterdir() if p.is_file()]
                if files:
                    log.info("resubmitting %d orphaned file(s) from %s", len(files), job_dir)
                    self._submit(entry, files, reuse_dir=job_dir)
                else:
                    job_dir.rmdir()

    async def _loop(self) -> None:
        while self._running:
            try:
                self._scan_all()
                self._check_active_jobs()
            except Exception:  # pragma: no cover - defensive
                log.exception("watcher scan failed")
            await asyncio.sleep(self.settings.watch_poll_seconds)

    # ------------------------------------------------------------------
    def _scan_all(self) -> None:
        import time

        now = time.time()
        for entry in self.entries:
            if not entry.folder.is_dir():
                continue
            ready: list[Path] = []
            for path in self._candidates(entry):
                try:
                    stat = path.stat()
                except OSError:
                    self._snapshots.pop(path, None)
                    continue
                snap = self._snapshots.get(path)
                if (
                    snap
                    and snap.size == stat.st_size
                    and snap.mtime == stat.st_mtime
                    and now - snap.seen_at >= self.settings.watch_stable_seconds
                ):
                    ready.append(path)
                elif snap is None or snap.size != stat.st_size or snap.mtime != stat.st_mtime:
                    self._snapshots[path] = _Snapshot(stat.st_size, stat.st_mtime, now)
            if ready:
                self._submit(entry, ready)
                for path in ready:
                    self._snapshots.pop(path, None)

    def _candidates(self, entry: WatchEntry):
        output_resolved = entry.output.resolve()
        for path in sorted(entry.folder.rglob("*")):
            if not path.is_file():
                continue
            if path.name.startswith(".") or path.name.endswith(".part"):
                continue
            relative = path.relative_to(entry.folder)
            head = relative.parts[0] if relative.parts else ""
            if head in _RESERVED:
                continue
            try:
                if path.resolve().is_relative_to(output_resolved):
                    continue  # never re-ingest our own outputs
            except OSError:
                continue
            if path.suffix.lower() not in READ_EXTENSIONS:
                continue
            yield path

    def _submit(self, entry: WatchEntry, files: list[Path], reuse_dir: Path | None = None) -> None:
        recipes = self.queue.recipes_provider()
        recipe = recipes.get(entry.recipe)
        if recipe is None:
            log.warning("watch: recipe '%s' vanished, skipping pickup", entry.recipe)
            return
        from ..queue.jobs import new_job_id

        job_id = reuse_dir.name if reuse_dir is not None else new_job_id()
        intake = reuse_dir if reuse_dir is not None else entry.intake_root / job_id
        intake.mkdir(parents=True, exist_ok=True)
        moved: list[Path] = []
        if reuse_dir is None:
            for path in files:
                target = intake / path.name
                n = 2
                while target.exists():
                    target = intake / f"{path.stem}-{n}{path.suffix}"
                    n += 1
                try:
                    path.rename(target)  # atomic on the same volume
                    moved.append(target)
                except OSError as exc:
                    log.warning("watch: cannot move %s into intake: %s", path, exc)
        else:
            moved = files
        accepted, skipped = self.queue.collect_input_files(moved, recipe)
        if not accepted:
            for path in moved:
                self._move_to(path, entry.folder / FAILED_DIR)
            self._cleanup_intake(intake)
            return
        try:
            job = self.queue.submit_with_id(
                job_id,
                recipe,
                accepted,
                source="watch",
                output_dir=entry.output,
                webhook_url=entry.webhook,
                skipped=skipped,
            )
        except BatchroomError as exc:
            log.error("watch: submit failed: %s", exc)
            for path in moved:
                self._move_to(path, entry.folder / FAILED_DIR)
            self._cleanup_intake(intake)
            return
        self._active_jobs[job["id"]] = entry
        log.info("watch: %s picked up %d file(s) -> job %s", entry.folder, len(accepted), job["id"])

    # ------------------------------------------------------------------
    def _check_active_jobs(self) -> None:
        for job_id in list(self._active_jobs):
            try:
                job = self.queue.get_job(job_id)
            except JobError:
                self._active_jobs.pop(job_id, None)
                continue
            if job["status"] in ("done", "cancelled"):
                entry = self._active_jobs.pop(job_id)
                self._disposition(job_id, entry)

    def _disposition(self, job_id: str, entry: WatchEntry) -> None:
        """Move intake sources to done/ or failed/ according to item results."""
        intake = entry.intake_root / job_id
        if not intake.is_dir():
            return
        try:
            items = self.queue.get_items(job_id)
        except JobError:
            items = []
        status_by_path = {Path(i["source_path"]).name: i["status"] for i in items}
        for path in sorted(p for p in intake.iterdir() if p.is_file()):
            status = status_by_path.get(path.name, "failed")
            target_dir = entry.folder / (DONE_DIR if status == "done" else FAILED_DIR)
            self._move_to(path, target_dir)
        self._cleanup_intake(intake)

    @staticmethod
    def _move_to(path: Path, target_dir: Path) -> None:
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / path.name
            n = 2
            while target.exists():
                target = target_dir / f"{path.stem}-{n}{path.suffix}"
                n += 1
            path.rename(target)
        except OSError as exc:
            log.warning("watch: cannot move %s -> %s: %s", path, target_dir, exc)

    @staticmethod
    def _cleanup_intake(intake: Path) -> None:
        try:
            next(intake.iterdir())
        except StopIteration:
            intake.rmdir()
        except OSError:
            pass
