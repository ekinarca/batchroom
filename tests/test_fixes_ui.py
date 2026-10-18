"""Regression tests for Pro-layer fixes found during the UI/pro bug hunt."""

from __future__ import annotations

import base64
from datetime import date, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from batchroom.pro import license as license_mod
from batchroom.pro import parse_license
from batchroom.pro import users as users_mod
from batchroom.pro.users import authenticate, create_user
from batchroom.queue.db import Database

KEY_ID = "test-fixes-ui"
_PRIVATE = Ed25519PrivateKey.generate()
_PUBLIC_HEX = _PRIVATE.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
).hex()


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _sign(payload: dict) -> str:
    body = license_mod.canonical_payload(payload)
    return f"{license_mod.PREFIX}.{_b64e(body)}.{_b64e(_PRIVATE.sign(body))}"


@pytest.mark.parametrize("seats", ["ten", [3], 1e309])
def test_malformed_seat_count_is_rejected_not_raised(seats):
    payload = {
        "v": 1, "key_id": KEY_ID, "tier": "pro", "licensee": "X", "email": "x@y.co",
        "issued": date.today().isoformat(),
        "expires": (date.today() + timedelta(days=30)).isoformat(),
        "seats": seats,
    }
    info = parse_license(_sign(payload), extra_keys={KEY_ID: _PUBLIC_HEX})
    assert not info.valid
    assert "seat" in (info.error or "")


def test_unknown_account_costs_one_scrypt_like_a_known_one(settings, monkeypatch):
    """Absent accounts used to hash a fresh dummy password on every attempt
    (two scrypt runs instead of one), which made them measurably slower."""
    db = Database(settings.db_path)
    create_user(db, email="known@y.co", password="longenough")
    authenticate(db, "ghost@y.co", "warm-up-pw")  # builds the equalizer hash once

    calls = []
    real = users_mod.hashlib.scrypt

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(users_mod.hashlib, "scrypt", counting)
    assert authenticate(db, "ghost@y.co", "whatever-pw") is None
    absent = len(calls)
    calls.clear()
    assert authenticate(db, "known@y.co", "wrong-password") is None
    assert absent == len(calls) == 1
