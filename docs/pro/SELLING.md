# Operator runbook: selling Batchroom Pro

Internal doc for the maintainer. Customers never need this file.

## One-time setup (already done on the dev machine, 2026-07-05)

The production signing keypair exists:

- private: `~/.batchroom-keys/batchroom-signing-prod-2026.pem` (chmod 600)
  — **BACK THIS UP** to a password manager / encrypted drive. Losing it
  means new key_id rotation; leaking it means anyone can mint licenses.
- public: embedded in `src/batchroom/pro/license.py` as `prod-2026`.

If you ever need a new keypair:

```bash
python scripts/license_admin.py new-keypair --key-id prod-2027
# add the printed public key to PUBLIC_KEYS (keep the old id for old keys)
```

## Fulfilling a sale (manual, fine up to ~50 sales/mo)

1. Receive payment (see storefront options below).
2. Issue the key:

```bash
python scripts/license_admin.py issue \
  --tier pro --licensee "Acme GmbH" --email ops@acme.example \
  --days 366 --seats 10
# yearly = 366 days (leap-safe); monthly = 32 days; trial = 30 days
# enterprise: --tier enterprise --seats 100
```

3. Email the customer the key string with install instructions:

> Thanks for supporting Batchroom! Your Pro license key is below.
> Install it in the UI (System → License → paste → Activate) or:
> `batchroom license install 'BR1.…'`
> Docs: https://github.com/ekinarca/batchroom/blob/main/docs/PRO.md
> Renewal: you'll get a fresh key before this one expires.

4. Log the sale (spreadsheet: date, licensee, email, tier, expiry) —
   you need this for renewals; there is no server-side registry by design.

## Renewals

Offline keys can't auto-renew. Calendar reminder ~2 weeks before expiry
→ send payment link → issue a fresh key on payment. The 3-day grace in
the verifier absorbs weekend renewals.

## Storefront options — Turkey reality (verified 2026-07-05)

The operator is a Turkey resident. Hard constraints: **Stripe has no
merchant support for Turkey** (not in its 46 supported countries) and
**PayPal does not operate in Turkey** — so anything PayPal-payout-only
is out, and direct Stripe requires a foreign entity.

| Option | Fee | Turkey status | Notes |
|---|---|---|---|
| **Polar.sh** ✅ recommended | ~4% + 40¢ | **Türkiye explicitly in their payout country list** (Stripe Connect Express payouts) | OSS-native MoR: they are the seller, handle VAT/sales tax, license-key webhooks; you just receive payouts |
| **Paddle** ✅ fallback | ~5% + 50¢ | Sellers worldwide; payout via **wire or Payoneer** (a $15 SWIFT fee can apply) | Established MoR; stricter onboarding review (wants a live site) |
| Lemon Squeezy | 5% + 50¢ | Unverified for TR bank payouts; PayPal payouts unusable in TR | Check their bank-payout country list before relying on it |
| Stripe direct | 1.5-2.9% | ❌ Not without a **US LLC** (Atlas ~$500 + yearly US filings) | Revisit only at serious MRR; you'd also handle VAT yourself |

TR side: to invoice payouts legally you'll want a şahıs şirketi + mali
müşavir; ask about the software/service-export tax advantages
(hizmet ihracatı KDV istisnası, kazanç indirimi — accountant confirms
eligibility). MoR platforms bill the end customer, so you invoice only
the platform's payout — much simpler bookkeeping.

Automation path when manual gets old: a tiny webhook receiver (can run
on the same box) that calls `license_admin.py issue` on
`order.completed` and emails the key. ~50 lines; write it when sales > 1/day.

## Refunds

14 days no-questions. The key keeps working until expiry — acceptable
loss; goodwill is worth more.

## Price anchors (from the plan, 2026-07)

Pro €24/mo/server vs Photoroom Ultra $82.50/mo *metered* and Stirling-PDF
Server $99/mo. Do not discount below €19 except LTD experiments.
