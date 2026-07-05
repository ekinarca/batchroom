"""REST API routes.

Conventions: JSON in/out, errors as ``{"detail": ..., "hint": ...}``,
async job submission returning 202-style job objects immediately.
"""

from __future__ import annotations

import asyncio
import re
import threading
import zipfile
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from .. import __version__
from ..config import Settings
from ..core.errors import BatchroomError, JobError, ModelError, RecipeError
from ..core.image_io import FORMATS, format_available
from ..core.recipe import load_recipe_text
from ..i18n import available_languages, load_strings
from ..models.registry import CATALOG
from ..ops.base import all_ops
from ..queue.events import sse_format
from ..queue.jobs import JobQueue, new_job_id
from .auth import actor_of, features

router = APIRouter()
open_router = APIRouter()  # unauthenticated: i18n only (login screen needs strings)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ ()-]+")


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


@router.post("/recipes", status_code=201)
def save_recipe(request: Request, body: RecipeBody) -> dict:
    settings = _settings(request)
    try:
        recipe = load_recipe_text(body.text)
    except RecipeError as exc:
        raise _error(exc) from exc
    path = settings.recipes_dir / f"{recipe.name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.text, encoding="utf-8")
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


@router.delete("/models/{model_id}")
def delete_model(request: Request, model_id: str) -> dict:
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


async def _save_upload(upload: UploadFile, target: Path, limit_bytes: int) -> int:
    size = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "wb") as fh:
            while chunk := await upload.read(1 << 20):
                size += len(chunk)
                if size > limit_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"file '{upload.filename}' exceeds the upload limit",
                    )
                fh.write(chunk)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise
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
        body = PathJobBody.model_validate(await request.json())
        recipe = _resolve_recipe(request, body.recipe)
        paths = [Path(p).expanduser() for p in body.paths]
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            raise HTTPException(
                status_code=400, detail=f"path(s) not found: {', '.join(missing[:5])}"
            )
        files, skipped = queue.collect_input_files(paths, recipe)
        output_dir = Path(body.output_dir).expanduser() if body.output_dir else None
        try:
            job = queue.submit(
                recipe, files, source="api", output_dir=output_dir,
                webhook_url=body.webhook_url, skipped=skipped,
                user_id=actor_of(request).user_id,
                priority=_clamp_priority(request, body.priority),
            )
        except BatchroomError as exc:
            raise _error(exc) from exc
        return job

    form = await request.form()
    recipe_name = str(form.get("recipe") or "")
    if not recipe_name:
        raise HTTPException(status_code=400, detail="missing 'recipe' form field")
    recipe = _resolve_recipe(request, recipe_name)
    uploads = [v for v in form.getlist("files") if isinstance(v, UploadFile)]
    if not uploads:
        raise HTTPException(status_code=400, detail="no files uploaded (field name: 'files')")
    webhook_url = str(form.get("webhook_url") or "") or None

    job_id = new_job_id()
    in_dir = settings.jobs_dir / job_id / "in"
    limit = settings.max_upload_mb * 1024 * 1024
    saved: list[Path] = []
    for i, upload in enumerate(uploads):
        raw_name = Path(upload.filename or f"upload-{i}").name
        clean = _SAFE_NAME_RE.sub("-", raw_name).strip(" .-") or f"upload-{i}"
        # per-index subdir keeps duplicate upload names apart without renaming them
        target = in_dir / f"{i:04d}" / clean
        await _save_upload(upload, target, limit)
        saved.append(target)

    files, skipped = queue.collect_input_files(saved, recipe)
    try:
        job = queue.submit_with_id(
            job_id, recipe, files, source="api", webhook_url=webhook_url, skipped=skipped,
            user_id=actor_of(request).user_id,
            priority=_clamp_priority(request, form.get("priority")),
        )
    except BatchroomError as exc:
        raise _error(exc) from exc
    return job


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
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="output not found")
    return FileResponse(candidate, filename=candidate.name)


@router.get("/jobs/{job_id}/archive")
def job_archive(request: Request, job_id: str) -> FileResponse:
    job = _job_or_404(request, job_id)
    if job["status"] not in ("done", "cancelled"):
        raise HTTPException(status_code=409, detail="job is still running")
    out_dir = Path(job["output_dir"]).resolve()
    if not out_dir.is_dir():
        raise HTTPException(status_code=404, detail="output directory missing")
    settings = _settings(request)
    archive_dir = settings.tmp_dir / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / f"{job_id}.zip"
    manifest = out_dir / "manifest.json"
    stale = not archive.is_file() or (
        manifest.is_file() and manifest.stat().st_mtime > archive.stat().st_mtime
    )
    if stale:
        tmp = archive.with_suffix(".zip.part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in sorted(out_dir.rglob("*")):
                if path.is_file() and not path.name.endswith(".part"):
                    zf.write(path, path.relative_to(out_dir).as_posix())
        tmp.replace(archive)
    return FileResponse(archive, filename=f"batchroom-{job_id}.zip")


# ---------------------------------------------------------------------------
# events (SSE)
# ---------------------------------------------------------------------------


@router.get("/events")
async def events_stream(request: Request) -> StreamingResponse:
    bus = request.app.state.events

    async def generator():
        queue = bus.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
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
