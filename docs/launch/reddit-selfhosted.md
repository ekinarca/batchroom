# r/selfhosted draft

**Title:**

> Batchroom – self-hosted batch background removal + image pipelines (Photoroom alternative). No meters, no cloud, single container

**Body:**

After the hundredth "you've used your 40 free background removals this
month" popup I did the r/selfhosted thing and built it for my own server.

**What it is:** a single Docker container that watches folders / takes
uploads / exposes an API, and runs recipe pipelines over images:
background removal → white canvas → every marketplace size → renamed →
manifest. AI models (ISNet, U²-Net, BiRefNet, Real-ESRGAN) run locally
on CPU via ONNX — no GPU needed, downloads are SHA-256 pinned.

**Why you might care:**

- `docker compose up -d`, one volume, no Redis/Postgres/broker. SQLite.
- Watch folders are polling + size-stability based, so they work on
  SMB/NFS mounts (inotify doesn't).
- Drop 500 photos → `done/` and `failed/` sorting, outputs + manifest
  in your output share. Interrupted batches resume after restart.
- Web UI in 10 languages (incl. RTL Arabic), dark, no build chain.
- amd64 + arm64 images — u2netp/silueta models are small & fast enough
  for ARM NAS boxes.
- EXIF stripped from outputs by default. No telemetry. Air-gap friendly
  (pre-pull models, cut network).
- MIT core, and the model catalog only contains commercially-usable
  weights (licenses shown in the UI — no BRIA-style NC surprises).

**Honest limits:** CPU 4× upscaling is slow (~10 s per 512² tile-set on
a fast CPU — queue it, don't watch it). No video, on purpose. Single
user for now (API key auth).

Repo + compose: https://github.com/ekinarca/batchroom

Happy to answer anything about the watch-folder semantics or model
licensing. Recipe ideas welcome — marketplace size packs are curated in
the repo, PRs encouraged when platforms change specs.

---

*Post plan: different day than HN. Also adapt for r/DataHoarder
(archive conversion angle: HEIC→JPG, EXIF-strip, manifest w/ hashes)
and r/gamedev (steam-capsule-pack angle: every capsule size from one
key art in 5 seconds).*
