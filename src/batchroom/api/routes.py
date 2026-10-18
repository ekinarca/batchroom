"""REST API routes.

Conventions: JSON in/out, errors as ``{"detail": ..., "hint": ...}``,
async job submission returning 202-style job objects immediately.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.datastructures import UploadFile

from .. import __version__
from ..config import Settings
from ..core.errors import BatchroomError, JobError, ModelError, RecipeError
from ..core.image_io import FORMATS, format_available
from ..core.inputs import safe_upload_name
from ..core.manifest import MANIFEST_NAME
from ..core.recipe import load_recipe_text
from ..i18n import available_languages, load_strings
from ..models.registry import CATALOG
from ..ops.base import all_ops
from ..queue.events import sse_format
from ..queue.jobs import JobQueue, new_job_id
from .archive import stream_zip
from .auth import actor_of, features, require_admin

router = APIRouter()
open_router = APIRouter()  # unauthenticated: i18n only (login screen needs strings)

MAX_UPLOAD_FILES = 10000  # per request; matches the JSON mode's path limit


def _clamp_priority(request: Request, value) -> int:
    """Priority is a Pro feature; without it every job runs at 0 (FIFO)."""
    try:
        priority = int(value)
    except (TypeError, ValueError):
        return 0
    if "priority_queue" not in features(request.app):
        return 0
    return max(0, min(10, priority))


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _queue(request: Request) -> JobQueue:
    return request.app.state.queue


def _error(exc: BatchroomError, status: int = 400) -> HTTPException:
    detail = {"detail": exc.message}
    if exc.hint:
        detail["hint"] = exc.hint
    return HTTPException(status_code=status, detail=detail)


def _webhook_or_400(url: str | None) -> str | None:
    """Reject unusable webhook targets at submit time, not silently at delivery."""
    url = (url or "").strip()
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(status_code=400, detail="webhook_url must be an http(s) URL")
    return url


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------


@router.get("/system")
def system_info(request: Request) -> dict:
    settings = _settings(request)
    queue = _queue(request)
    watcher = request.app.state.watcher
    import time

    return {
        "app": "batchroom",
        "version": __version__,
        "workers": settings.workers,
        "queue": queue.queue_depth(),
        "uptime_seconds": int(time.time() - request.app.state.started_at),
        "auth_required": bool(settings.api_key),
        "formats": {
            fmt: format_available(fmt) for fmt in sorted(set(FORMATS) - {"jpeg"})
        },
        "watch": watcher.status(),
        "ops": [
            {"name": op.name, "summary": op.summary} for op in all_ops().values()
        ],
        "license": request.app.state.license_info.to_public_dict(),
    }


# ---------------------------------------------------------------------------
# i18n (open: the login screen localizes before any auth exists)
# ---------------------------------------------------------------------------


@open_router.get("/i18n")
def i18n_languages() -> dict:
    return {"languages": available_languages()}


@open_router.get("/i18n/{lang}")
def i18n_strings(lang: str) -> dict:
    return load_strings(lang)


# ---------------------------------------------------------------------------
# recipes
# ---------------------------------------------------------------------------


class RecipeBody(BaseModel):
    text: str = Field(min_length=1, max_length=65536)


@router.get("/recipes")
def list_recipes(request: Request) -> dict:
    store = request.app.state.recipes
    recipes = store.get(force=True)
    return {
        "recipes": [r.summary() for r in recipes.values()],
        "problems": store.problems,
    }


@router.get("/recipes/{name}")
def get_recipe(request: Request, name: str) -> dict:
    recipes = request.app.state.recipes.get()
    recipe = recipes.get(name)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"recipe '{name}' not found")
    return {**recipe.summary(), "source": recipe.source_text}


@router.post("/recipes/validate")
def validate_recipe(body: RecipeBody) -> dict:
    try:
        recipe = load_recipe_text(body.text)
    except RecipeError as exc:
        return {"valid": False, "error": exc.message, "hint": exc.hint}
    return {"valid": True, "summary": recipe.summary()}


def _user_recipe_path(request: Request, name: str) -> Path | None:
    """The file in the user recipe dir that currently defines ``name``.

    Its filename may differ from the recipe name (dropped in by hand); edits
    must go back to that file, or the old file keeps shadowing them."""
    recipe = request.app.state.recipes.get(force=True).get(name)
    if recipe is None or recipe.path is None:
        return None
    user_dir = _settings(request).recipes_dir.resolve()
    path = recipe.path.resolve()
    return path if path.parent == user_dir else None


@router.post("/recipes", status_code=201)
def save_recipe(request: Request, body: RecipeBody) -> dict:
    settings = _settings(request)
    try:
        recipe = load_recipe_text(body.text)
    except RecipeError as exc:
        raise _error(exc) from exc
    path = _user_recipe_path(request, recipe.name) or settings.recipes_dir / f"{recipe.name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")  # discovery only globs *.yaml / *.yml
    tmp.write_text(body.text, encoding="utf-8")
    tmp.replace(path)
    request.app.state.recipes.get(force=True)
    return {"saved": recipe.name, "path": str(path)}


@router.delete("/recipes/{name}")
def delete_recipe(request: Request, name: str) -> dict:
    settings = _settings(request)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", name):
        raise HTTPException(status_code=400, detail="invalid recipe name")
    removed = False
    for suffix in (".yaml", ".yml"):
        path = settings.recipes_dir / f"{name}{suffix}"
        if path.is_file():
            path.unlink()
            removed = True
    if not removed:
        path = _user_recipe_path(request, name)
        if path is not None and path.is_file():
            path.unlink()
            removed = True
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"no user recipe file '{name}' (builtin recipes cannot be deleted)",
        )
    request.app.state.recipes.get(force=True)
    return {"deleted": name}


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


@router.get("/models")
def list_models(request: Request) -> dict:
    manager = request.app.state.models
    pulling: set[str] = request.app.state.__dict__.setdefault("pulling_models", set())
    out = []
    for status in manager.list_status():
        spec = status.spec
        out.append(
            {
                "id": spec.id,
                "task": spec.task,
                "description": spec.description,
                "license": spec.license,
                "license_url": spec.license_url,
                "homepage": spec.homepage,
                "size_bytes": spec.size_bytes,
                "downloaded": status.downloaded,
                "downloading": spec.id in pulling,
                "pinned": status.pinned,
                "default": spec.default,
                "tags": list(spec.tags),
            }
        )
    return {"models": out}


class PullBody(BaseModel):
    allow_unpinned: bool = False


@router.post("/models/{model_id}/pull", status_code=202)
def pull_model(request: Request, model_id: str, body: PullBody | None = None) -> dict:
    manager = request.app.state.models
    events = request.app.state.events
    if model_id not in CATALOG:
        raise HTTPException(status_code=404, detail=f"unknown model '{model_id}'")
    if manager.is_available(model_id):
        return {"status": "already-downloaded", "model": model_id}
    pulling: set[str] = request.app.state.__dict__.setdefault("pulling_models", set())
    if model_id in pulling:
        return {"status": "already-downloading", "model": model_id}
    pulling.add(model_id)
    allow_unpinned = bool(body and body.allow_unpinned)

    def progress(done: int, total: int | None) -> None:
        events.publish_from_thread(
            "model.progress", {"model": model_id, "done": done, "total": total}
        )

    def work() -> None:
        try:
            manager.ensure(model_id, progress=progress, allow_unpinned=allow_unpinned)
            events.publish_from_thread("model.ready", {"model": model_id})
        except BatchroomError as exc:
            events.publish_from_thread(
                "model.error", {"model": model_id, "error": exc.message, "hint": exc.hint}
            )
        finally:
            pulling.discard(model_id)

    threading.Thread(target=work, name=f"pull-{model_id}", daemon=True).start()
    return {"status": "downloading", "model": model_id}


@router.get("/models/{model_id}/verify")
def verify_model(request: Request, model_id: str) -> dict:
    manager = request.app.state.models
    try:
        ok, detail = manager.verify(model_id)
    except ModelError as exc:
        raise _error(exc, 404) from exc
    return {"model": model_id, "ok": ok, "detail": detail}


@router.delete("/models/{model_id}", dependencies=[Depends(require_admin)])
def delete_model(request: Request, model_id: str) -> dict:
    """Admin-only in multi-user mode: the weights are shared by every user."""
    manager = request.app.state.models
    if model_id not in CATALOG:
        raise HTTPException(status_code=404, detail=f"unknown model '{model_id}'")
    removed = manager.delete(model_id)
    return {"model": model_id, "removed": removed}


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


class PathJobBody(BaseModel):
    recipe: str
    paths: list[str] = Field(min_length=1, max_length=10000)
    output_dir: str | None = None
    webhook_url: str | None = None
    priority: int = 0


def _resolve_recipe(request: Request, name: str):
    recipes = request.app.state.recipes.get()
    recipe = recipes.get(name)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"recipe '{name}' not found")
    return recipe


def _copy_upload(src, target: Path, limit_bytes: int, name: str) -> int:
    """Copy a parsed (spooled) upload to its job dir. Runs in a worker
    thread: hundreds of MB of disk writes must not stall the event loop."""
    size = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    src.seek(0)
    with open(target, "wb") as fh:
        while chunk := src.read(1 << 20):
            size += len(chunk)
            if size > limit_bytes:
                raise HTTPException(
                    status_code=413, detail=f"file '{name}' exceeds the upload limit"
                )
            fh.write(chunk)
    return size


@router.post("/jobs", status_code=202)
async def create_job(request: Request) -> dict:
    """Create a job. Two modes:

    * ``multipart/form-data``: fields ``recipe``, optional ``webhook_url``,
      files in ``files``.
    * ``application/json``: ``{recipe, paths[], output_dir?, webhook_url?}``
      for files already on the server (NAS mounts, CI). See SECURITY.md.
    """
    queue = _queue(request)
    settings = _settings(request)
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("application/json"):
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="request body is not valid JSON") from None
        try:
            body = PathJobBody.model_validate(payload)
        except ValidationError as exc:  # same 422 shape FastAPI gives typed bodies
            raise RequestValidationError(exc.errors(include_url=False)) from None
        recipe = _resolve_recipe(request, body.recipe)
        webhook_url = _webhook_or_400(body.webhook_url)
        paths = [Path(p).expanduser() for p in body.paths]
        missing = [str(p) for p in paths if not _path_exists(p)]
        if missing:
            raise HTTPException(
                status_code=400, detail=f"path(s) not found: {', '.join(missing[:5])}"
            )
        output_dir = Path(body.output_dir).expanduser() if body.output_dir else None
        # an output folder inside an input folder must not feed earlier results back in
        files, skipped = queue.collect_input_files(
            paths, recipe, exclude_dirs=(output_dir,) if output_dir else ()
        )
        try:
            job = queue.submit(
                recipe, files, source="api", output_dir=output_dir,
                webhook_url=webhook_url, skipped=skipped,
                user_id=actor_of(request).user_id,
                priority=_clamp_priority(request, body.priority),
            )
        except BatchroomError as exc:
            raise _error(exc) from exc
        return job

    form = await request.form(max_files=MAX_UPLOAD_FILES, max_fields=MAX_UPLOAD_FILES + 100)
    recipe_name = str(form.get("recipe") or "")
    if not recipe_name:
        raise HTTPException(status_code=400, detail="missing 'recipe' form field")
    recipe = _resolve_recipe(request, recipe_name)
    uploads = [v for v in form.getlist("files") if isinstance(v, UploadFile)]
    if not uploads:
        raise HTTPException(status_code=400, detail="no files uploaded (field name: 'files')")
    webhook_url = _webhook_or_400(str(form.get("webhook_url") or ""))

    job_id = new_job_id()
    job_dir = settings.jobs_dir / job_id
    limit = settings.max_upload_mb * 1024 * 1024
    saved: list[Path] = []
    try:
        for i, upload in enumerate(uploads):
            # Unicode names survive (商品.jpg, сумка.jpg): they become {original}
            clean = safe_upload_name(upload.filename or "", f"upload-{i}")
            # per-index subdir keeps duplicate upload names apart without renaming them
            target = job_dir / "in" / f"{i:04d}" / clean
            await asyncio.to_thread(_copy_upload, upload.file, target, limit, clean)
            saved.append(target)
        files, skipped = queue.collect_input_files(saved, recipe)
        job = queue.submit_with_id(
            job_id, recipe, files, source="api", webhook_url=webhook_url, skipped=skipped,
            user_id=actor_of(request).user_id,
            priority=_clamp_priority(request, form.get("priority")),
        )
    except BaseException as exc:
        # nothing was queued: don't leave the uploaded copies behind
        if not _job_exists(queue, job_id):
            shutil.rmtree(job_dir, ignore_errors=True)
        if isinstance(exc, BatchroomError):
            raise _error(exc) from exc
        raise
    return job


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:  # permission denied on a parent, name too long...
        return False


def _job_exists(queue: JobQueue, job_id: str) -> bool:
    try:
        queue.get_job(job_id)
    except Exception:  # not found, or the database is unreachable right now
        return False
    return True


@router.get("/jobs")
def list_jobs(request: Request, limit: int = 50, offset: int = 0) -> dict:
    limit = max(1, min(limit, 200))
    actor = actor_of(request)
    user_filter = None if actor.is_admin else (actor.user_id or -1)
    return {
        "jobs": _queue(request).list_jobs(
            limit=limit, offset=max(0, offset), user_id=user_filter
        )
    }


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> dict:
    queue = _queue(request)
    job = _job_or_404(request, job_id)
    try:
        items = queue.get_items(job_id)
    except JobError as exc:
        raise _error(exc, 404) from exc
    return {**job, "items": items}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: str) -> dict:
    _job_or_404(request, job_id)
    try:
        return _queue(request).cancel(job_id)
    except JobError as exc:
        raise _error(exc, 404) from exc


@router.post("/jobs/{job_id}/retry")
def retry_job(request: Request, job_id: str) -> dict:
    """Re-queue a finished job's failed/cancelled items (e.g. after fixing
    the recipe or the network); done items are kept as they are."""
    _job_or_404(request, job_id)
    try:
        return _queue(request).retry(job_id)
    except JobError as exc:
        raise _error(exc, 404 if "not found" in exc.message else 409) from exc


@router.get("/jobs/{job_id}/manifest")
def job_manifest(request: Request, job_id: str) -> dict:
    """This job's manifest, rebuilt from the queue database (also while it
    runs, and when several jobs share one output folder)."""
    _job_or_404(request, job_id)
    try:
        return _queue(request).manifest_for(job_id)
    except JobError as exc:
        raise _error(exc, 404) from exc


@router.delete("/jobs/{job_id}")
def delete_job(request: Request, job_id: str) -> dict:
    _job_or_404(request, job_id)
    try:
        _queue(request).delete(job_id)
    except JobError as exc:
        status = 404 if "not found" in exc.message else 409
        raise _error(exc, status) from exc
    return {"deleted": job_id}


# ---------------------------------------------------------------------------
# job files: thumbnails, outputs, archive
# ---------------------------------------------------------------------------


def _job_or_404(request: Request, job_id: str) -> dict:
    """Fetch a job the current actor may access. Members only see their own
    jobs; a 404 (not 403) avoids leaking other users' job ids."""
    try:
        job = _queue(request).get_job(job_id)
    except JobError as exc:
        raise _error(exc, 404) from exc
    actor = actor_of(request)
    if not actor.is_admin and job.get("user_id") != actor.user_id:
        raise HTTPException(status_code=404, detail=f"job '{job_id}' not found")
    return job


@router.get("/jobs/{job_id}/thumb/{idx}/{kind}")
def job_thumb(request: Request, job_id: str, idx: int, kind: str) -> FileResponse:
    if kind not in ("in", "out") or idx < 0 or idx > 99999:
        raise HTTPException(status_code=400, detail="bad thumbnail reference")
    _job_or_404(request, job_id)
    path = _settings(request).jobs_dir / job_id / "thumbs" / f"{idx:05d}-{kind}.webp"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="thumbnail not available")
    return FileResponse(path, media_type="image/webp")


@router.get("/jobs/{job_id}/outputs/{relpath:path}")
def job_output(request: Request, job_id: str, relpath: str) -> FileResponse:
    job = _job_or_404(request, job_id)
    out_dir = Path(job["output_dir"]).resolve()
    candidate = (out_dir / relpath).resolve()
    if not candidate.is_relative_to(out_dir):
        raise HTTPException(status_code=400, detail="path escapes the job output directory")
    # only this job's recorded outputs: a shared output folder (or one pointed
    # at an arbitrary directory) must not serve everything else in it
    recorded = _job_outputs(_queue(request), job_id, out_dir).values()
    if candidate not in recorded or not candidate.is_file():
        raise HTTPException(status_code=404, detail="output not found")
    return FileResponse(candidate, filename=candidate.name)


def _job_outputs(queue: JobQueue, job_id: str, out_dir: Path) -> dict[str, Path]:
    """``relpath -> resolved path`` of the outputs recorded for this job that
    stay inside its output directory."""
    outputs: dict[str, Path] = {}
    for item in queue.get_items(job_id):
        for output in (item.get("result") or {}).get("outputs", []):
            relpath = str(output.get("path", ""))
            candidate = (out_dir / relpath).resolve()
            if relpath and candidate.is_relative_to(out_dir):
                outputs[relpath] = candidate
    return outputs


@router.get("/jobs/{job_id}/archive")
def job_archive(request: Request, job_id: str) -> StreamingResponse:
    """ZIP of this job's own outputs (+ its manifest), streamed as it's built.

    Only files recorded for this job are included: a path/watch job writing
    into a shared folder must not hand out everything else in that folder."""
    job = _job_or_404(request, job_id)
    if job["status"] not in ("done", "cancelled"):
        raise HTTPException(status_code=409, detail="job is still running")
    queue = _queue(request)
    out_dir = Path(job["output_dir"]).resolve()
    entries: dict[str, Path | bytes] = {
        relpath: candidate
        for relpath, candidate in _job_outputs(queue, job_id, out_dir).items()
        if candidate.is_file()
    }
    if queue.wants_manifest_file(job["recipe"]) and MANIFEST_NAME not in entries:
        entries[MANIFEST_NAME] = _manifest_bytes(queue, job_id, out_dir / MANIFEST_NAME)
    filename = f"batchroom-{job_id}.zip"
    return StreamingResponse(
        stream_zip(sorted(entries.items())),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _manifest_bytes(queue: JobQueue, job_id: str, on_disk: Path) -> bytes:
    """The manifest file as integrations saw it when it belongs to this job;
    rebuilt from the database when another job's run overwrote it."""
    try:
        raw = on_disk.read_bytes()
        if json.loads(raw).get("job_id") == job_id:
            return raw
    except (OSError, ValueError, AttributeError):
        pass
    return json.dumps(queue.manifest_for(job_id), indent=2, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# events (SSE)
# ---------------------------------------------------------------------------


@router.get("/events")
async def events_stream(request: Request) -> StreamingResponse:
    bus = request.app.state.events
    actor = actor_of(request)
    jobs = _queue(request)
    owned: dict[str, bool] = {}  # job id -> may this member see its events

    async def visible(event: dict) -> bool:
        """Members only get events of their own jobs (job ids must not leak)."""
        if actor.is_admin:
            return True
        job = event.get("job")
        job_id = job.get("id") if isinstance(job, dict) else event.get("job_id")
        if job_id is None:  # model.* and other non-job events
            return True
        if isinstance(job, dict):
            owned[job_id] = job.get("user_id") == actor.user_id
        elif job_id not in owned:
            try:
                found = await asyncio.to_thread(jobs.get_job, job_id)
            except JobError:
                return False  # e.g. job.deleted of a job never seen here
            owned[job_id] = found.get("user_id") == actor.user_id
        return owned[job_id]

    async def generator():
        queue = bus.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    if await visible(event):
                        yield sse_format(event)
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


@router.get("/watch")
def watch_status(request: Request) -> dict:
    return request.app.state.watcher.status()


@router.post("/watch/reload")
def watch_reload(request: Request) -> dict:
    watcher = request.app.state.watcher
    watcher.reload_config()
    return watcher.status()


def error_payload(detail: str, hint: str | None = None) -> JSONResponse:
    payload = {"detail": detail}
    if hint:
        payload["hint"] = hint
    return JSONResponse(status_code=400, content=payload)
