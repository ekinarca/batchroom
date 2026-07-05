#!/usr/bin/env python3
"""License administration (operator-side). NEVER runs on customer servers.

    new-keypair   generate a signing keypair (private stays on YOUR machine)
    issue         sign a license key for a customer
    inspect       verify/decode a key string offline

Runbook: docs/pro/SELLING.md. The private key never enters the repository;
losing it means issuing a new key_id (add to PUBLIC_KEYS) — old licenses
keep verifying against the old id.

Usage:
    python scripts/license_admin.py new-keypair --key-id prod-2026
    python scripts/license_admin.py issue --key-id prod-2026 \
        --tier pro --licensee "Acme GmbH" --email ops@acme.example --days 366
    python scripts/license_admin.py inspect BR1.eyJ...
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from batchroom.pro.license import PREFIX, canonical_payload, parse_license

DEFAULT_KEY_DIR = Path.home() / ".batchroom-keys"


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _private_key_path(key_dir: Path, key_id: str) -> Path:
    return key_dir / f"batchroom-signing-{key_id}.pem"


def cmd_new_keypair(args) -> int:
    key_dir = Path(args.key_dir).expanduser()
    key_dir.mkdir(parents=True, exist_ok=True)
    path = _private_key_path(key_dir, args.key_id)
    if path.exists():
        print(f"refusing to overwrite existing private key: {path}", file=sys.stderr)
        return 1
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    path.chmod(0o600)
    public_hex = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    print(f"private key: {path}  (chmod 600 — BACK THIS UP, never commit)")
    print(f"key_id:      {args.key_id}")
    print(f"public key (embed in src/batchroom/pro/license.py PUBLIC_KEYS):\n  \"{args.key_id}\": \"{public_hex}\",")
    return 0


def _load_private(key_dir: Path, key_id: str) -> Ed25519PrivateKey:
    path = _private_key_path(Path(key_dir).expanduser(), key_id)
    if not path.is_file():
        raise SystemExit(f"private key not found: {path} (run new-keypair first)")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"{path} is not an Ed25519 private key")
    return key


def cmd_issue(args) -> int:
    private = _load_private(args.key_dir, args.key_id)
    issued = date.today()
    payload = {
        "v": 1,
        "key_id": args.key_id,
        "tier": args.tier,
        "licensee": args.licensee,
        "email": args.email,
        "issued": issued.isoformat(),
        "expires": (issued + timedelta(days=args.days)).isoformat() if args.days else None,
        "seats": args.seats,
    }
    if payload["expires"] is None:
        del payload["expires"]
    body = canonical_payload(payload)
    signature = private.sign(body)
    key = f"{PREFIX}.{_b64e(body)}.{_b64e(signature)}"
    check = parse_license(key)
    if not check.valid:
        print(f"internal error: issued key fails verification: {check.error}", file=sys.stderr)
        return 1
    print(key)
    print(
        f"\n# {args.tier} · {args.licensee} <{args.email}> · seats {args.seats}"
        f" · expires {payload.get('expires', 'never')}",
        file=sys.stderr,
    )
    return 0


def cmd_inspect(args) -> int:
    text = args.key
    try:
        if len(text) < 512 and Path(text).is_file():
            text = Path(text).read_text(encoding="utf-8")
    except OSError:
        pass  # not a path — treat as a raw key string
    info = parse_license(text)
    print(json.dumps(info.to_public_dict(), indent=2))
    return 0 if info.valid else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new-keypair", help="generate a signing keypair")
    p_new.add_argument("--key-id", default="prod-2026")
    p_new.add_argument("--key-dir", default=str(DEFAULT_KEY_DIR))
    p_new.set_defaults(func=cmd_new_keypair)

    p_issue = sub.add_parser("issue", help="issue a customer license key")
    p_issue.add_argument("--key-id", default="prod-2026")
    p_issue.add_argument("--key-dir", default=str(DEFAULT_KEY_DIR))
    p_issue.add_argument("--tier", choices=["pro", "enterprise"], required=True)
    p_issue.add_argument("--licensee", required=True)
    p_issue.add_argument("--email", required=True)
    p_issue.add_argument("--days", type=int, default=366, help="0 = perpetual")
    p_issue.add_argument("--seats", type=int, default=None)
    p_issue.set_defaults(func=cmd_issue)

    p_inspect = sub.add_parser("inspect", help="verify/decode a key")
    p_inspect.add_argument("key", help="key string or path to a key file")
    p_inspect.set_defaults(func=cmd_inspect)

    args = parser.parse_args()
    if getattr(args, "seats", None) is None and getattr(args, "tier", None):
        args.seats = 10 if args.tier == "pro" else 100
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
