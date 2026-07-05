#!/usr/bin/env python3
"""Record the README demo GIF from the real product (reproducible).

Spins a throwaway community-mode server (temp data dir, models symlinked
from ./data/models), drives the real UI with Playwright, captures the
drag → queue → before/after flow, and assembles docs/assets/demo.gif.

Dev-only deps: pip install playwright && playwright install chromium
Usage: .venv/bin/python scripts/record_demo.py
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from batchroom.core.image_io import ImageData, save_image  # noqa: E402

OUT_GIF = REPO / "docs" / "assets" / "demo.gif"
VIEWPORT = {"width": 1180, "height": 740}
FRAME_MS = [1500, 1500, 1300, 1700, 1400, 1700]


# ---------------------------------------------------------------------------
# demo product photos (synthetic but photographic-looking)
# ---------------------------------------------------------------------------


def _backdrop(w, h, top, bottom):
    y = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    img = (1 - y) * np.array(top, np.float32) + y * np.array(bottom, np.float32)
    img = np.broadcast_to(img, (h, w, 3)).copy()
    xx, yy = np.meshgrid(np.linspace(-1, 1, w), np.linspace(-1, 1, h))
    img *= (1 - 0.22 * (xx**2 + yy**2))[..., None]
    return img


def _finish(img, path):
    rng = np.random.default_rng(7)
    img = np.clip(img + rng.normal(0, 2.0, img.shape), 0, 255).astype(np.uint8)
    px = np.dstack([img, np.full(img.shape[:2], 255, np.uint8)])
    save_image(ImageData(pixels=px), path, "jpg", quality=92)


def make_mug(path, w=1100, h=820):
    img = _backdrop(w, h, (208, 194, 178), (166, 150, 136))
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w * 0.5, h * 0.56
    img *= (1 - 0.5 * np.exp(-(((x - cx) / 240) ** 2 + ((y - (cy + 195)) / 44) ** 2)))[..., None]
    body = (np.abs((x - cx) / 175) ** 3 + np.abs((y - cy) / 195) ** 3) < 1
    shade = np.clip(0.55 + 0.5 * np.cos(((x - cx) / 175 + 0.35) * 2.2), 0, 1.15)
    spec = np.exp(-(((x - (cx - 88)) / 24) ** 2 + ((y - (cy - 82)) / 88) ** 2)) * 0.9
    mug = np.stack([22 * shade + 6, 118 * shade + 10, 122 * shade + 12], -1) + spec[..., None] * 215
    img[body] = mug[body]
    ring = np.abs(np.hypot((x - (cx + 198)) / 56, (y - (cy - 12)) / 80) - 1) < 0.23
    img[ring & ~body] = np.stack([18 * shade + 6, 110 * shade + 10, 116 * shade + 12], -1)[ring & ~body]
    _finish(img, path)


def make_bottle(path, w=1100, h=820):
    img = _backdrop(w, h, (222, 214, 204), (170, 160, 150))
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    cx = w * 0.52
    img *= (1 - 0.45 * np.exp(-(((x - cx) / 150) ** 2 + ((y - h * 0.87) / 36) ** 2)))[..., None]
    width = np.where(y < h * 0.34, 34 + (y / (h * 0.34)) * 26,
                     np.where(y < h * 0.42, 60 + (y - h * 0.34) / (h * 0.08) * 60, 120))
    body = (np.abs(x - cx) < width) & (y > h * 0.16) & (y < h * 0.86)
    shade = np.clip(0.5 + 0.55 * np.cos((x - cx) / 130 + 0.5), 0, 1.1)
    bottle = np.stack([150 * shade + 30, 92 * shade + 14, 40 * shade + 8], -1)
    spec = np.exp(-(((x - (cx - 52)) / 14) ** 2)) * np.where((y > h * 0.2) & (y < h * 0.8), 1, 0) * 130
    img[body] = (bottle + spec[..., None])[body]
    cap = (np.abs(x - cx) < 30) & (y > h * 0.10) & (y <= h * 0.16)
    img[cap] = np.stack([30 * shade + 12, 30 * shade + 12, 32 * shade + 14], -1)[cap]
    _finish(img, path)


def make_box(path, w=1100, h=820):
    img = _backdrop(w, h, (198, 202, 210), (150, 152, 160))
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w * 0.5, h * 0.58
    img *= (1 - 0.5 * np.exp(-(((x - cx) / 260) ** 2 + ((y - (cy + 175)) / 40) ** 2)))[..., None]
    front = (np.abs(x - cx + 60) < 170) & (np.abs(y - cy + 20) < 150)
    side = (x - cx >= 108) & (x - cx < 230) & (np.abs(y - cy + 20 - (x - cx - 108) * 0.28) < 150)
    img[front] = np.array((226, 220, 208), np.float32)
    img[side] = np.array((176, 168, 154), np.float32)
    stripe = front & (np.abs(y - cy + 46) < 22)
    img[stripe] = np.array((214, 92, 74), np.float32)
    _finish(img, path)


# ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("pip install playwright && playwright install chromium", file=sys.stderr)
        return 1
    import io

    from PIL import Image

    tmp = Path(tempfile.mkdtemp(prefix="batchroom-demo-"))
    data_dir = tmp / "data"
    data_dir.mkdir()
    models_src = REPO / "data" / "models"
    if models_src.is_dir():
        (data_dir / "models").symlink_to(models_src)
    photos = tmp / "photos"
    photos.mkdir()
    # Real product photography from docs/assets/demo-inputs/ (professional
    # AI-generated shots, committed); synthetic fallback keeps the script
    # self-contained if the assets are ever absent.
    inputs_dir = REPO / "docs" / "assets" / "demo-inputs"
    real = (
        sorted([*inputs_dir.glob("*.jpg"), *inputs_dir.glob("*.png")])
        if inputs_dir.is_dir() else []
    )
    if len(real) >= 3:
        for src in real[:3]:
            shutil.copy(src, photos / src.name)
    else:
        make_mug(photos / "mug.jpg")
        make_bottle(photos / "bottle.jpg")
        make_box(photos / "box.jpg")

    port = free_port()
    env = {**os.environ, "BATCHROOM_DATA_DIR": str(data_dir), "BATCHROOM_WATCH": "0"}
    server = subprocess.Popen(
        [sys.executable, "-m", "batchroom", "serve", "--host", "127.0.0.1",
         "--port", str(port), "--workers", "2"],
        env=env, cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    frames: list[bytes] = []
    try:
        import httpx

        for _ in range(60):
            try:
                if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT, color_scheme="dark")
            page.goto(base)
            page.wait_for_selector("#nav a", timeout=15000)
            page.select_option("#recipe-select", "ecom-packshot")
            page.wait_for_timeout(400)
            frames.append(page.screenshot(type="png"))

            page.set_input_files(
                "input[type=file]", [str(p) for p in sorted(photos.iterdir())]
            )
            page.wait_for_selector(".filecard", timeout=5000)
            page.wait_for_timeout(500)
            frames.append(page.screenshot(type="png"))

            page.click("button.btn.sig")  # Start batch
            page.wait_for_url("**/#/queue", timeout=20000)
            page.wait_for_selector(".jobcard", timeout=10000)
            page.wait_for_timeout(350)
            frames.append(page.screenshot(type="png"))

            # wait for done, open the drawer
            page.wait_for_selector(".jobcard .chip.ok", timeout=60000)
            page.click(".jobcard")
            page.wait_for_selector(".drawer .thumb-pair img", timeout=10000)
            page.wait_for_timeout(600)
            frames.append(page.screenshot(type="png"))

            # before/after compare, two slider positions
            page.click(".drawer .thumb-pair img:nth-child(2)")
            page.wait_for_selector(".compare", timeout=5000)
            box = page.locator(".compare").bounding_box()
            page.mouse.move(box["x"] + box["width"] * 0.38, box["y"] + box["height"] * 0.5)
            page.mouse.down()
            page.mouse.up()
            page.wait_for_timeout(350)
            frames.append(page.screenshot(type="png"))
            page.mouse.move(box["x"] + box["width"] * 0.38, box["y"] + box["height"] * 0.5)
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * 0.72, box["y"] + box["height"] * 0.5, steps=12)
            page.mouse.up()
            page.wait_for_timeout(350)
            frames.append(page.screenshot(type="png"))
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)

    images = []
    for raw in frames:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((940, 10000), Image.Resampling.LANCZOS)
        images.append(img.quantize(colors=256, dither=Image.Dither.FLOYDSTEINBERG))
    OUT_GIF.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        OUT_GIF, save_all=True, append_images=images[1:],
        duration=FRAME_MS, loop=0, optimize=True,
    )
    print(f"{OUT_GIF} — {len(images)} frames, {OUT_GIF.stat().st_size // 1024} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
