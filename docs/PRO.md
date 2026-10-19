# Batchroom Pro

The core is MIT and complete: every image operation, unlimited volume,
watch folders, API, recipes — forever, for everyone. **Pro exists for
teams and companies**, and pays for the maintenance that keeps the free
core healthy (the Stirling-PDF model).

## What Pro adds

| | Community (MIT) | **Pro** | Enterprise |
|---|---|---|---|
| All image ops, unlimited volume | ✔ | ✔ | ✔ |
| Watch folders, REST API, webhooks, CLI | ✔ | ✔ | ✔ |
| 10-language web UI | ✔ | ✔ | ✔ |
| Optional single API key | ✔ | ✔ | ✔ |
| **Team accounts + roles** (members see their jobs, admins see all) | — | ✔ (10 seats) | ✔ (100 seats) |
| **Per-user API keys** | — | ✔ | ✔ |
| **Priority queue** (urgent batches jump the line) | — | ✔ | ✔ |
| **Email notifications** on job completion | — | ✔ | ✔ |
| Priority support (48h) | — | ✔ | ✔ (SLA) |
| SSO (OIDC/SAML), audit log | — | — | roadmap |
| License type | — | offline signed key, per server, **no phone-home** | offline / air-gapped |

## Pricing

- **Pro: €24 / month or €240 / year, per server.** Unlimited images —
  that is one outsourced-editing *day* at typical per-image rates
  ($0.02–0.10/image cloud APIs; $0.80+/photo human editing), for a month
  of unmetered batches.
- **Enterprise: from €99 / month** — SSO/audit roadmap access, more
  seats, SLA. Talk to us.

**How to buy:** **[→ Buy Batchroom Pro](https://buy.polar.sh/polar_cl_IOVj9pe9dlWU428JxxwJIal3GDohw7ZaurvbQ4NLXGO)**
(choose monthly or yearly at checkout; payments handled by Polar as
merchant of record — they take care of VAT/invoicing). Your license key
is emailed within one business day of purchase. Questions or trial keys:
**ekinarca40@gmail.com** with subject "Batchroom Pro".

## How licensing works (honestly)

- Your key is an **Ed25519-signed file** — the server validates it
  offline. No activation servers, no phone-home, no telemetry. Air-gapped
  installs work.
- Install it in the UI (System → License), or:
  `batchroom license install BR1.…`
- Subscriptions get a key valid for the paid period (+3-day grace).
  **If it expires, nobody gets locked out**: existing accounts and jobs
  keep working; user management and Pro features pause until renewal.
- One key per server. Honor-system fair use — we'd rather build features
  than DRM.

## Setup (5 minutes)

```bash
batchroom license install BR1.…                       # or paste in the UI
batchroom users create-admin --email you@company.com  # first admin
# then in the UI: Users → New user (members see only their own jobs)
```

Optional Pro configuration:

```bash
BATCHROOM_SMTP_URL=smtp://user:pass@mail.example.com:587?from=batchroom@example.com
BATCHROOM_NOTIFY_EMAIL=ops@example.com   # fallback recipient
# The mail server's TLS certificate is verified; for an internal relay with a
# self-signed certificate add &verify=0 to the URL.
```

Priority per job: the **Queue priority** selector in the UI, or
`-F priority=10` / `"priority": 10` on `POST /api/jobs`.

## FAQ

**Can I try Pro first?** Yes — email us for a 30-day trial key.

**What happens to my data if I stop paying?** Nothing. Your server, your
files, your accounts — Pro *features* pause, data never does.

**Is the Pro source visible?** Yes — `src/batchroom/pro/` ships in the
repo under [LICENSE-PRO](../LICENSE-PRO) (source-available). The MIT core
never depends on it.

**Why should a solo self-hoster pay?** You shouldn't. Community is the
full product for individuals — that's the point.
