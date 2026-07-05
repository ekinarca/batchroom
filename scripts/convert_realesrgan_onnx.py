#!/usr/bin/env python3
"""Convert the official Real-ESRGAN x4plus weights (BSD-3) to ONNX.

Reproducible supply chain for the ``realesrgan-x4`` catalog entry:

1. Downloads the *official* RealESRGAN_x4plus.pth from xinntao's GitHub
   release (pinned by SHA-256).
2. Rebuilds the RRDBNet architecture (faithful re-implementation below —
   ``load_state_dict(strict=True)`` proves the match key-for-key).
3. Exports ONNX with dynamic height/width.
4. Cross-checks torch vs onnxruntime outputs on random tensors.
5. Prints the SHA-256 to pin in ``batchroom/models/registry.py``.

Requires torch (dev-only): pip install torch
Usage: python scripts/convert_realesrgan_onnx.py [--out data/models/realesrgan-x4.onnx]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

WEIGHTS_URL = (
    "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth"
)
# Official artifact hash, verified 2026-07-05 (see docs/MODELS.md).
WEIGHTS_SHA256 = "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_rrdbnet():
    """RRDBNet(3, 3, 64, 23, gc=32), x4 — matches the official state dict."""
    import torch
    from torch import nn
    from torch.nn import functional as F

    class ResidualDenseBlock(nn.Module):
        def __init__(self, num_feat=64, num_grow_ch=32):
            super().__init__()
            self.conv1 = nn.Conv2d(num_feat, num_grow_ch, 3, 1, 1)
            self.conv2 = nn.Conv2d(num_feat + num_grow_ch, num_grow_ch, 3, 1, 1)
            self.conv3 = nn.Conv2d(num_feat + 2 * num_grow_ch, num_grow_ch, 3, 1, 1)
            self.conv4 = nn.Conv2d(num_feat + 3 * num_grow_ch, num_grow_ch, 3, 1, 1)
            self.conv5 = nn.Conv2d(num_feat + 4 * num_grow_ch, num_feat, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

        def forward(self, x):
            x1 = self.lrelu(self.conv1(x))
            x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
            x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
            x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
            x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
            return x5 * 0.2 + x

    class RRDB(nn.Module):
        def __init__(self, num_feat, num_grow_ch=32):
            super().__init__()
            self.rdb1 = ResidualDenseBlock(num_feat, num_grow_ch)
            self.rdb2 = ResidualDenseBlock(num_feat, num_grow_ch)
            self.rdb3 = ResidualDenseBlock(num_feat, num_grow_ch)

        def forward(self, x):
            out = self.rdb3(self.rdb2(self.rdb1(x)))
            return out * 0.2 + x

    class RRDBNet(nn.Module):
        def __init__(self, num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32):
            super().__init__()
            self.conv_first = nn.Conv2d(num_in_ch, num_feat, 3, 1, 1)
            self.body = nn.Sequential(*[RRDB(num_feat, num_grow_ch) for _ in range(num_block)])
            self.conv_body = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
            self.conv_up1 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
            self.conv_up2 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
            self.conv_hr = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
            self.conv_last = nn.Conv2d(num_feat, num_out_ch, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

        def forward(self, x):
            feat = self.conv_first(x)
            body_feat = self.conv_body(self.body(feat))
            feat = feat + body_feat
            feat = self.lrelu(self.conv_up1(F.interpolate(feat, scale_factor=2.0, mode="nearest")))
            feat = self.lrelu(self.conv_up2(F.interpolate(feat, scale_factor=2.0, mode="nearest")))
            return self.conv_last(self.lrelu(self.conv_hr(feat)))

    return RRDBNet()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/models/realesrgan-x4.onnx")
    parser.add_argument("--weights-cache", default="data/tmp/RealESRGAN_x4plus.pth")
    args = parser.parse_args()

    try:
        import torch
    except ImportError:
        print("torch is required (dev-only): pip install torch", file=sys.stderr)
        return 1

    weights = Path(args.weights_cache)
    weights.parent.mkdir(parents=True, exist_ok=True)
    if not weights.is_file():
        print(f"downloading official weights: {WEIGHTS_URL}")
        urllib.request.urlretrieve(WEIGHTS_URL, weights)  # noqa: S310 - pinned https url
    digest = sha256_file(weights)
    print(f"weights sha256: {digest}")
    if WEIGHTS_SHA256 and digest != WEIGHTS_SHA256:
        print(f"ERROR: expected {WEIGHTS_SHA256}", file=sys.stderr)
        return 1

    model = build_rrdbnet()
    state = torch.load(weights, map_location="cpu", weights_only=True)
    key = "params_ema" if "params_ema" in state else "params"
    model.load_state_dict(state[key], strict=True)  # strict: proves faithful architecture
    model.eval()
    print("state dict loaded strict=True — architecture verified")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.rand(1, 3, 64, 64)
    torch.onnx.export(
        model,
        dummy,
        str(out_path),
        input_names=["image"],
        output_names=["upscaled"],
        dynamic_axes={"image": {2: "h", 3: "w"}, "upscaled": {2: "h4", 3: "w4"}},
        opset_version=17,
        dynamo=False,
    )
    print(f"exported: {out_path}")

    # numeric cross-check torch vs onnxruntime
    import numpy as np
    import onnxruntime as ort

    sess = ort.InferenceSession(str(out_path), providers=["CPUExecutionProvider"])
    for shape in [(1, 3, 64, 64), (1, 3, 37, 51)]:
        x = torch.rand(*shape)
        with torch.no_grad():
            ref = model(x).numpy()
        got = sess.run(None, {"image": x.numpy()})[0]
        diff = float(np.abs(ref - got).max())
        print(f"cross-check {shape}: max abs diff {diff:.2e}")
        if diff > 1e-3:
            print("ERROR: outputs diverge", file=sys.stderr)
            return 1

    print(f"onnx sha256: {sha256_file(out_path)}")
    print("pin this hash for 'realesrgan-x4' in src/batchroom/models/registry.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
