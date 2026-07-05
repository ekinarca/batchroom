#!/usr/bin/env python3
"""Acceptance suite: verify every product promise against a REAL server.

Unlike the unit/golden suites (hermetic, stub models), this boots actual
``batchroom serve`` processes with the real downloaded models and checks
the claims made in README.md / docs/PRO.md / batchroom.app, end to end:

  formats, every bg-removal model, shipped-recipe size contracts, EXIF
  privacy, input immutability, manifest integrity, kill -9 resume, cancel,
  ZIP, upscale, priority ordering, multi-user isolation, license
  lifecycle, watch folders, webhooks, SSE, security guards, performance.

Usage:  .venv/bin/python scripts/acceptance_test.py
Requires: models downloaded in ./data/models (isnet-general, u2net,
u2netp, silueta, birefnet-general-lite, realesrgan-x4).
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import contextlib  # noqa: E402

import pillow_heif  # noqa: E402
from PIL import Image  # noqa: E402

pillow_heif.register_heif_opener()
with contextlib.suppress(Exception):
    pillow_heif.register_avif_opener()

RESULTS: list[tuple[str, bool, str]] = []
DEMO_INPUTS = REPO / "docs" / "assets" / "demo-inputs"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """A real `batchroom serve` subprocess on a temp data dir."""

    def __init__(self, tag: str, *, workers: int = 2, watch: bool = False,
                 env_extra: dict | None = None, data_dir: Path | None = None):
        self.tmp = Path(tempfile.mkdtemp(prefix=f"br-accept-{tag}-"))
        self.data = data_dir or (self.tmp / "data")
        self.data.mkdir(exist_ok=True)
        models = self.data / "models"
        if not models.exists():
            models.symlink_to(REPO / "data" / "models")
        self.port = free_port()
        self.workers = workers
        self.watch = watch
        self.env_extra = env_extra or {}
        self.proc: subprocess.Popen | None = None
        self.base = f"http://127.0.0.1:{self.port}"

    def start(self) -> Server:
        env = {**os.environ, "BATCHROOM_DATA_DIR": str(self.data),
               "BATCHROOM_WATCH": "1" if self.watch else "0", **self.env_extra}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "batchroom", "serve", "--host", "127.0.0.1",
             "--port", str(self.port), "--workers", str(self.workers)],
            env=env, cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.base}/api/health", timeout=2).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.3)
        raise RuntimeError(f"server {self.port} never became healthy")

    def kill9(self) -> None:
        assert self.proc is not None
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=10)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def cleanup(self) -> None:
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


def wait_done(client: httpx.Client, base: str, job_id: str, timeout: float = 300,
              headers: dict | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"{base}/api/jobs/{job_id}", headers=headers or {}).json()
        if job["status"] in ("done", "cancelled"):
            return job
        time.sleep(0.25)
    raise TimeoutError(f"job {job_id} did not finish")


def submit_files(client, base, recipe, files, headers=None, **form) -> dict:
    payload = [("files", (name, data, "application/octet-stream")) for name, data in files]
    r = client.post(f"{base}/api/jobs", data={"recipe": recipe, **form}, files=payload,
                    headers=headers or {}, timeout=120)
    r.raise_for_status()
    return r.json()


def photo_bytes(name: str = "mug.jpg") -> bytes:
    return (DEMO_INPUTS / name).read_bytes()


def make_exif_jpeg() -> bytes:
    """JPEG carrying EXIF incl. GPS + camera model."""
    img = Image.open(DEMO_INPUTS / "serum.jpg").copy()
    exif = Image.Exif()
    exif[272] = "AcceptanceCam 9000"           # Model
    exif[34853] = {1: "N", 2: (41.0, 0.0, 0.0), 3: "E", 4: (29.0, 0.0, 0.0)}  # GPSInfo
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, exif=exif)
    return buf.getvalue()


# ---------------------------------------------------------------------------
def main() -> int:  # noqa: PLR0915
    issues: list[str] = []
    t_start = time.time()

    # =====================================================================
    print("\n[1/8] Core pipeline promises (single server, real models)")
    s = Server("core", workers=4).start()
    c = httpx.Client(timeout=120)
    try:
        # -- format roundtrip in and out --------------------------------
        fmt_recipe = "name: fmt-{f}\nsteps:\n  - op: convert\n    format: {f}\noutput: { structure: flat }\n"
        src_img = Image.open(io.BytesIO(photo_bytes())).resize((640, 478))
        for fmt in ["jpg", "png", "webp", "avif", "heic", "tiff"]:
            c.post(f"{s.base}/api/recipes",
                   json={"text": fmt_recipe.replace("{f}", fmt)}).raise_for_status()
            buf = io.BytesIO()
            save_fmt = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP", "avif": "AVIF",
                        "heic": "HEIF", "tiff": "TIFF"}[fmt]
            try:
                src_img.save(buf, format=save_fmt, quality=90)
            except Exception:
                src_img.save(buf, format=save_fmt)
            job = submit_files(c, s.base, f"fmt-{fmt}", [(f"in.{fmt}", buf.getvalue())])
            job = wait_done(c, s.base, job["id"])
            ok = job["done"] == 1
            if ok:
                out = job["items"][0]["result"]["outputs"][0]
                blob = c.get(f"{s.base}/api/jobs/{job['id']}/outputs/{out['path']}").content
                decoded = Image.open(io.BytesIO(blob))
                ok = decoded.size == (640, 478)
            check(f"format roundtrip {fmt} in→{fmt} out", ok,
                  "" if ok else str(job["items"][0].get("error")))

        # -- every bg-removal model on a real photo ----------------------
        for model in ["isnet-general", "u2net", "u2netp", "silueta", "birefnet-general-lite"]:
            recipe = (f"name: bg-{model}\nsteps:\n  - op: remove-background\n"
                      f"    model: {model}\n  - op: convert\n    format: png\n"
                      "output: { structure: flat }\n")
            c.post(f"{s.base}/api/recipes", json={"text": recipe}).raise_for_status()
            job = submit_files(c, s.base, f"bg-{model}", [("mug.jpg", photo_bytes())])
            job = wait_done(c, s.base, job["id"])
            ok, detail = job["done"] == 1, ""
            if ok:
                out = job["items"][0]["result"]["outputs"][0]
                blob = c.get(f"{s.base}/api/jobs/{job['id']}/outputs/{out['path']}").content
                px = Image.open(io.BytesIO(blob)).convert("RGBA")
                import numpy as np

                a = np.asarray(px)[:, :, 3]
                h, w = a.shape
                center = a[h // 2 - 40:h // 2 + 40, w // 2 - 40:w // 2 + 40].mean()
                corners = (a[:40, :40].mean() + a[:40, -40:].mean()
                           + a[-40:, :40].mean() + a[-40:, -40:].mean()) / 4
                ok = center > 200 and corners < 40
                detail = f"subject α={center:.0f}, corners α={corners:.0f}"
            check(f"bg-removal model {model}", ok, detail)

        # -- shipped recipe size contracts -------------------------------
        contracts = {
            "marketplace-pack": {"amazon-main": (2000, 2000), "etsy-listing": (2000, 1500),
                                 "shopify-square": (2048, 2048), "trendyol": (1200, 1800),
                                 "instagram-feed": (1080, 1080)},
            "steam-capsule-pack": {"main-capsule": (1232, 706), "header-capsule": (920, 430),
                                   "small-capsule": (462, 174), "vertical-capsule": (748, 896),
                                   "library-capsule": (600, 900), "library-hero": (3840, 1240)},
            "ecom-packshot": {"web-2048": (2048, 2048), "web-1024": (1024, 1024),
                              "transparent-1500": (1500, 1500)},
        }
        for recipe_name, expected in contracts.items():
            job = submit_files(c, s.base, recipe_name, [("sneaker.jpg", photo_bytes("sneaker.jpg"))])
            job = wait_done(c, s.base, job["id"])
            outs = {o["preset"]: (o["width"], o["height"])
                    for o in job["items"][0]["result"]["outputs"]}
            ok = job["done"] == 1 and outs == expected
            check(f"recipe contract {recipe_name}", ok,
                  "" if ok else f"got {outs}")

        # -- EXIF privacy -------------------------------------------------
        exif_jpg = make_exif_jpeg()
        strip_r = ("name: exif-strip\nsteps:\n  - op: convert\n    format: jpg\n"
                   "output: { structure: flat }\n")
        keep_r = ("name: exif-keep\nsteps:\n  - op: convert\n    format: jpg\n"
                  "    keep_metadata: true\noutput: { structure: flat }\n")
        for text in (strip_r, keep_r):
            c.post(f"{s.base}/api/recipes", json={"text": text}).raise_for_status()

        def output_exif(recipe_name: str) -> Image.Exif:
            job = submit_files(c, s.base, recipe_name, [("p.jpg", exif_jpg)])
            job = wait_done(c, s.base, job["id"])
            out = job["items"][0]["result"]["outputs"][0]
            blob = c.get(f"{s.base}/api/jobs/{job['id']}/outputs/{out['path']}").content
            return Image.open(io.BytesIO(blob)).getexif()

        stripped = output_exif("exif-strip")
        kept = output_exif("exif-keep")
        check("EXIF+GPS stripped by default", len(stripped) == 0,
              f"{len(stripped)} tags remain" if stripped else "")
        check("EXIF preserved with keep_metadata", kept.get(272) == "AcceptanceCam 9000",
              f"Model tag={kept.get(272)!r}")

        # -- immutability + manifest integrity + ZIP ---------------------
        src_dir = s.tmp / "sources"
        src_dir.mkdir()
        for n in ["mug.jpg", "serum.jpg", "sneaker.jpg"]:
            shutil.copy(DEMO_INPUTS / n, src_dir / n)
        before = {p.name: sha256(p.read_bytes()).hexdigest() for p in src_dir.iterdir()}
        out_dir = s.tmp / "path-out"
        r = c.post(f"{s.base}/api/jobs", json={
            "recipe": "ecom-packshot", "paths": [str(src_dir)], "output_dir": str(out_dir)})
        job = wait_done(c, s.base, r.json()["id"])
        after = {p.name: sha256(p.read_bytes()).hexdigest() for p in src_dir.iterdir()}
        check("originals never modified", before == after)

        manifest = json.loads((out_dir / "manifest.json").read_text())
        m_ok = manifest["counts"] == {"total": 3, "done": 3, "failed": 0, "cancelled": 0}
        hash_ok = size_ok = True
        for item in manifest["items"]:
            for o in item["outputs"]:
                f = out_dir / o["path"]
                data = f.read_bytes()
                hash_ok &= sha256(data).hexdigest() == o["sha256"]
                size_ok &= len(data) == o["bytes"]
                with Image.open(io.BytesIO(data)) as im:
                    size_ok &= im.size == (o["width"], o["height"])
        check("manifest counts/sha256/bytes/dims all truthful", m_ok and hash_ok and size_ok)

        zr = c.get(f"{s.base}/api/jobs/{job['id']}/archive")
        zf = zipfile.ZipFile(io.BytesIO(zr.content))
        disk = {p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file()}
        check("ZIP archive == outputs on disk", set(zf.namelist()) == disk,
              f"{len(zf.namelist())} entries")

        # -- upscale ------------------------------------------------------
        up_l = ("name: up-lanczos\nsteps:\n  - op: upscale\n    scale: 2\n    model: lanczos\n"
                "  - op: convert\n    format: png\noutput: { structure: flat }\n")
        up_e = ("name: up-esrgan\nsteps:\n  - op: resize\n    width: 320\n"
                "  - op: upscale\n    scale: 4\n    model: realesrgan-x4\n"
                "  - op: convert\n    format: png\noutput: { structure: flat }\n")
        for text in (up_l, up_e):
            c.post(f"{s.base}/api/recipes", json={"text": text}).raise_for_status()
        small = io.BytesIO()
        src_img.resize((400, 300)).save(small, format="PNG")
        job = submit_files(c, s.base, "up-lanczos", [("x.png", small.getvalue())])
        job = wait_done(c, s.base, job["id"])
        o = job["items"][0]["result"]["outputs"][0]
        check("upscale 2x lanczos", (o["width"], o["height"]) == (800, 600))
        job = submit_files(c, s.base, "up-esrgan", [("m.jpg", photo_bytes())])
        job = wait_done(c, s.base, job["id"], timeout=600)
        ok = job["done"] == 1
        detail = ""
        if ok:
            o = job["items"][0]["result"]["outputs"][0]
            ok = o["width"] == 1280
            detail = f"320→{o['width']}px in {job['items'][0]['result']['duration_ms']}ms"
        check("upscale 4x Real-ESRGAN", ok, detail)

        # -- cancel + SSE + security guards -------------------------------
        events: list[str] = []

        def sse_listen():
            try:
                with httpx.stream("GET", f"{s.base}/api/events", timeout=30) as resp:
                    for line in resp.iter_lines():
                        if line.startswith("data:"):
                            events.append(json.loads(line[5:])["type"])
                        if len(events) >= 3:
                            return
            except httpx.HTTPError:
                pass

        listener = threading.Thread(target=sse_listen, daemon=True)
        listener.start()
        time.sleep(0.5)
        many = [(f"c{i}.jpg", photo_bytes()) for i in range(12)]
        job = submit_files(c, s.base, "ecom-packshot", many)
        time.sleep(1.0)
        c.post(f"{s.base}/api/jobs/{job['id']}/cancel")
        job = wait_done(c, s.base, job["id"])
        statuses = {i["status"] for i in job["items"]}
        check("cancel mid-batch", job["status"] == "cancelled"
              and "cancelled" in statuses, f"item statuses={sorted(statuses)}")
        listener.join(timeout=10)
        check("SSE live events stream", len(events) >= 3, f"saw {events[:5]}")

        trav = c.get(f"{s.base}/api/jobs/{job['id']}/outputs/../../../../etc/passwd")
        check("path traversal blocked", trav.status_code in (400, 404),
              f"HTTP {trav.status_code}")

        bomb = Image.new("RGB", (20000, 15010), (250, 250, 250))
        bomb_buf = io.BytesIO()
        bomb.save(bomb_buf, format="PNG")
        job = submit_files(c, s.base, "fmt-jpg", [("bomb.png", bomb_buf.getvalue())])
        job = wait_done(c, s.base, job["id"])
        item = job["items"][0]
        check("decompression bomb refused cleanly", job["status"] == "done"
              and item["status"] == "failed" and "too large" in (item["error"] or ""),
              (item["error"] or "")[:70])

        # -- performance re-measurement -----------------------------------
        big = Image.open(io.BytesIO(photo_bytes())).resize((1920, 1080))
        big_buf = io.BytesIO()
        big.save(big_buf, format="JPEG", quality=92)
        perf_files = [(f"p{i}.jpg", big_buf.getvalue()) for i in range(8)]
        t0 = time.time()
        job = submit_files(c, s.base, "bg-isnet-general", perf_files)
        job = wait_done(c, s.base, job["id"], timeout=600)
        wall = time.time() - t0
        durs = [i["result"]["duration_ms"] for i in job["items"]]
        per_img = sum(durs) / len(durs) / 1000
        rate = int(len(perf_files) / wall * 3600)
        # Plan §8.2 promise: ≤2.5 s/image E2E on 8-core; also report throughput.
        check("perf: 1080p bg-removal within promised ≤2.5s/img E2E", per_img <= 2.5,
              f"{per_img:.2f}s/img under 4-way contention; {rate} images/hour E2E")
    finally:
        c.close()
        s.cleanup()

    # =====================================================================
    print("\n[2/8] Kill -9 crash resume")
    s = Server("resume", workers=2).start()
    c = httpx.Client(timeout=60)
    data_dir = s.data
    try:
        files = [(f"r{i}.jpg", photo_bytes()) for i in range(10)]
        job = submit_files(c, s.base, "marketplace-pack", files)
        job_id = job["id"]
        while True:
            j = c.get(f"{s.base}/api/jobs/{job_id}").json()
            if j["done"] >= 2:
                break
            time.sleep(0.2)
        s.kill9()
        check("server SIGKILLed mid-batch", True, f"{j['done']}/10 done at kill")
    finally:
        c.close()
    s2 = Server("resume2", workers=2, data_dir=data_dir).start()
    c = httpx.Client(timeout=60)
    try:
        job = wait_done(c, s2.base, job_id, timeout=600)
        outs = [o["path"] for i in job["items"] for o in (i["result"] or {}).get("outputs", [])]
        expected_count = 10 * 5  # marketplace-pack: 5 presets per image
        check("resume completes every item", job["status"] == "done" and job["done"] == 10,
              f"done={job['done']} failed={job['failed']}")
        check("resume produces exact output set (no dupes/gaps)",
              len(outs) == expected_count and len(set(outs)) == expected_count,
              f"{len(outs)} outputs")
    finally:
        c.close()
        s2.cleanup()

    # =====================================================================
    print("\n[3/8] Watch folder (live)")
    s = Server("watch", workers=2, watch=True).start()
    hot = s.tmp / "hot"
    hot.mkdir()
    out = s.tmp / "hot-out"
    (s.data / "watch.yaml").write_text(
        f"watch:\n  - folder: {hot}\n    recipe: steam-capsule-pack\n    output: {out}\n")
    c = httpx.Client(timeout=60)
    try:
        c.post(f"{s.base}/api/watch/reload")
        # growing file must NOT be picked up
        part = hot / "copying.png"
        part.write_bytes(b"x" * 1000)
        time.sleep(2.5)
        part.write_bytes(b"x" * 5000)
        # real file
        shutil.copy(DEMO_INPUTS / "mug.jpg", hot / "drop.jpg")
        deadline = time.time() + 60
        processed = False
        while time.time() < deadline:
            if (hot / "done" / "drop.jpg").is_file() and (out / "manifest.json").is_file():
                processed = True
                break
            time.sleep(1)
        check("watch: stable file processed → done/ + outputs + manifest", processed)
        check("watch: growing file left alone", (hot / "copying.png").exists()
              or (hot / "failed" / "copying.png").exists())
        n_before = len(list(out.rglob("*.png")))
        time.sleep(6)  # a few more scans: outputs must not be re-ingested
        n_after = len(list(out.rglob("*.png")))
        check("watch: own outputs never re-ingested", n_before == n_after and n_before == 6,
              f"{n_after} outputs stable")
    finally:
        c.close()
        s.cleanup()

    # =====================================================================
    print("\n[4/8] Webhook delivery")
    received: list[dict] = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    hook_srv = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=hook_srv.serve_forever, daemon=True).start()
    s = Server("hook", workers=2).start()
    c = httpx.Client(timeout=60)
    try:
        job = submit_files(c, s.base, "steam-capsule-pack", [("k.jpg", photo_bytes())],
                           webhook_url=f"http://127.0.0.1:{hook_srv.server_port}/x")
        wait_done(c, s.base, job["id"])
        deadline = time.time() + 15
        while time.time() < deadline and not received:
            time.sleep(0.2)
        ok = bool(received) and received[0]["job_id"] == job["id"] \
            and received[0]["counts"]["done"] == 1
        check("webhook POSTs manifest on completion", ok)
    finally:
        c.close()
        s.cleanup()
        hook_srv.shutdown()

    # =====================================================================
    print("\n[5/8] Pro: license lifecycle")
    key_dir = Path.home() / ".batchroom-keys"
    issue = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "license_admin.py"), "issue",
         "--tier", "pro", "--licensee", "Acceptance Co", "--email", "a@b.co", "--days", "366"],
        capture_output=True, text=True, cwd=REPO)
    pro_key = issue.stdout.strip().splitlines()[-1]
    check("keygen issues a valid key", issue.returncode == 0 and pro_key.startswith("BR1."))

    # expired key (signed with the real prod key, expiry in the past)
    from cryptography.hazmat.primitives import serialization as ser

    priv = ser.load_pem_private_key(
        (key_dir / "batchroom-signing-prod-2026.pem").read_bytes(), password=None)
    payload = {"v": 1, "key_id": "prod-2026", "tier": "pro", "licensee": "Old",
               "email": "old@x.co", "issued": "2025-01-01", "expires": "2025-02-01",
               "seats": 10}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    b64 = lambda d: base64.urlsafe_b64encode(d).decode().rstrip("=")  # noqa: E731
    expired_key = f"BR1.{b64(body)}.{b64(priv.sign(body))}"

    s = Server("pro", workers=1).start()
    c = httpx.Client(timeout=60)
    try:
        r = c.post(f"{s.base}/api/license", json={"key": "BR1.garbage.garbage"})
        check("forged license rejected", r.status_code == 400)
        r = c.post(f"{s.base}/api/license", json={"key": expired_key})
        check("expired license rejected at install", r.status_code == 400,
              r.json().get("detail", "")[:60])
        r = c.post(f"{s.base}/api/license", json={"key": pro_key})
        feats = set(r.json().get("features", []))
        check("valid Pro license activates features",
              r.status_code == 200 and feats == {"email_notifications", "multi_user",
                                                 "priority_queue"}, str(sorted(feats)))
        job = submit_files(c, s.base, "steam-capsule-pack", [("p.jpg", photo_bytes())],
                           priority="9")
        check("priority honored when licensed", job["priority"] == 9)
        c.delete(f"{s.base}/api/license")
        job = submit_files(c, s.base, "steam-capsule-pack", [("p.jpg", photo_bytes())],
                           priority="9")
        check("priority clamped to 0 without license", job["priority"] == 0)
        wait_done(c, s.base, job["id"])
    finally:
        c.close()
        s.cleanup()

    # =====================================================================
    print("\n[6/8] Pro: multi-user isolation + priority ordering")
    s = Server("users", workers=1, env_extra={"BATCHROOM_API_KEY": "env-admin"}).start()
    c = httpx.Client(timeout=120)
    admin_h = {"X-API-Key": "env-admin"}
    try:
        c.post(f"{s.base}/api/license", json={"key": pro_key}, headers=admin_h)
        for email, role in [("boss@acme.co", "admin"), ("worker@acme.co", "member")]:
            r = c.post(f"{s.base}/api/users", headers=admin_h,
                       json={"email": email, "password": "acceptance-1", "role": role})
            r.raise_for_status()
        r = c.get(f"{s.base}/api/system")
        check("anonymous locked out in multi-user mode", r.status_code == 401)

        def login(email):
            cl = httpx.Client(timeout=120)
            cl.post(f"{s.base}/api/auth/login",
                    json={"email": email, "password": "acceptance-1"}).raise_for_status()
            return cl

        boss, worker = login("boss@acme.co"), login("worker@acme.co")
        wj = submit_files(worker, s.base, "steam-capsule-pack", [("w.jpg", photo_bytes())])
        bj = submit_files(boss, s.base, "steam-capsule-pack", [("b.jpg", photo_bytes())])
        wait_done(boss, s.base, bj["id"])
        wait_done(worker, s.base, wj["id"])
        worker_view = {j["id"] for j in worker.get(f"{s.base}/api/jobs").json()["jobs"]}
        boss_view = {j["id"] for j in boss.get(f"{s.base}/api/jobs").json()["jobs"]}
        leak = worker.get(f"{s.base}/api/jobs/{bj['id']}")
        check("member sees only own jobs", wj["id"] in worker_view
              and bj["id"] not in worker_view)
        check("member gets 404 (not 403) for others' jobs", leak.status_code == 404)
        check("admin sees all jobs", {wj["id"], bj["id"]} <= boss_view)
        check("member blocked from user management",
              worker.get(f"{s.base}/api/users").status_code == 403)

        # priority ordering with 1 worker: normal flood, then urgent job
        flood = submit_files(boss, s.base, "marketplace-pack",
                             [(f"f{i}.jpg", photo_bytes()) for i in range(6)])
        urgent = submit_files(boss, s.base, "steam-capsule-pack",
                              [("u.jpg", photo_bytes())], priority="10")
        u_done = wait_done(boss, s.base, urgent["id"], timeout=600)
        flood_now = boss.get(f"{s.base}/api/jobs/{flood['id']}").json()
        check("priority job overtakes the queue", u_done["status"] == "done"
              and flood_now["status"] == "running",
              f"urgent done while flood at {flood_now['done']}/6")
        wait_done(boss, s.base, flood["id"], timeout=900)
        boss.close()
        worker.close()
    finally:
        c.close()
        s.cleanup()

    # =====================================================================
    print("\n[7/8] i18n + docs endpoints")
    s = Server("i18n", workers=1).start()
    c = httpx.Client(timeout=30)
    try:
        langs = c.get(f"{s.base}/api/i18n").json()["languages"]
        en = c.get(f"{s.base}/api/i18n/en").json()
        parity = all(set(c.get(f"{s.base}/api/i18n/{L['code']}").json()) == set(en)
                     for L in langs)
        check("10 locales, full key parity, ar=RTL", len(langs) == 10 and parity
              and any(L["rtl"] for L in langs if L["code"] == "ar"))
        check("OpenAPI served", c.get(f"{s.base}/api/openapi.json").status_code == 200)
        check("UI served at /", "Batchroom" in c.get(f"{s.base}/").text)
    finally:
        c.close()
        s.cleanup()

    # =====================================================================
    print("\n[8/8] Summary")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"\n{'=' * 64}\n{passed}/{len(RESULTS)} checks passed"
          f" in {time.time() - t_start:.0f}s")
    if failed:
        print("FAILURES:")
        for n, d in failed:
            print(f"  ✗ {n} — {d}")
        issues.extend(n for n, _ in failed)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
