# Architecture

One process, no external services. State is SQLite; models are files;
the web UI is static files served by the same server.

```
┌────────────────────────────────────────────────────────────────┐
│ batchroom serve (FastAPI + uvicorn, one process)               │
│                                                                │
│  web UI (vanilla JS) ──► REST API ──► JobQueue ──► SQLite(WAL) │
│                              │            │                    │
│  SSE /api/events ◄── EventBus│      dispatcher (asyncio)       │
│                              │            │                    │
│  Watcher (polling) ──────────┘      ThreadPool workers (N)     │
│    watch.yaml folders                     │                    │
│                                     process_item()             │
│                                     ┌─ recipe steps ─┐         │
│                                     │ ops on RGBA np │         │
│                                     │ ONNX sessions  │◄─ ModelManager
│                                     └─ materialize ──┘   (sha-pinned
│                                        outputs+manifest    downloads)
└────────────────────────────────────────────────────────────────┘
```

## Key decisions & why

**Threads, not processes, for workers.** onnxruntime and Pillow release
the GIL during the heavy work, so a thread pool gives real parallelism
while sharing one ONNX session per model (`InferenceSession.run` is
thread-safe). Process pools would multiply model memory by N workers.

**Item-granular work units.** A job is N items; workers claim one item
at a time. Consequences: parallelism inside a single job, a corrupt file
fails alone, and resume-after-crash is trivial (re-queue items stuck in
`running` at boot — done in `JobQueue._recover`).

**SQLite in WAL mode, short-lived connections.** Every state transition
commits; any thread can write without a shared-connection lock. Nothing
else to install or operate.

**Single-encode pipeline.** Steps pass `(H, W, 4) uint8 RGBA` numpy
arrays; planning ops (`convert`, `rename`, `preset-pack`) record intent
only. Pixels hit a codec exactly once, at materialize time — no
generational JPEG loss between steps.

**Polling watch folders.** inotify/FSEvents are unreliable on SMB/NFS —
precisely the NAS mounts this feature exists for. The watcher polls,
requires size+mtime stability across scans plus a settle window, moves
ready files into a job-named intake dir (atomic rename), and never
ingests its own outputs.

**Models are downloads, not image layers.** Keeps the Docker image slim,
lets users pick quality/speed models per hardware, and each download is
verified against a SHA-256 pinned in the catalog.

## Layout

```
src/batchroom/
  config.py        env-driven Settings
  server.py        app factory, lifespan, auth, static UI
  cli.py           serve / run / recipes / models
  api/routes.py    REST endpoints
  core/            image_io, imaging (geometry), matting (guided filter),
                   recipe, pipeline, manifest, output_naming, errors
  ops/             base (registry, ItemState) + one module per op family
  models/          registry (catalog) + manager (download/verify/sessions)
  queue/           db, jobs (queue+dispatcher), events (SSE bus), webhooks
  watch/           watcher
  ui/              index.html, app.js, style.css (no build chain)
  i18n/strings/    10 locale catalogs (en.json is source of truth)
  recipes/         built-in recipes
tests/             unit + integration + golden/ (regression corpus)
```

## The open-core boundary (real, shipped)

The MIT core stays complete: all ops, unlimited volume, watch folders,
API, recipes, single-user auth. The paid layer lives in
`src/batchroom/pro/` (source-available, LICENSE-PRO):

- `pro/license.py` — Ed25519 offline key verification; entitlements map
  tiers → feature flags. The core only ever *asks* for flags.
- `pro/users.py` — accounts/sessions/API keys on the same SQLite DB
  (scrypt + hashed tokens; schema v2 migration is core so community
  databases stay upgradeable).
- `pro/emailer.py` — SMTP notifications.
- `api/auth.py` + `api/routes_pro.py` — actor resolution and the
  license/users endpoints; when no license/users exist, behavior is
  byte-for-byte the community single-user model.

Rule enforced by design: removing `pro/` must never break a community
feature — gates default to "feature absent", not "error".
