"""Regression tests for API-layer fixes (routes / auth)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from batchroom.api.auth import Actor
from batchroom.api.routes import events_stream
from batchroom.core.image_io import save_image
from batchroom.pro.users import create_user
from batchroom.queue.db import Database
from batchroom.server import create_app

from .conftest import gradient_image
from .test_api import client, upload_bytes, wait_done  # noqa: F401  (fixture)
from .test_pro import (  # noqa: F401  (autouse fixtures)
    _fresh_login_limits,
    _register_test_pubkey,
    licensed_settings,
    login,
)


class TestJobOutputs:
    def test_output_route_serves_only_recorded_outputs(self, client, tmp_path):  # noqa: F811
        # a job writing into an existing folder used to hand out any file in it
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "unrelated.txt").write_text("someone else's file")
        src = tmp_path / "art.png"
        save_image(gradient_image(48, 48), src, "png")
        r = client.post("/api/jobs", json={
            "recipe": "steam-capsule-pack", "paths": [str(src)], "output_dir": str(shared),
        })
        job = wait_done(client, r.json()["id"])
        assert client.get(f"/api/jobs/{job['id']}/outputs/unrelated.txt").status_code == 404
        path = job["items"][0]["result"]["outputs"][0]["path"]
        assert client.get(f"/api/jobs/{job['id']}/outputs/{path}").status_code == 200

    def test_overlong_output_path_is_404_not_500(self, client):  # noqa: F811
        r = client.post(
            "/api/jobs", data={"recipe": "steam-capsule-pack"},
            files=[("files", ("a.jpg", upload_bytes(), "image/jpeg"))],
        )
        job = wait_done(client, r.json()["id"])
        assert client.get(f"/api/jobs/{job['id']}/outputs/{'x' * 5000}").status_code == 404


class TestJsonModeErrors:
    def test_unstat_able_path_is_400(self, client):  # noqa: F811
        # OSError other than ENOENT (EACCES, ENAMETOOLONG) escaped as a 500
        r = client.post("/api/jobs", json={
            "recipe": "steam-capsule-pack", "paths": ["/tmp/" + "x" * 5000],
        })
        assert r.status_code == 400


def _collect_events(app, actor: Actor, publish) -> list[dict]:
    """Drive the SSE generator directly: publish after subscribing, then read
    until the trailing ``test.marker`` event."""

    async def disconnected() -> bool:
        return False

    request = SimpleNamespace(
        app=app, state=SimpleNamespace(actor=actor), is_disconnected=disconnected
    )

    async def run() -> list[dict]:
        response = await events_stream(request)
        body = response.body_iterator
        assert (await body.__anext__()).startswith(": connected")
        publish(app.state.events)
        app.state.events.publish("test.marker")
        seen = []
        try:
            while True:
                chunk = await asyncio.wait_for(body.__anext__(), timeout=5.0)
                if chunk.startswith("data: "):
                    event = json.loads(chunk[6:])
                    if event["type"] == "test.marker":
                        return seen
                    seen.append(event)
        finally:
            await body.aclose()

    return asyncio.run(run())


class TestEventStreamOwnership:
    def test_members_only_get_their_own_job_events(self, licensed_settings):  # noqa: F811
        db = Database(licensed_settings.db_path)
        create_user(db, email="admin@co.example", password="admin-pass-1", role="admin")
        member = create_user(db, email="member@co.example", password="member-pass-1")
        app = create_app(licensed_settings)
        with TestClient(app) as c:
            login(c, "member@co.example", "member-pass-1")
            mine = c.post("/api/jobs", data={"recipe": "steam-capsule-pack"},
                          files=[("files", ("m.jpg", upload_bytes(), "image/jpeg"))]).json()
            wait_done(c, mine["id"])
            login(c, "admin@co.example", "admin-pass-1")
            other = c.post("/api/jobs", data={"recipe": "steam-capsule-pack"},
                           files=[("files", ("a.jpg", upload_bytes(), "image/jpeg"))]).json()
            wait_done(c, other["id"])
            queue = app.state.queue

            def publish(bus):
                for job_id in (other["id"], mine["id"]):
                    bus.publish("job.updated", job=queue.get_job(job_id))
                    bus.publish("item.progress", job_id=job_id, idx=0, message="m")
                bus.publish("model.ready", model="x")

            events = _collect_events(app, Actor(kind="user", user=member), publish)
            ids = {e.get("job", {}).get("id") or e.get("job_id") for e in events}
            assert other["id"] not in ids  # admin's job never reaches the member
            assert mine["id"] in ids
            assert any(e["type"] == "model.ready" for e in events)

            admin_events = _collect_events(app, Actor(kind="env"), publish)
            assert len(admin_events) == 5


class TestJsonModeBadInput:
    def test_unknown_home_dir_is_400(self, client):  # noqa: F811
        for body in (
            {"paths": ["~nosuchuser-br/x.jpg"]},
            {"paths": ["/tmp"], "output_dir": "~nosuchuser-br/out"},
        ):
            r = client.post("/api/jobs", json={"recipe": "steam-capsule-pack", **body})
            assert r.status_code == 400, r.text

    def test_nul_in_output_dir_is_400(self, client, tmp_path):  # noqa: F811
        src = tmp_path / "art.png"
        save_image(gradient_image(48, 48), src, "png")
        r = client.post("/api/jobs", json={
            "recipe": "steam-capsule-pack", "paths": [str(src)], "output_dir": "/tmp/a\x00b",
        })
        assert r.status_code == 400, r.text

    def test_malformed_webhook_url_is_400(self, client):  # noqa: F811
        r = client.post("/api/jobs", json={
            "recipe": "steam-capsule-pack", "paths": ["/tmp"], "webhook_url": "http://[::1/hook",
        })
        assert r.status_code == 400

    def test_huge_offset_is_not_500(self, client):  # noqa: F811
        r = client.get("/api/jobs", params={"offset": 10**20})
        assert r.status_code == 200 and r.json()["jobs"] == []


class TestExtraRecipeDirs:
    def test_save_and_delete_answer_409_instead_of_shadowing(self, settings, tmp_path):
        extra = tmp_path / "team-recipes"
        extra.mkdir()
        (extra / "team.yaml").write_text(
            "name: team\ndescription: old\nsteps:\n  - op: convert\n    format: png\n"
        )
        settings.extra_recipe_dirs = [extra]
        with TestClient(create_app(settings)) as c:
            text = "name: team\ndescription: new\nsteps:\n  - op: convert\n    format: png\n"
            assert c.post("/api/recipes", json={"text": text}).status_code == 409
            assert not (settings.recipes_dir / "team.yaml").exists()
            assert c.delete("/api/recipes/team").status_code == 409
            assert (extra / "team.yaml").exists()


class TestSharedConfigIsAdminOnly:
    def test_members_cannot_change_recipes_or_reload_watch(self, licensed_settings):  # noqa: F811
        db = Database(licensed_settings.db_path)
        create_user(db, email="admin@co.example", password="admin-pass-1", role="admin")
        create_user(db, email="member@co.example", password="member-pass-1")
        text = "name: team-recipe\ndescription: d\nsteps:\n  - op: convert\n    format: png\n"
        with TestClient(create_app(licensed_settings)) as c:
            login(c, "member@co.example", "member-pass-1")
            assert c.post("/api/recipes", json={"text": text}).status_code == 403
            assert c.post("/api/watch/reload").status_code == 403
            assert c.post("/api/recipes/validate", json={"text": text}).json()["valid"]
            login(c, "admin@co.example", "admin-pass-1")
            assert c.post("/api/recipes", json={"text": text}).status_code == 201
            login(c, "member@co.example", "member-pass-1")
            assert c.delete("/api/recipes/team-recipe").status_code == 403
            assert c.get("/api/recipes/team-recipe").status_code == 200


class TestLoginBurst:
    def test_concurrent_wrong_passwords_respect_the_limit(self, licensed_settings):  # noqa: F811
        from concurrent.futures import ThreadPoolExecutor

        db = Database(licensed_settings.db_path)
        create_user(db, email="admin@co.example", password="admin-pass-1", role="admin")
        with TestClient(create_app(licensed_settings)) as c:
            def attempt(_):
                return c.post("/api/auth/login", json={
                    "email": "admin@co.example", "password": "wrong-pass",
                }).status_code

            with ThreadPoolExecutor(20) as pool:
                codes = list(pool.map(attempt, range(20)))
            assert codes.count(401) == 5
            assert codes.count(429) == 15

    def test_successful_login_does_not_count_as_failure(self, licensed_settings):  # noqa: F811
        db = Database(licensed_settings.db_path)
        create_user(db, email="admin@co.example", password="admin-pass-1", role="admin")
        with TestClient(create_app(licensed_settings)) as c:
            for _ in range(6):
                login(c, "admin@co.example", "admin-pass-1")


class TestDeletedEventReachesOwner:
    def test_member_hears_deletion_of_own_job(self, licensed_settings):  # noqa: F811
        db = Database(licensed_settings.db_path)
        create_user(db, email="admin@co.example", password="admin-pass-1", role="admin")
        member = create_user(db, email="member@co.example", password="member-pass-1")
        app = create_app(licensed_settings)
        with TestClient(app) as c:
            login(c, "member@co.example", "member-pass-1")
            job = c.post("/api/jobs", data={"recipe": "steam-capsule-pack"},
                         files=[("files", ("m.jpg", upload_bytes(), "image/jpeg"))]).json()
            wait_done(c, job["id"])
            owner = app.state.queue.get_job(job["id"])["user_id"]
            app.state.queue.delete(job["id"])

            def publish(bus):  # what delete() published, replayed for a fresh client
                bus.publish("job.deleted", job_id=job["id"], user_id=owner)
                bus.publish("job.deleted", job_id="j-other", user_id=owner + 1)

            events = _collect_events(app, Actor(kind="user", user=member), publish)
            assert [e["job_id"] for e in events] == [job["id"]]


class TestSmtpTls:
    def test_certificates_are_verified_by_default(self, monkeypatch):
        import ssl

        from batchroom.pro.emailer import EmailNotifier, SmtpConfig

        contexts = []

        class FakeSMTP:
            def __init__(self, *args, context=None, **kwargs):
                contexts.append(context)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self, context=None):
                contexts.append(context)

            def login(self, *args):
                pass

            def send_message(self, message):
                pass

        monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
        monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)
        for url in ("smtp://u:p@mail.example.com:587?from=a@b.c", "smtps://u:p@mail.example.com"):
            EmailNotifier(SmtpConfig.from_url(url))._deliver(None)
        tls = [c for c in contexts if c is not None]
        assert len(tls) == 2
        assert all(c.verify_mode == ssl.CERT_REQUIRED and c.check_hostname for c in tls)

        contexts.clear()
        cfg = SmtpConfig.from_url("smtps://u:p@relay.lan?verify=0")
        EmailNotifier(cfg)._deliver(None)
        assert contexts[0].verify_mode == ssl.CERT_NONE


class TestLicenseExpiresWhileRunning:
    def test_expiry_is_checked_on_read(self):
        from datetime import date, timedelta

        from batchroom.pro import parse_license

        from .test_pro import issue_key

        info = parse_license(issue_key(days=1))
        assert info.active
        info.today = date.today() + timedelta(days=5)  # the server just kept running
        assert info.expired and not info.active and info.features() == set()


class TestSystemAuthState:
    def test_multiuser_mode_reports_auth_required(self, licensed_settings):  # noqa: F811
        db = Database(licensed_settings.db_path)
        create_user(db, email="admin@co.example", password="admin-pass-1", role="admin")
        with TestClient(create_app(licensed_settings)) as c:
            login(c, "admin@co.example", "admin-pass-1")
            assert c.get("/api/system").json()["auth_required"] is True
