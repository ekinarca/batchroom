# Changelog

All notable changes to Batchroom. Format: [Keep a Changelog](https://keepachangelog.com), versioning: [SemVer](https://semver.org).

## [Unreleased]

### Added

- First-run onboarding tour: a three-step "how it works" dialog on first
  visit, reopenable anytime from the sidebar — localized in all 10
  languages.
- **Retry**: a finished job's failed and cancelled items can be run again
  (job drawer button or `POST /api/jobs/{id}/retry`) — finished outputs
  are kept. Handy after fixing a recipe or a network hiccup during the
  first model download.
- **Folders in the web UI**: drag a whole folder onto the drop zone or use
  "Choose a folder"; hidden and NAS system folders are skipped, files
  added twice are de-duplicated, and non-image files are reported
  instead of silently dropped.
- Live step text under each running item in the job drawer (current op,
  first-use model download percentage).
- `GET /api/jobs/{id}/manifest`: the job's own manifest, also while it
  runs and when several jobs share one output folder.

### Changed

- ZIP downloads are streamed while they are built: the first byte goes
  out at once (large jobs no longer hit reverse-proxy timeouts), no
  second copy of the outputs piles up under `data/tmp`, and the archive
  holds only this job's outputs + manifest — not everything else in a
  shared output folder.
- Recipe geometry is bounded at validation time: sizes 1–20000 px per
  side, percentage padding under 50%, non-negative pixel padding; any
  single result above ~240 MP fails that item cleanly instead of
  exhausting memory.
- Multi-user mode: deleting a model (shared by everyone) needs the admin
  role.
- Login rate limiting counts failed attempts per account+source and per
  source, and can no longer be reset by spraying throwaway emails.
- Watch folders, server-path jobs and `batchroom run` share one input
  rule set: hidden folders, NAS system folders (`@eaDir`, `#recycle`,
  `$RECYCLE.BIN`…) and the job's own output folder are never walked.
  Watch-folder scans run off the event loop, so a slow SMB/NFS share no
  longer stalls the UI and API.
- Python 3.14 supported and CI-tested (3.11–3.14). CI uses the Node 24
  majors of `actions/checkout` / `actions/setup-python` (GitHub removed
  Node 20 from its runners).
- Dev extra: `httpx2`, which Starlette ≥ 1.0's TestClient runs on.

### Fixed

- Memory: background-removal sessions kept their peak working memory
  for good (ONNX Runtime's CPU arena). With 4 workers the default model
  alone held ~3.6 GB; running each background model once OOM-killed a
  16 GB machine — not what a 4–8 GB NAS can afford. They now run without
  the arena (~0.5 GB, equal or better throughput); tiled upscaling keeps
  it, where it is ~30% faster for a bounded ~1 GB.
- Before/after compare no longer misaligns when a recipe changes the
  aspect ratio (1:1 packshots, capsule crops): both sides now letterbox
  into one frame, and the revealed side always reads as pure "after".
- Uploads named in non-Latin scripts (`商品.jpg`, `сумка.jpg`, `منتج.jpg`)
  were rejected as "no processable input files", and Turkish names were
  mangled (`çanta` → `anta`); Unicode filenames now survive into
  `{original}`.
- Cancelling a job didn't count the items it cancelled while still
  pending, so cancelled jobs showed e.g. "2/30" forever. Counters are
  now exact, and existing databases are repaired on upgrade (schema v3).
- Crash recovery: a job whose cancel was requested before a crash, or
  that crashed between its last item and finalizing, no longer stays
  "running" forever after a restart.
- One transient database error (e.g. "database is locked") stopped the
  job dispatcher until the next restart; it now logs and carries on.
- `POST /api/jobs` with a malformed or incomplete JSON body answered 500;
  it now returns 400/422. Invalid `webhook_url`s are rejected up front.
- A rejected or oversized upload left its files behind under
  `data/jobs/`.
- A recipe size like `[null, 3]` crashed validation with a 500.
- `output.manifest: false` was ignored by the server (only the CLI
  honored it).
- `{date}` in rename patterns now uses the job's start date for every
  item, even when a batch runs past midnight.
- Saving a recipe from the editor whose file had a different name than
  the recipe wrote a second file that the original kept shadowing; the
  original file is updated now (and can be deleted from the UI).
- Concurrent first use of a model loaded it once per worker (4× the
  memory for a moment); it is loaded once and shared.
- Watch folders: files deleted before they became stable are forgotten
  instead of tracked forever; an output folder that contains the watched
  folder is reported as a configuration problem.
- Webhook deliveries in flight are kept referenced (no silent garbage
  collection) and get a moment to finish on shutdown.
- API key comparison is constant-time.
- CLI: a malformed `BATCHROOM_*` variable prints a clean error instead
  of a traceback; `batchroom run` with `-o` inside an input folder no
  longer re-processes its previous results.
- Web UI: a remembered recipe that was deleted or renamed made uploads
  fail with "recipe not found" although the picker showed another one;
  the job drawer no longer jumps to the top, replays its slide-in or
  stacks duplicates on every live update; output links with `#` or `%`
  in the name work; modals close on backdrop click and Escape (editors
  and the shown-once API key excepted); the RTL compare slider's reveal
  now follows the divider; the RTL drawer slides in from the correct
  side; pending items no longer fire thumbnail 404s on every refresh;
  the running-jobs badge stays current on every page.

## [0.1.0] — 2026-07-05

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
