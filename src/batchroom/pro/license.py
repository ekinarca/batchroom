"""Offline license keys: Ed25519-signed, no phone-home.

Key string format::

    BR1.<base64url(payload json)>.<base64url(signature)>

The payload is canonical JSON (sorted keys, compact separators); the
signature covers exactly those bytes. Verification needs only the public
keys embedded below — servers never contact anything to validate, which
is the self-hosted contract (plan Faz 3: "imzalı offline anahtar").

Issuing keys requires the *private* key, which lives only on the
operator's machine (see scripts/license_admin.py and docs/pro/SELLING.md).
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PREFIX = "BR1"

# key_id -> raw Ed25519 public key (hex). Rotation = add a new id here.
PUBLIC_KEYS: dict[str, str] = {
    # Production signing key, generated 2026-07-05. Private half lives in
    # the operator's ~/.batchroom-keys/ — never in this repository.
    "prod-2026": "d04c0723d6734f1fe45f3bb16ece51db7f5535eaed89f4ad8172303891c9459e",
}

TIER_FEATURES: dict[str, set[str]] = {
    "pro": {"multi_user", "priority_queue", "email_notifications"},
    "enterprise": {
        "multi_user",
        "priority_queue",
        "email_notifications",
        # reserved for the enterprise roadmap (SSO, audit log):
        "sso",
        "audit_log",
    },
}

DEFAULT_SEATS = {"pro": 10, "enterprise": 100}

LICENSE_FILENAME = "license.key"


@dataclass
class LicenseInfo:
    """Result of parsing a key. ``valid`` means signature + schema are good;
    an expired-but-authentic key stays ``valid`` with ``expired=True`` so the
    UI can show *whose* license lapsed instead of pretending none exists."""

    raw: str = ""
    tier: str = "community"
    licensee: str = ""
    email: str = ""
    issued: str = ""
    expires: str | None = None
    seats: int = 1
    key_id: str = ""
    valid: bool = False
    expired: bool = False
    error: str | None = None

    @property
    def active(self) -> bool:
        return self.valid and not self.expired

    def features(self) -> set[str]:
        return entitlements(self)

    def to_public_dict(self) -> dict:
        return {
            "tier": self.tier if self.valid else "community",
            "licensee": self.licensee,
            "email": self.email,
            "issued": self.issued,
            "expires": self.expires,
            "seats": self.seats,
            "valid": self.valid,
            "expired": self.expired,
            "active": self.active,
            "error": self.error,
            "features": sorted(self.features()),
        }


def entitlements(info: LicenseInfo | None) -> set[str]:
    if info is None or not info.active:
        return set()
    return set(TIER_FEATURES.get(info.tier, set()))


def canonical_payload(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _b64d(part: str) -> bytes:
    pad = "=" * (-len(part) % 4)
    return base64.urlsafe_b64decode(part + pad)


def _fail(raw: str, message: str) -> LicenseInfo:
    return LicenseInfo(raw=raw, valid=False, error=message)


def parse_license(
    text: str, *, extra_keys: dict[str, str] | None = None, today: date | None = None
) -> LicenseInfo:
    """Verify a key string. Never raises — returns LicenseInfo with error set."""
    raw = (text or "").strip()
    if not raw:
        return _fail(raw, "empty license key")
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        return _fail(raw, "not a Batchroom license key (expected BR1.<payload>.<sig>)")
    try:
        payload_bytes = _b64d(parts[1])
        signature = _b64d(parts[2])
        payload = json.loads(payload_bytes)
    except (binascii.Error, ValueError):
        return _fail(raw, "malformed license key")
    if not isinstance(payload, dict):
        return _fail(raw, "malformed license payload")

    key_id = str(payload.get("key_id", ""))
    keys = {**PUBLIC_KEYS, **(extra_keys or {})}
    pub_hex = keys.get(key_id)
    if not pub_hex:
        return _fail(raw, f"unknown signing key '{key_id}'")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
        public_key.verify(signature, canonical_payload(payload))
    except (InvalidSignature, ValueError):
        return _fail(raw, "signature verification failed — key was altered or forged")

    tier = str(payload.get("tier", ""))
    if tier not in TIER_FEATURES:
        return _fail(raw, f"unknown tier '{tier}'")

    expires = payload.get("expires")
    expired = False
    if expires is not None:
        try:
            expiry_date = date.fromisoformat(str(expires))
        except ValueError:
            return _fail(raw, "malformed expiry date")
        # 3-day grace so a renewal weekend never interrupts a production queue
        expired = (today or datetime.now(UTC).date()) > expiry_date + timedelta(days=3)

    return LicenseInfo(
        raw=raw,
        tier=tier,
        licensee=str(payload.get("licensee", "")),
        email=str(payload.get("email", "")),
        issued=str(payload.get("issued", "")),
        expires=str(expires) if expires is not None else None,
        seats=int(payload.get("seats") or DEFAULT_SEATS.get(tier, 1)),
        key_id=key_id,
        valid=True,
        expired=expired,
    )


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def license_path(settings) -> Path:
    return settings.data_dir / LICENSE_FILENAME


def load_license(settings, *, extra_keys: dict[str, str] | None = None) -> LicenseInfo:
    path = license_path(settings)
    if not path.is_file():
        return LicenseInfo(tier="community", valid=False, error=None)
    try:
        return parse_license(path.read_text(encoding="utf-8"), extra_keys=extra_keys)
    except OSError as exc:  # pragma: no cover - unlikely
        return LicenseInfo(tier="community", valid=False, error=f"cannot read license: {exc}")


def save_license(settings, text: str) -> None:
    path = license_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def remove_license(settings) -> bool:
    path = license_path(settings)
    if path.is_file():
        path.unlink()
        return True
    return False
