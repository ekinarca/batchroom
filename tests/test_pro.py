"""Pro layer: license keys, multi-user auth, ownership, priority, email."""

from __future__ import annotations

import base64
import json
import time
from datetime import date, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from batchroom.core.image_io import encode_image
from batchroom.pro import license as license_mod
from batchroom.pro import parse_license, save_license
from batchroom.pro.emailer import EmailNotifier, SmtpConfig
from batchroom.pro.users import (
    UserError,
    authenticate,
    create_session,
    create_user,
    hash_password,
    resolve_session,
    verify_password,
)
from batchroom.queue.db import Database
from batchroom.server import create_app

from .golden.gen import gradient_image

TEST_KEY_ID = "test-suite"
_PRIVATE = Ed25519PrivateKey.generate()
_PUBLIC_HEX = _PRIVATE.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
).hex()


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def issue_key(tier="pro", days=366, seats=None, licensee="Test Co", key_id=TEST_KEY_ID):
    payload = {
        "v": 1, "key_id": key_id, "tier": tier, "licensee": licensee,
        "email": "buyer@test.example", "issued": date.today().isoformat(),
        "seats": seats or (10 if tier == "pro" else 100),
    }
    if days:
        payload["expires"] = (date.today() + timedelta(days=days)).isoformat()
    body = license_mod.canonical_payload(payload)
    return f"{license_mod.PREFIX}.{_b64e(body)}.{_b64e(_PRIVATE.sign(body))}"


@pytest.fixture(autouse=True)
def _register_test_pubkey(monkeypatch):
    monkeypatch.setitem(license_mod.PUBLIC_KEYS, TEST_KEY_ID, _PUBLIC_HEX)


@pytest.fixture(autouse=True)
def _fresh_login_limits():
    """The login limiters are process-wide; isolate tests from each other."""
    from batchroom.api import auth

    auth._login_limiter.buckets.clear()
    auth._source_limiter.buckets.clear()
    yield


@pytest.fixture()
def licensed_settings(settings):
    save_license(settings, issue_key())
    return settings


def upload_bytes():
    return encode_image(gradient_image(48, 48), "jpg")


# ---------------------------------------------------------------------------
# license parsing
# ---------------------------------------------------------------------------


class TestLicense:
    def test_valid_key(self):
        info = parse_license(issue_key())
        assert info.valid and info.active and info.tier == "pro"
        assert info.features() == {"multi_user", "priority_queue", "email_notifications"}
        assert info.seats == 10

    def test_enterprise_superset(self):
        info = parse_license(issue_key(tier="enterprise"))
        assert {"multi_user", "sso", "audit_log"} <= info.features()

    def test_tampered_signature(self):
        key = issue_key()
        assert not parse_license(key[:-4] + "AAAA").valid

    def test_forged_payload(self):
        key = issue_key()
        payload = json.loads(base64.urlsafe_b64decode(key.split(".")[1] + "=="))
        payload["seats"] = 99999
        forged = (
            f"{license_mod.PREFIX}."
            f"{_b64e(license_mod.canonical_payload(payload))}.{key.split('.')[2]}"
        )
        assert not parse_license(forged).valid

    def test_unknown_key_id(self):
        info = parse_license(issue_key(key_id="nobody"))
        assert not info.valid and "unknown signing key" in info.error

    def test_expiry_and_grace(self):
        key = issue_key(days=10)
        soon = date.today() + timedelta(days=12)  # inside 3-day grace
        later = date.today() + timedelta(days=20)
        assert parse_license(key, today=soon).active
        expired = parse_license(key, today=later)
        assert expired.valid and expired.expired and not expired.active
        assert expired.features() == set()

    def test_garbage_inputs(self):
        for junk in ("", "hello", "BR1.x", "BR1.!!.!!", "BR2.a.b"):
            assert not parse_license(junk).valid

    def test_perpetual_key(self):
        info = parse_license(issue_key(days=0))
        assert info.active and info.expires is None


# ---------------------------------------------------------------------------
# password / session primitives
# ---------------------------------------------------------------------------


class TestUserPrimitives:
    def test_password_roundtrip(self):
        stored = hash_password("hunter2boat")
        assert verify_password("hunter2boat", stored)
        assert not verify_password("hunter2boaT", stored)
        assert not verify_password("hunter2boat", "garbage")

    def test_short_password_rejected(self):
        with pytest.raises(UserError, match="8 characters"):
            hash_password("short")

    def test_sessions(self, settings):
        db = Database(settings.db_path)
        user = create_user(db, email="a@b.co", password="longenough", role="admin")
        token = create_session(db, user["id"])
        assert resolve_session(db, token)["email"] == "a@b.co"
        assert resolve_session(db, "wrong") is None
        assert resolve_session(db, None) is None

    def test_seat_limit(self, settings):
        db = Database(settings.db_path)
        create_user(db, email="a@b.co", password="longenough", seat_limit=2)
        create_user(db, email="b@b.co", password="longenough", seat_limit=2)
        with pytest.raises(UserError, match="seat limit"):
            create_user(db, email="c@b.co", password="longenough", seat_limit=2)

    def test_last_admin_protected(self, settings):
        from batchroom.pro.users import delete_user, update_user

        db = Database(settings.db_path)
        admin = create_user(db, email="a@b.co", password="longenough", role="admin")
        with pytest.raises(UserError, match="last admin"):
            update_user(db, admin["id"], role="member")
        with pytest.raises(UserError, match="last admin"):
            delete_user(db, admin["id"])

    def test_authenticate(self, settings):
        db = Database(settings.db_path)
        create_user(db, email="x@y.co", password="longenough")
        assert authenticate(db, "X@Y.CO", "longenough")  # case-insensitive email
        assert authenticate(db, "x@y.co", "wrongwrong") is None
        assert authenticate(db, "ghost@y.co", "longenough") is None


# ---------------------------------------------------------------------------
# multi-user API flows
# ---------------------------------------------------------------------------


@pytest.fixture()
def multiuser_client(licensed_settings):
    db = Database(licensed_settings.db_path)
    create_user(db, email="admin@co.example", name="Admin", password="admin-pass-1",
                role="admin")
    create_user(db, email="member@co.example", name="Member", password="member-pass-1",
                role="member")
    app = create_app(licensed_settings)
    with TestClient(app) as client:
        yield client


def login(client, email, password):
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


class TestAuthFlows:
    def test_anonymous_locked_out(self, multiuser_client):
        c = multiuser_client
        assert c.get("/api/system").status_code == 401
        me = c.get("/api/auth/me").json()
        assert me["authenticated"] is False and me["multiuser"] is True
        assert me["license"]["tier"] == "pro"

    def test_login_logout(self, multiuser_client):
        c = multiuser_client
        login(c, "admin@co.example", "admin-pass-1")
        assert c.get("/api/system").status_code == 200
        assert c.get("/api/auth/me").json()["role"] == "admin"
        c.post("/api/auth/logout")
        assert c.get("/api/system").status_code == 401

    def test_wrong_password_and_rate_limit(self, licensed_settings):
        db = Database(licensed_settings.db_path)
        create_user(db, email="solo@co.example", password="admin-pass-1", role="admin")
        app = create_app(licensed_settings)
        with TestClient(app) as c:
            statuses = [
                c.post("/api/auth/login",
                       json={"email": "solo@co.example", "password": f"nope-{i}"}).status_code
                for i in range(6)
            ]
            assert statuses[:5] == [401] * 5
            assert statuses[5] == 429  # rate limited

    def test_spraying_emails_hits_the_source_limit(self, licensed_settings):
        db = Database(licensed_settings.db_path)
        create_user(db, email="solo@co.example", password="admin-pass-1", role="admin")
        with TestClient(create_app(licensed_settings)) as c:
            codes = [
                c.post("/api/auth/login",
                       json={"email": f"x{i}@co.example", "password": "nope-nope"}).status_code
                for i in range(31)
            ]
        assert codes[:30] == [401] * 30 and codes[30] == 429

    def test_limiter_not_reset_by_spraying_keys(self):
        from batchroom.api.auth import _RateLimiter

        limiter = _RateLimiter()
        for _ in range(5):
            limiter.hit("1.2.3.4:admin@co.example")
        assert limiter.blocked("1.2.3.4:admin@co.example")
        for i in range(1001):  # the old table was wiped once it held 1000 keys
            limiter.hit(f"1.2.3.4:spray{i}@co.example")
        assert limiter.blocked("1.2.3.4:admin@co.example")

    def test_member_job_isolation(self, multiuser_client):
        c = multiuser_client
        login(c, "member@co.example", "member-pass-1")
        r = c.post("/api/jobs", data={"recipe": "steam-capsule-pack"},
                   files=[("files", ("m.jpg", upload_bytes(), "image/jpeg"))])
        assert r.status_code == 202
        member_job = r.json()["id"]

        login(c, "admin@co.example", "admin-pass-1")
        r = c.post("/api/jobs", data={"recipe": "steam-capsule-pack"},
                   files=[("files", ("a.jpg", upload_bytes(), "image/jpeg"))])
        admin_job = r.json()["id"]
        admin_sees = {j["id"] for j in c.get("/api/jobs").json()["jobs"]}
        assert {member_job, admin_job} <= admin_sees

        login(c, "member@co.example", "member-pass-1")
        member_sees = {j["id"] for j in c.get("/api/jobs").json()["jobs"]}
        assert member_job in member_sees and admin_job not in member_sees
        assert c.get(f"/api/jobs/{admin_job}").status_code == 404  # no existence leak
        assert c.get(f"/api/jobs/{admin_job}/thumb/0/in").status_code == 404
        assert c.get(f"/api/jobs/{admin_job}/archive").status_code == 404

    def test_member_cannot_manage(self, multiuser_client):
        c = multiuser_client
        login(c, "member@co.example", "member-pass-1")
        assert c.get("/api/users").status_code == 403
        assert c.post("/api/license", json={"key": issue_key()}).status_code == 403
        assert c.delete("/api/models/u2netp").status_code == 403  # shared weights: admins only
        # members may still browse recipes/models
        assert c.get("/api/recipes").status_code == 200
        assert c.get("/api/models").status_code == 200

    def test_user_admin_crud(self, multiuser_client):
        c = multiuser_client
        login(c, "admin@co.example", "admin-pass-1")
        r = c.post("/api/users", json={
            "email": "new@co.example", "name": "New", "password": "new-pass-123",
        })
        assert r.status_code == 201
        uid = r.json()["id"]
        assert any(u["email"] == "new@co.example" for u in c.get("/api/users").json()["users"])
        assert c.patch(f"/api/users/{uid}", json={"role": "admin"}).json()["role"] == "admin"
        assert c.patch(f"/api/users/{uid}", json={"disabled": True}).json()["disabled"] is True
        assert c.delete(f"/api/users/{uid}").status_code == 200

    def test_duplicate_email_rejected(self, multiuser_client):
        c = multiuser_client
        login(c, "admin@co.example", "admin-pass-1")
        r = c.post("/api/users", json={
            "email": "member@co.example", "password": "whatever-123",
        })
        assert r.status_code == 400

    def test_per_user_api_key(self, multiuser_client):
        c = multiuser_client
        login(c, "admin@co.example", "admin-pass-1")
        me = c.get("/api/auth/me").json()["user"]
        raw = c.post(f"/api/users/{me['id']}/api-key").json()["api_key"]
        assert raw.startswith("brk_")
        c.post("/api/auth/logout")
        assert c.get("/api/system").status_code == 401
        assert c.get("/api/system", headers={"X-API-Key": raw}).status_code == 200

    def test_env_key_still_admin(self, licensed_settings):
        licensed_settings.api_key = "automation-key"
        db = Database(licensed_settings.db_path)
        create_user(db, email="admin@co.example", password="admin-pass-1", role="admin")
        app = create_app(licensed_settings)
        with TestClient(app) as c:
            assert c.get("/api/system").status_code == 401
            r = c.get("/api/users", headers={"X-API-Key": "automation-key"})
            assert r.status_code == 200  # env key = admin equivalent

    def test_license_install_endpoint(self, settings):
        app = create_app(settings)  # community, single-user mode
        with TestClient(app) as c:
            assert c.get("/api/license").json()["tier"] == "community"
            r = c.post("/api/license", json={"key": issue_key()})
            assert r.status_code == 200 and r.json()["tier"] == "pro"
            assert c.get("/api/system").json()["license"]["active"] is True
            bad = c.post("/api/license", json={"key": "BR1.junk.junk"})
            assert bad.status_code == 400
            assert c.delete("/api/license").json()["tier"] == "community"


# ---------------------------------------------------------------------------
# priority queue
# ---------------------------------------------------------------------------


class TestPriority:
    def _stopped_queue(self, settings):
        """A JobQueue that never dispatches — lets us inspect claim order."""
        from batchroom.core.recipe import discover_recipes, recipe_dirs
        from batchroom.models.manager import ModelManager
        from batchroom.queue.events import EventBus
        from batchroom.queue.jobs import JobQueue

        (settings.recipes_dir / "tiny.yaml").write_text(
            "name: tiny\nsteps:\n  - op: trim\n", encoding="utf-8"
        )

        def provider():
            recipes, _ = discover_recipes(recipe_dirs(settings))
            return recipes

        return JobQueue(
            settings, Database(settings.db_path), ModelManager(settings.models_dir),
            EventBus(), provider,
        )

    def test_high_priority_claims_first(self, settings, tmp_path):
        from batchroom.core.image_io import save_image

        queue = self._stopped_queue(settings)
        recipe = queue.recipes_provider()["tiny"]
        low = tmp_path / "low.png"
        high = tmp_path / "high.png"
        save_image(gradient_image(16, 16), low, "png")
        save_image(gradient_image(16, 16), high, "png")
        queue.submit(recipe, [low], priority=0)
        time.sleep(0.01)  # ensure distinct created_at ordering scenario
        queue.submit(recipe, [high], priority=5)
        claimed = queue._claim_next()
        assert claimed["source_name"] == "high.png"

    def test_priority_clamped_without_license(self, settings, tmp_path):
        app = create_app(settings)  # community
        with TestClient(app) as c:
            r = c.post("/api/jobs", data={"recipe": "steam-capsule-pack", "priority": "9"},
                       files=[("files", ("x.jpg", upload_bytes(), "image/jpeg"))])
            assert r.json()["priority"] == 0

    def test_priority_honored_with_license(self, licensed_settings):
        app = create_app(licensed_settings)
        with TestClient(app) as c:
            r = c.post("/api/jobs", data={"recipe": "steam-capsule-pack", "priority": "9"},
                       files=[("files", ("x.jpg", upload_bytes(), "image/jpeg"))])
            assert r.json()["priority"] == 9


# ---------------------------------------------------------------------------
# email notifications
# ---------------------------------------------------------------------------


class TestEmail:
    def test_smtp_url_parsing(self):
        cfg = SmtpConfig.from_url(
            "smtp://no%40reply:p%40ss@mail.example.com:587?tls=starttls&from=bot@example.com"
        )
        assert cfg.host == "mail.example.com" and cfg.port == 587
        assert cfg.username == "no@reply" and cfg.password == "p@ss"
        assert cfg.tls == "starttls" and cfg.sender == "bot@example.com"
        assert SmtpConfig.from_url("smtps://u:p@h").tls == "ssl"
        assert SmtpConfig.from_url("http://nope") is None
        assert SmtpConfig.from_url("smtp://host-only") is None  # no sender

    def test_message_content(self):
        cfg = SmtpConfig("h", 587, None, None, "none", "bot@x.co")
        notifier = EmailNotifier(cfg)
        manifest = {
            "recipe": {"name": "ecom-packshot"},
            "counts": {"total": 3, "done": 2, "failed": 1, "cancelled": 0},
            "started_at": "s", "finished_at": "f",
            "items": [{"status": "failed", "source": "bad.jpg", "error": "cannot decode"}],
        }
        message = notifier._build_message(
            "ops@x.co", {"id": "j-1", "status": "done", "output_dir": "/out"}, manifest
        )
        assert message["To"] == "ops@x.co"
        assert "2 ok, 1 failed" in message["Subject"]
        assert "bad.jpg: cannot decode" in message.get_content()

    def test_end_to_end_notification(self, licensed_settings, monkeypatch):
        licensed_settings.smtp_url = "smtp://mail.example.com?from=bot@example.com"
        licensed_settings.notify_email = "ops@example.com"
        sent = []
        monkeypatch.setattr(
            EmailNotifier, "_deliver", lambda self, message: sent.append(message)
        )
        app = create_app(licensed_settings)
        with TestClient(app) as c:
            r = c.post("/api/jobs", data={"recipe": "steam-capsule-pack"},
                       files=[("files", ("x.jpg", upload_bytes(), "image/jpeg"))])
            jid = r.json()["id"]
            deadline = time.time() + 15
            while time.time() < deadline:
                if c.get(f"/api/jobs/{jid}").json()["status"] == "done" and sent:
                    break
                time.sleep(0.05)
        assert sent, "no notification email captured"
        assert sent[0]["To"] == "ops@example.com"
        assert jid in sent[0]["Subject"]
