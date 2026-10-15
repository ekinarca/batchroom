# Recipe reference

A recipe is a YAML file:

```yaml
name: my-recipe            # required: lowercase slug (a-z, 0-9, -)
description: what it does  # optional
version: 1                 # optional, for your own tracking
input:
  formats: [jpg, png, webp, heic]   # optional; omit = accept all readable
steps:                     # required: at least one op
  - op: <op-name>
    <params...>
output:                    # optional
  structure: per-preset-folder      # or: flat
  manifest: true
  format: jpg              # default output format when no convert/preset step
  quality: 90
  keep_metadata: false     # EXIF is stripped unless true
```

Validation is strict: unknown ops, unknown parameters and malformed
values are rejected at load time with the recipe name and step number.
User recipes live in `/data/recipes/*.yaml` and override built-ins with
the same `name`.

**Encoding happens once.** Steps operate on lossless in-memory RGBA;
only the final write encodes. `convert` and `preset-pack` set the output
encoding; everything upstream is quality-neutral.

Rename pattern variables: `{original}` `{preset}` `{index}` `{recipe}`
`{date}` (the job's start date, `YYYYMMDD`) `{width}` `{height}`.

Geometry limits, checked when the recipe is validated: sizes are
1–20000 px per side, percentage padding stays under 50%, pixel padding
is 0–20000. Any single result above ~240 MP (resize, composite canvas,
preset, upscale) fails that item with a clear error instead of
exhausting the server's memory.

---

## Ops

### `remove-background`

Cut the subject out; result has a transparent background. Existing
transparency is respected (multiplied, not replaced).

| param | default | notes |
|---|---|---|
| `model` | `auto` | `auto` → `isnet-general`. Also: `u2net`, `u2netp`, `silueta`, `birefnet-general-lite` |
| `alpha_matting` | `false` | guided-filter edge refinement against the source image — cleaner hair/fur/soft edges for ~30 ms extra |

### `upscale`

| param | default | notes |
|---|---|---|
| `scale` | `4` | `2` or `4` (2 uses the 4× model + downsample) |
| `model` | `auto` | `auto` → `realesrgan-x4`, downloaded once on first use (checksum-verified); `lanczos` always works, instantly |
| `tile` | `256` | tile size for bounded memory; lower on small devices |

Output capped at ~240 MP for safety. CPU 4× is slow (~10 s per 512² on a
fast CPU) — design batches around the queue, or use `lanczos`.

### `resize`

| param | default | notes |
|---|---|---|
| `size` | — | `"1600x1600"`; or use `width`/`height` (one = proportional) or `scale` (factor) |
| `fit` | `contain` | `contain` (fit within, keeps aspect), `pad` (exact canvas, letterboxed), `cover` (fill + center-crop), `stretch` |
| `background` | `transparent` | pad color, `#RRGGBB[AA]` or `white/black/transparent` |
| `padding` | `0` | pad mode inset: `8%` or `32px` |

### `trim`

Crop to the alpha bounding box.
`threshold` (default 8): alpha above this counts as content.
`margin` (default 0): pixels kept around the box.

### `composite`

Place the (optionally trimmed) subject on a colored canvas — the packshot op.

| param | default | notes |
|---|---|---|
| `background` | `#FFFFFF` | canvas color (can be `transparent` for pure padding) |
| `padding` | `0` | space around the subject: `8%` or px |
| `trim` | `true` | trim to alpha bbox first |
| `aspect` | `auto` | `auto` = subject + padding; or force `1:1`, `4:5`, `16:9`, … |
| `threshold` | `8` | trim threshold |

### `convert`

Set the output encoding for the single-output path.

| param | default | notes |
|---|---|---|
| `format` | — | `jpg`, `png`, `webp`, `avif`, `heic`, `tiff`, `bmp` |
| `quality` | `90` | 1–100 (lossy formats) |
| `lossless` | `false` | WebP lossless |
| `keep_metadata` | `false` | carry EXIF through (privacy: off by default) |

### `preset-pack`

Fan out to multiple outputs. One per recipe. Each preset:

| field | default | notes |
|---|---|---|
| `name` | — | slug, becomes the folder (per-preset-folder) and `{preset}` |
| `size` | — | `"2000x2000"` |
| `format` | `jpg` | any convert format |
| `quality` | `90` | |
| `lossless` | `false` | |
| `fit` | `pad` | same modes as resize |
| `background` | white for opaque formats, transparent for alpha formats | must be an alpha format to use a transparent background |
| `padding` | `0` | |

### `rename`

`pattern`: e.g. `"{original}-{preset}-{width}x{height}"`. Unsafe filename
characters are replaced; collisions get `-2`, `-3`… automatically.

### `strip-metadata`

`exif` (default `true`) — drop EXIF/GPS. `icc` (default `false`) — also
drop the color profile (usually keep it; dropping shifts colors).

---

## Cookbook

**Just convert a folder to WebP:**

```yaml
name: to-webp
steps:
  - op: convert
    format: webp
    quality: 82
output: { structure: flat }
```

**Real-estate photos — same size, EXIF-free, sRGB kept:**

```yaml
name: listing-photos
steps:
  - op: resize
    size: 1920x1080
    fit: cover
  - op: strip-metadata
  - op: convert
    format: jpg
    quality: 88
```

**Transparent PNG stickers from photos:**

```yaml
name: stickers
steps:
  - op: remove-background
    alpha_matting: true
  - op: trim
    margin: 12
  - op: convert
    format: png
```
