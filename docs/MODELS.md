# Model provenance & verification

Every catalog entry pins the exact weight file by SHA-256; the manager
refuses mismatches. Licenses were verified per weight file on 2026-07-05.

| id | file | sha256 | license (weights) | source |
|---|---|---|---|---|
| `isnet-general` | isnet-general-use.onnx | `60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a` | Apache-2.0 ([DIS](https://github.com/xuebinqin/DIS)) | rembg release v0.0.0 |
| `u2net` | u2net.onnx | `8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491` | Apache-2.0 ([U-2-Net](https://github.com/xuebinqin/U-2-Net)) | rembg release v0.0.0 |
| `u2netp` | u2netp.onnx | `309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8` | Apache-2.0 | rembg release v0.0.0 |
| `silueta` | silueta.onnx | `75da6c8d2f8096ec743d071951be73b4a8bc7b3e51d9a6625d63644f90ffeedb` | MIT ([silueta.me](https://developers.silueta.me/)) | rembg release v0.0.0 |
| `birefnet-general-lite` | BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx | `5600024376f572a557870a5eb0afb1e5961636bef4e1e22132025467d0f03333` | MIT ([BiRefNet](https://github.com/ZhengPeng7/BiRefNet)) | rembg release v0.0.0 |
| `realesrgan-x4` | realesrgan-x4plus.onnx | `0bc07a359c7e5f878cb0310096c61db43e6be189aa0b17ea2a50dad946bad2b2` | BSD-3-Clause ([Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN)) | built by us, see below |

## Real-ESRGAN: reproducible conversion

There is no official ONNX export of Real-ESRGAN, and we don't trust
random re-uploads. So Batchroom ships its own conversion with a fully
transparent supply chain — anyone can reproduce the byte-identical file:

```bash
pip install torch          # dev-only dependency
python scripts/convert_realesrgan_onnx.py
```

The script:

1. downloads the **official** `RealESRGAN_x4plus.pth` from xinntao's
   GitHub release, pinned to
   `4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1`;
2. rebuilds RRDBNet and loads the state dict with `strict=True`
   (key-for-key proof the architecture is faithful);
3. exports ONNX (opset 17, dynamic H×W);
4. cross-checks torch vs onnxruntime outputs (observed max abs diff
   ≈ 2.3e-06);
5. prints the SHA-256 that must match the catalog pin above.

The artifact is published on the project's GitHub release
(`models-v1`) and verified against the pin at download time.

## Deliberately excluded

The rembg release also hosts `bria-rmbg-2.0.onnx` — **CC BY-NC**;
commercial use requires an agreement with BRIA. Excellent model, wrong
license for a tool people run for their businesses. It is not in the
catalog and won't be (see CONTRIBUTING.md). The same applies to S-Lab
NC weights (CodeFormer) and "research-only" releases.

## Verify what you have

```bash
batchroom models verify isnet-general
# isnet-general: sha256 verified (60920e99c45464f2…)
```

`batchroom models pull --allow-unpinned <id>` exists for catalog entries
without a pin (none today); the computed hash is then recorded in
`models.lock.json` and enforced from that point on.
