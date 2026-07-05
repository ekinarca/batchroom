"""API surface tests via the FastAPI TestClient (full lifespan)."""

import time

import pytest
from fastapi.testclient import TestClient

from batchroom.core.image_io import encode_image
from batchroom.server import create_app

from .conftest import gradient_image


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def upload_bytes():
    return encode_image(gradient_image(64, 48), "jpg")


def wait_done(client, job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job never finished")


class TestSystemAndCatalogs:
    def test_health_open(self, client):
        assert client.get("/api/health").json()["status"] == "ok"

    def test_system(self, client):
        data = client.get("/api/system").json()
        assert data["app"] == "batchroom"
        assert data["queue"] == {"pending": 0, "running": 0}
        assert data["formats"]["jpg"] is True

    def test_recipes_list_builtins(self, client):
        names = {r["name"] for r in client.get("/api/recipes").json()["recipes"]}
        assert {"ecom-packshot", "marketplace-pack", "steam-capsule-pack"} <= names

    def test_models_expose_licenses(self, client):
        models = client.get("/api/models").json()["models"]
        assert all(m["license"] and m["license_url"] for m in models)
        bria = [m for m in models if "bria" in m["id"].lower()]
        assert not bria  # NC-licensed models must never appear

    def test_i18n(self, client):
        langs = client.get("/api/i18n").json()["languages"]
        assert len(langs) == 10
        assert any(lang["rtl"] for lang in langs)
        tr = client.get("/api/i18n/tr").json()
        en = client.get("/api/i18n/en").json()
        assert set(tr) == set(en)
        fallback = client.get("/api/i18n/xx").json()
        assert fallback == en

    def test_docs_served(self, client):
        assert client.get("/api/openapi.json").status_code == 200


class TestRecipeEndpoints:
    def test_validate_good_and_bad(self, client):
        good = "name: ok-r\nsteps:\n  - op: trim\n"
        assert client.post("/api/recipes/validate", json={"text": good}).json()["valid"]
        bad = client.post("/api/recipes/validate", json={"text": "name: bad\nsteps: []"}).json()
        assert not bad["valid"] and "steps" in bad["error"]

    def test_save_get_delete_user_recipe(self, client):
        text = "name: mine-x\ndescription: d\nsteps:\n  - op: trim\n"
        r = client.post("/api/recipes", json={"text": text})
        assert r.status_code == 201
        detail = client.get("/api/recipes/mine-x").json()
        assert detail["source"] == text
        assert not detail["builtin"]
        assert client.delete("/api/recipes/mine-x").json()["deleted"] == "mine-x"
        assert client.get("/api/recipes/mine-x").status_code == 404

    def test_delete_builtin_refused(self, client):
        assert client.delete("/api/recipes/ecom-packshot").status_code == 404

    def test_save_invalid_rejected(self, client):
        assert client.post("/api/recipes", json={"text": "name: x\nsteps: []"}).status_code == 400


class TestJobFlow:
    def test_upload_flow_and_files(self, client, settings):
        r = client.post(
            "/api/jobs",
            data={"recipe": "steam-capsule-pack"},
            files=[("files", ("art.jpg", upload_bytes(), "image/jpeg"))],
        )
        assert r.status_code == 202
        job = wait_done(client, r.json()["id"])
        assert job["done"] == 1
        outputs = job["items"][0]["result"]["outputs"]
        # original stem preserved (no upload-index prefix)
        assert outputs[0]["path"].endswith("art-main-capsule.png")
        got = client.get(f"/api/jobs/{job['id']}/outputs/{outputs[0]['path']}")
        assert got.status_code == 200
        assert client.get(f"/api/jobs/{job['id']}/thumb/0/in").status_code == 200
        archive = client.get(f"/api/jobs/{job['id']}/archive")
        assert archive.status_code == 200 and archive.content[:2] == b"PK"

    def test_path_traversal_blocked(self, client):
        r = client.post(
            "/api/jobs",
            data={"recipe": "steam-capsule-pack"},
            files=[("files", ("a.jpg", upload_bytes(), "image/jpeg"))],
        )
        job = wait_done(client, r.json()["id"])
        blocked = client.get(f"/api/jobs/{job['id']}/outputs/../../secret")
        assert blocked.status_code in (400, 404)

    def test_json_path_mode(self, client, tmp_path):
        from batchroom.core.image_io import save_image

        src = tmp_path / "server-side.png"
        save_image(gradient_image(48, 48), src, "png")
        out = tmp_path / "results"
        r = client.post(
            "/api/jobs",
            json={"recipe": "steam-capsule-pack", "paths": [str(src)], "output_dir": str(out)},
        )
        assert r.status_code == 202
        job = wait_done(client, r.json()["id"])
        assert job["done"] == 1
        assert (out / "manifest.json").is_file()

    def test_unknown_recipe_404(self, client):
        r = client.post(
            "/api/jobs", data={"recipe": "nope"},
            files=[("files", ("a.jpg", upload_bytes(), "image/jpeg"))],
        )
        assert r.status_code == 404

    def test_no_processable_files(self, client):
        r = client.post(
            "/api/jobs", data={"recipe": "steam-capsule-pack"},
            files=[("files", ("notes.txt", b"hello", "text/plain"))],
        )
        assert r.status_code == 400

    def test_job_listing_and_delete(self, client):
        r = client.post(
            "/api/jobs", data={"recipe": "steam-capsule-pack"},
            files=[("files", ("a.jpg", upload_bytes(), "image/jpeg"))],
        )
        job = wait_done(client, r.json()["id"])
        assert any(j["id"] == job["id"] for j in client.get("/api/jobs").json()["jobs"])
        assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
        assert client.get(f"/api/jobs/{job['id']}").status_code == 404


class TestAuth:
    def test_api_key_enforced(self, settings):
        settings.api_key = "sekret"
        app = create_app(settings)
        with TestClient(app) as c:
            assert c.get("/api/system").status_code == 401
            assert c.get("/api/health").status_code == 200  # healthcheck stays open
            assert c.get("/api/system", headers={"X-API-Key": "wrong"}).status_code == 401
            assert c.get("/api/system", headers={"X-API-Key": "sekret"}).status_code == 200
            assert c.get(
                "/api/system", headers={"Authorization": "Bearer sekret"}
            ).status_code == 200
            assert c.get("/api/system?api_key=sekret").status_code == 200
