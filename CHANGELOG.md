# Changelog

All notable changes to Batchroom. Format: [Keep a Changelog](https://keepachangelog.com), versioning: [SemVer](https://semver.org).

## [0.1.0] — unreleased (launch candidate)

First public version. Everything below is new.

### Pro (paid layer, source-available under LICENSE-PRO)

- Offline license keys: Ed25519-signed, verified locally — no
  phone-home, air-gap friendly; 3-day renewal grace; expired licenses
  never lock accounts or data, only management features pause.
- Multi-user accounts with admin/member roles: scrypt passwords,
  hashed server-side sessions, per-user API keys, seat limits from the
  license, job-level ownership (members see only their own jobs).
- Priority queue: high-priority jobs claim workers first.
- Email notifications on job completion (SMTP via one env var; mails the
  submitting user or a configured fallback).
- License panel, login screen and user administration in the web UI
  (all 10 languages); `batchroom license` and `batchroom users` CLI;
  operator tooling (`scripts/license_admin.py`) for issuing keys.

### Core

- Recipe engine: YAML pipelines with strict validation (unknown ops/params
  fail at load with file+step context, before any image is touched).
- Ops: `remove-background` (ONNX, guided-filter `alpha_matting` edge
  refinement), `upscale` (Real-ESRGAN ONNX tiled, or Lanczos), `resize`
  (contain/pad/cover/stretch), `trim`, `composite` (aspect + padding),
  `convert` (JPG/PNG/WebP/AVIF/HEIC/TIFF, quality/lossless), `preset-pack`
  (multi-output fan-out), `rename` (pattern vars), `strip-metadata`
  (EXIF privacy default).
- Single-encode pipeline: intermediate steps stay in memory as RGBA;
  files are encoded exactly once at materialize time.
- Batch manifest (`manifest.json`): outputs with dimensions, byte sizes
  and SHA-256 per file; machine-readable job record.
- Immutable inputs, atomic output writes (`.part` + rename).

### Server

- FastAPI server + job queue on SQLite (WAL) — zero external services.
- Parallel workers (thread pool; ONNX/Pillow release the GIL), per-item
  isolation: one corrupt file can't fail a batch.
- Crash resume: in-flight items re-queue on boot; finished work is never
  redone.
- REST API: async jobs (multipart upload or server-side paths), cancel,
  delete, ZIP archive, thumbnails, SSE progress events, recipe CRUD +
  validation, model management, watch status. OpenAPI at `/api/docs`.
- Webhooks: manifest POSTed on completion, 3 attempts with backoff.
- Watch folders: polling + size-stability pickup (SMB/NFS-safe),
  `done/`/`failed/` disposition, crash-orphan recovery.
- Optional API-key auth; path-traversal-safe file serving; upload caps.

### Models

- Catalog (permissive licenses only, SHA-256 pinned): `isnet-general`
  (default), `u2net`, `u2netp`, `silueta`, `birefnet-general-lite`,
  `realesrgan-x4`.
- On-demand checksum-verified downloads; license shown before download;
  `batchroom models pull/verify/rm`.
- Reproducible Real-ESRGAN ONNX conversion from official BSD-3 weights
  (`scripts/convert_realesrgan_onnx.py`, torch cross-checked to 2e-6).

### UI

- Single-page vanilla-JS web UI, dark monochrome design, no build chain.
- Drag & drop batches, live queue (SSE), before/after compare slider,
  recipe editor with validation, model manager with licenses + download
  progress, watch-folder status, system panel.
- 10 complete languages: en, tr, fr, it, ja, zh, es, ar (RTL), pl, ru —
  parity enforced by CI.

### Distribution

- Dockerfile (non-root, healthcheck, amd64+arm64-ready), docker-compose,
  Unraid template, CasaOS template.
- CLI: `serve`, `run` (headless batch, CI-friendly), `recipes
  list/validate`, `models list/pull/verify/rm`.
- CI: ruff + i18n verification + 121 tests (unit, integration, golden-image
  regression corpus) on Python 3.11–3.13 + Docker build & smoke test.
