# Show HN draft

**Title:**

> Show HN: Batchroom – self-hosted alternative to Photoroom/remove.bg, no meters

**Body:**

Hi HN — I built Batchroom because I kept hitting the same wall: every
"remove background" tool is a metered cloud API. $0.02–0.10 per image is
fine for ten images and absurd for a catalog of 5,000 — and for client
work the images often aren't allowed to leave the building at all.

Batchroom is a self-hosted server (single Docker container, no external
services) that runs *pipelines* over batches of images:

- background removal (ISNet/U²-Net/BiRefNet — permissive-licensed ONNX,
  CPU-first, checksum-pinned downloads)
- resize/crop/pad to marketplace specs (Amazon/Etsy/Shopify/Trendyol,
  Steam capsules), format conversion incl. HEIC/AVIF, EXIF stripping,
  4x Real-ESRGAN upscale
- everything driven by YAML "recipes", with a web UI, REST API +
  webhooks, and watch folders for the NAS crowd

Design choices people here might care about:

- No Redis/broker — SQLite (WAL) + an in-process worker pool. One
  process, one volume.
- Interrupted batches resume; originals are never touched; every batch
  writes a manifest.json with SHA-256s of outputs.
- Honest CPU numbers on the README (measured, not marketing).
- Model catalog is permissive-licensed only — the popular BRIA weights
  are CC BY-NC, so they're deliberately excluded; the catalog shows the
  license before download.
- UI ships in 10 languages incl. full RTL Arabic.

The core is MIT and stays complete (no feature ransom). If it grows,
the plan is a paid layer for org stuff (SSO/multi-user/audit) — the
Stirling-PDF model, not the bait-and-switch model.

Repo: https://github.com/ekinarca/batchroom
Quick start: `docker run -p 5151:5151 -v batchroom-data:/data ghcr.io/ekinarca/batchroom`

I'd love feedback on the recipe format and what ops you'd want next.

---

*Post plan: Tue–Thu, 8-10am ET. First comment: architecture notes +
"ask me anything about the licensing audit of model weights".*
