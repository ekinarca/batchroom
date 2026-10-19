# Security

## Reporting

Please report vulnerabilities privately via GitHub Security Advisories
("Report a vulnerability" on the repo). You'll get an answer within 72 hours.
Please don't open public issues for security reports.

## Trust model

Batchroom follows the standard self-hosted trust model (same as
Stirling-PDF & friends): it is designed to run on localhost, a trusted
LAN, or behind your own reverse proxy — not to be exposed raw to the
internet.

- **Authentication is optional and off by default.** Set
  `BATCHROOM_API_KEY` to require a key (header `X-API-Key`,
  `Authorization: Bearer`, or `?api_key=` for SSE/downloads). Do this
  before exposing the port beyond localhost.
- **Multi-user mode (Pro)** replaces that with real accounts: scrypt
  password hashes, server-side sessions (only SHA-256 of tokens is
  stored; cookies are HttpOnly + SameSite=Lax), per-user API keys
  (`brk_…`, stored hashed), admin/member roles, login rate-limiting
  (5 failed attempts/min per account and source, 30/min per source —
  spraying many emails doesn't reset the counter), and job-level
  ownership — members can only see and download their own jobs
  (non-owners get 404, not 403, so job ids don't leak); only admins can
  change what every user shares: delete model weights, save or delete
  recipes, and reload the watch-folder config. The env API key keeps working as an
  admin equivalent (compared in constant time) so
  existing automations survive the upgrade. We deliberately did not add
  CSRF tokens: state-changing routes are JSON/fetch-only, cookies are
  SameSite=Lax and CORS stays closed — document this if you put the API
  on a shared origin.
- **`/api/health` is always unauthenticated** (container healthchecks).
  It leaks only the app name and version.
- **The JSON job mode reads server-side paths** (`{"paths": [...]}`) —
  by design, for NAS/CI workflows. Anyone who can reach the API can
  submit any path readable by the Batchroom user for processing.
  This is the main reason to set an API key on shared networks, and to
  run the container non-root (the image does, uid 1000).

## Hardening that's built in

- Output/thumbnail serving resolves and verifies paths stay inside the
  job's output directory (no traversal).
- Uploaded filenames are sanitized; uploads are size-capped
  (`BATCHROOM_MAX_UPLOAD_MB`, default 512 MB/file).
- Decompression-bomb guard: images over ~300 MP are refused at decode.
- EXIF (including GPS) is **stripped from outputs by default**; recipes
  must opt in with `keep_metadata: true`.
- Model downloads verify a pinned SHA-256 before the file is used; a
  mismatch aborts and deletes the download.
- Job deletion only removes directories Batchroom itself created under
  its data dir — never watch-folder sources or custom output dirs.
- No telemetry, no phone-home, no auto-update. Air-gapped operation is
  supported: pre-pull models, then cut the network.

## Reverse proxy notes

Terminate TLS at your proxy (Caddy/Traefik/nginx) and forward to
`batchroom:5151`. SSE (`/api/events`) needs response buffering disabled;
Batchroom already sends `X-Accel-Buffering: no` for nginx.

Login rate limits are keyed by client address. When the proxy runs in
another container, Batchroom sees every client as the proxy's address
unless uvicorn trusts its `X-Forwarded-For` header: set
`FORWARDED_ALLOW_IPS` to the proxy's address (e.g. `172.18.0.2`, or `*`
on a network only the proxy can reach).
