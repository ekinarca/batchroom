"""Model download, verification and ONNX session cache.

Weights live under ``<data_dir>/models/<id>.onnx``. Downloads stream to a
``.part`` file while hashing, verify against the pinned SHA-256, then rename
atomically — a crashed download never leaves a corrupt weight in place, and
a tampered mirror never gets loaded.

Unpinned catalog entries (sha256 = None) refuse to download unless the
caller passes ``allow_unpinned=True``; the computed hash is then recorded to
``models.lock.json`` and enforced from that point on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..core.errors import ModelError
from .registry import CATALOG, ModelSpec, get_spec

log = logging.getLogger("batchroom.models")

ProgressCb = Callable[[int, int | None], None]  # (bytes_done, bytes_total)


@dataclass
class ModelStatus:
    spec: ModelSpec
    downloaded: bool
    path: Path | None
    size_bytes: int | None
    pinned: bool


class ModelManager:
    def __init__(self, models_dir: Path, *, onnx_threads: int = 0):
        self.models_dir = Path(models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.onnx_threads = onnx_threads
        self._sessions: dict[str, object] = {}
        self._lock = threading.Lock()
        self._download_locks: dict[str, threading.Lock] = {}

    # ---- paths & lockfile ----------------------------------------------
    def path_for(self, model_id: str) -> Path:
        return self.models_dir / f"{model_id}.onnx"

    @property
    def _lockfile(self) -> Path:
        return self.models_dir / "models.lock.json"

    def _read_lock(self) -> dict[str, str]:
        try:
            return json.loads(self._lockfile.read_text())
        except (OSError, ValueError):
            return {}

    def _write_lock(self, data: dict[str, str]) -> None:
        self._lockfile.write_text(json.dumps(data, indent=2, sort_keys=True))

    def expected_sha(self, spec: ModelSpec) -> str | None:
        return spec.sha256 or self._read_lock().get(spec.id)

    # ---- status ---------------------------------------------------------
    def status(self, model_id: str) -> ModelStatus:
        spec = get_spec(model_id)
        if spec is None:
            raise ModelError(
                f"unknown model '{model_id}'", hint=f"known: {', '.join(sorted(CATALOG))}"
            )
        path = self.path_for(model_id)
        exists = path.is_file()
        return ModelStatus(
            spec=spec,
            downloaded=exists,
            path=path if exists else None,
            size_bytes=path.stat().st_size if exists else None,
            pinned=self.expected_sha(spec) is not None,
        )

    def list_status(self) -> list[ModelStatus]:
        return [self.status(mid) for mid in CATALOG]

    def is_available(self, model_id: str) -> bool:
        return self.path_for(model_id).is_file()

    # ---- download & verify ----------------------------------------------
    def ensure(
        self,
        model_id: str,
        *,
        progress: ProgressCb | None = None,
        allow_unpinned: bool = False,
    ) -> Path:
        """Return a verified local path, downloading if necessary."""
        spec = get_spec(model_id)
        if spec is None:
            raise ModelError(
                f"unknown model '{model_id}'", hint=f"known: {', '.join(sorted(CATALOG))}"
            )
        path = self.path_for(model_id)
        if path.is_file():
            return path
        with self._lock:
            dl_lock = self._download_locks.setdefault(model_id, threading.Lock())
        with dl_lock:
            if path.is_file():  # someone else finished while we waited
                return path
            return self._download(spec, path, progress, allow_unpinned)

    def _download(
        self,
        spec: ModelSpec,
        path: Path,
        progress: ProgressCb | None,
        allow_unpinned: bool,
    ) -> Path:
        expected = self.expected_sha(spec)
        if expected is None and not allow_unpinned:
            raise ModelError(
                f"model '{spec.id}' has no pinned checksum in this build",
                hint="pass allow_unpinned (CLI: --allow-unpinned) to download and pin it locally",
            )
        log.info("downloading %s from %s", spec.id, spec.url)
        tmp = path.with_suffix(".onnx.part")
        hasher = hashlib.sha256()
        done = 0
        try:
            with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)) as client:
                with client.stream("GET", spec.url) as response:
                    if response.status_code != 200:
                        raise ModelError(
                            f"download failed for '{spec.id}': HTTP {response.status_code}",
                            hint=f"url: {spec.url}",
                        )
                    total = spec.size_bytes
                    header_len = response.headers.get("content-length")
                    if header_len and header_len.isdigit():
                        total = int(header_len)
                    with open(tmp, "wb") as fh:
                        for chunk in response.iter_bytes(chunk_size=1 << 20):
                            fh.write(chunk)
                            hasher.update(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done, total)
        except httpx.HTTPError as exc:
            tmp.unlink(missing_ok=True)
            raise ModelError(
                f"download failed for '{spec.id}': {exc}",
                hint="check network access; models download once and are cached",
            ) from exc
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        digest = hasher.hexdigest()
        if expected is not None and digest != expected:
            tmp.unlink(missing_ok=True)
            raise ModelError(
                f"checksum mismatch for '{spec.id}': expected {expected[:16]}…, got {digest[:16]}…",
                hint="the remote file changed or was tampered with; refusing to use it",
            )
        if expected is None:
            lock = self._read_lock()
            lock[spec.id] = digest
            self._write_lock(lock)
            log.warning("model %s pinned locally to sha256:%s", spec.id, digest)
        tmp.replace(path)
        log.info("model %s ready (%d bytes, sha256:%s…)", spec.id, done, digest[:16])
        return path

    def verify(self, model_id: str) -> tuple[bool, str]:
        """Re-hash a downloaded model against its pin. Returns (ok, detail)."""
        status = self.status(model_id)
        if not status.downloaded or status.path is None:
            return False, "not downloaded"
        expected = self.expected_sha(status.spec)
        hasher = hashlib.sha256()
        with open(status.path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if expected is None:
            return True, f"unpinned; sha256:{digest}"
        if digest == expected:
            return True, f"sha256 verified ({digest[:16]}…)"
        return False, f"MISMATCH: expected {expected[:16]}…, got {digest[:16]}…"

    def delete(self, model_id: str) -> bool:
        """Remove a downloaded weight (the catalog entry stays)."""
        path = self.path_for(model_id)
        with self._lock:
            self._sessions.pop(model_id, None)
        if path.is_file():
            path.unlink()
            return True
        return False

    # ---- ONNX sessions ----------------------------------------------------
    def session(self, model_id: str):
        """Shared onnxruntime session (InferenceSession.run is thread-safe)."""
        with self._lock:
            cached = self._sessions.get(model_id)
        if cached is not None:
            return cached
        path = self.path_for(model_id)
        if not path.is_file():
            raise ModelError(
                f"model '{model_id}' is not downloaded",
                hint=f"run: batchroom models pull {model_id}",
            )
        try:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            if self.onnx_threads > 0:
                opts.intra_op_num_threads = self.onnx_threads
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            session = ort.InferenceSession(
                str(path), sess_options=opts, providers=["CPUExecutionProvider"]
            )
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError(
                f"cannot load model '{model_id}': {exc}",
                hint="the file may be corrupt; delete it and pull again",
            ) from exc
        with self._lock:
            self._sessions[model_id] = session
        return session
