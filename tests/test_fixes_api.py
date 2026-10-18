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
