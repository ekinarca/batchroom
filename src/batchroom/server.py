"""FastAPI application factory and process lifecycle."""

from __future__ import annotations

import logging
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import APP_NAME, __version__
from .config import Settings
from .core.recipe import Recipe, discover_recipes, recipe_dirs
from .models.manager import ModelManager
from .pro import load_license
from .pro.emailer import EmailNotifier, SmtpConfig
from .pro.users import get_user
from .queue.db import Database
from .queue.events import EventBus
from .queue.jobs import JobQueue
from .watch.watcher import Watcher

log = logging.getLogger("batchroom")

UI_DIR = Path(__file__).resolve().parent / "ui"


class RecipeStore:
    """Recipe discovery with a small TTL cache (recipes are files on disk)."""

    def __init__(self, settings: Settings, ttl: float = 2.0):
        self.settings = settings
        self.ttl = ttl
        self._cache: dict[str, Recipe] = {}
        self.problems: list[str] = []
        self._loaded_at = 0.0

    def get(self, *, force: bool = False) -> dict[str, Recipe]:
        now = time.monotonic()
        if force or now - self._loaded_at > self.ttl:
            self._cache, self.problems = discover_recipes(recipe_dirs(self.settings))
            self._loaded_at = now
        return self._cache

    def __call__(self) -> dict[str, Recipe]:
        return self.get()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    # ZIP downloads stream now; drop the on-disk cache older versions kept
    shutil.rmtree(settings.tmp_dir / "archives", ignore_errors=True)

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    database = Database(settings.db_path)
    events = EventBus()
    models = ModelManager(settings.models_dir, onnx_threads=settings.onnx_threads)
    recipes = RecipeStore(settings)

    # Pro: email notifications on job completion (no-op without SMTP config)
    notifier: EmailNotifier | None = None
    if settings.smtp_url:
        smtp = SmtpConfig.from_url(settings.smtp_url)
        if smtp:
            notifier = EmailNotifier(smtp)

    app_holder: dict = {}

    def notify_job_finished(job: dict, manifest: dict) -> None:
        app_ref = app_holder.get("app")
        if app_ref is None or notifier is None:
            return
        from .api.auth import features  # late import avoids a cycle

        if "email_notifications" not in features(app_ref):
            return
        to = None
        if job.get("user_id"):
            user = get_user(database, job["user_id"])
            if user:
                to = user["email"]
        to = to or settings.notify_email
        if to:
            notifier.notify_job_finished(to, job, manifest)

    queue = JobQueue(settings, database, models, events, recipes, notify=notify_job_finished)
    watcher = Watcher(settings, queue)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await queue.start()
        if settings.watch_enabled:
            await watcher.start()
        log.info(
            "%s %s ready — data=%s workers=%d ui=http://%s:%d",
            APP_NAME, __version__, settings.data_dir, settings.workers,
            settings.host, settings.port,
        )
        yield
        if settings.watch_enabled:
            await watcher.stop()
        await queue.stop()

    app = FastAPI(
        title=APP_NAME,
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app_holder["app"] = app
    app.state.settings = settings
    app.state.db = database
    app.state.events = events
    app.state.models = models
    app.state.recipes = recipes
    app.state.queue = queue
    app.state.watcher = watcher
    app.state.started_at = time.time()
    app.state.license_info = load_license(settings)

    from .api.auth import get_actor
    from .api.auth import router as auth_router
    from .api.routes import open_router
    from .api.routes import router as api_router
    from .api.routes_pro import router as pro_router

    # Open endpoints: health, i18n (login screen needs strings), auth probe/login.
    app.include_router(open_router, prefix="/api")
    app.include_router(auth_router, prefix="/api")
    # Everything else requires an actor (single-user or multi-user rules).
    app.include_router(api_router, prefix="/api", dependencies=[Depends(get_actor)])
    app.include_router(pro_router, prefix="/api", dependencies=[Depends(get_actor)])

    @app.get("/api/health", include_in_schema=False)
    def health() -> JSONResponse:  # unauthenticated by design (container healthcheck)
        return JSONResponse({"status": "ok", "app": APP_NAME, "version": __version__})

    if UI_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=UI_DIR), name="assets")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(UI_DIR / "index.html")

    return app
