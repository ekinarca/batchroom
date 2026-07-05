"""Server configuration.

Everything is driven by environment variables prefixed ``BATCHROOM_`` so the
same code works bare-metal, in Docker and in tests. There is deliberately no
external service dependency: state lives in SQLite under ``data_dir``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .core.errors import ConfigError

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def default_worker_count() -> int:
    cpus = os.cpu_count() or 2
    return max(1, min(4, cpus // 2))


@dataclass
class Settings:
    """Resolved runtime settings. Create via :meth:`from_env` or directly in tests."""

    data_dir: Path = field(default_factory=lambda: Path("data"))
    host: str = "127.0.0.1"
    port: int = 5151
    workers: int = field(default_factory=default_worker_count)
    api_key: str | None = None
    max_upload_mb: int = 512
    onnx_threads: int = 0  # 0 = let onnxruntime decide
    watch_enabled: bool = True
    watch_poll_seconds: float = 2.0
    watch_stable_seconds: float = 3.0
    log_level: str = "info"
    # Extra recipe directories besides <data_dir>/recipes and the bundled set.
    extra_recipe_dirs: list[Path] = field(default_factory=list)
    # Pro: email notifications
    smtp_url: str | None = None
    notify_email: str | None = None

    # ---- derived paths -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "batchroom.db"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def recipes_dir(self) -> Path:
        return self.data_dir / "recipes"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def watch_config_path(self) -> Path:
        return self.data_dir / "watch.yaml"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.models_dir,
            self.jobs_dir,
            self.uploads_dir,
            self.recipes_dir,
            self.tmp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # ---- construction --------------------------------------------------
    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.environ.get("BATCHROOM_DATA_DIR", "data")).expanduser()
        extra = [
            Path(p).expanduser()
            for p in os.environ.get("BATCHROOM_RECIPE_DIRS", "").split(os.pathsep)
            if p.strip()
        ]
        settings = cls(
            data_dir=data_dir,
            host=os.environ.get("BATCHROOM_HOST", "127.0.0.1"),
            port=_env_int("BATCHROOM_PORT", 5151, minimum=1),
            workers=_env_int("BATCHROOM_WORKERS", default_worker_count(), minimum=1),
            api_key=os.environ.get("BATCHROOM_API_KEY") or None,
            max_upload_mb=_env_int("BATCHROOM_MAX_UPLOAD_MB", 512, minimum=1),
            onnx_threads=_env_int("BATCHROOM_ONNX_THREADS", 0, minimum=0),
            watch_enabled=_env_bool("BATCHROOM_WATCH", True),
            log_level=os.environ.get("BATCHROOM_LOG_LEVEL", "info"),
            extra_recipe_dirs=extra,
            smtp_url=os.environ.get("BATCHROOM_SMTP_URL") or None,
            notify_email=os.environ.get("BATCHROOM_NOTIFY_EMAIL") or None,
        )
        return settings
