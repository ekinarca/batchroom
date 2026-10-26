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
  format: jpg              # default output format when no convert step
  quality: 90              # (format/quality/lossless: not with preset-pack)
  keep_metadata: false     # EXIF is stripped unless true
```

`manifest`, `lossless` and `keep_metadata` take `true`/`false` only; a
quoted `"false"` is an error, not a truthy string.

Validation is strict: unknown ops, unknown parameters and malformed
values are rejected at load time with the recipe name and step number.
User recipes live in `/data/recipes/*.yaml` and override built-ins with
the same `name`.

**Encoding happens once.** Steps operate on lossless in-memory RGBA;
only the final write encodes. `convert` and `preset-pack` set the output
encoding; everything upstream is quality-neutral.

Rename pattern variables: `{original}` `{preset}` `{index}` `{recipe}`
`{date}` (the job's start date, `YYYYMMDD`) `{width}` `{height}`. They are
lowercase in plain braces; anything else in braces (`{Original}`,
`{ original }`, an unclosed `{original`) is rejected at load.

Unquoted ratios like `aspect: 4:5` are read by YAML as a number: write
`aspect: "4:5"`.

Geometry limits, checked when the recipe is validated: sizes are
1–20000 px per side, percentage padding stays under 50%, pixel padding
is 0–20000. A fixed size above ~240 MP (a `resize` with `size` or
`width` + `height`, a preset) is rejected at load, and so is a fixed
resize that ends up as WebP wider or taller than 16383 px. Any other
single result above ~240 MP (scale factors, composite canvas, upscale)
fails that item with a clear error instead of exhausting the server's
memory.

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
| `quality` | `output.quality` (`90`) | 1–100 (lossy formats) |
| `lossless` | `output.lossless` (`false`) | WebP lossless |
| `keep_metadata` | `output.keep_metadata` (`false`) | carry EXIF through (privacy: off by default) |

Parameters a `convert` step leaves out keep the recipe's `output:` values.

### `preset-pack`

Fan out to multiple outputs. One per recipe. Only `rename` and
`strip-metadata` may follow it: the presets are already sized when it
runs, so a later pixel or format step is rejected at load time instead of
being silently ignored. Each preset is encoded with its own `format` /
`quality` / `lossless`, so a `convert` step before the pack and
`output.format` / `quality` / `lossless` are rejected too. Each preset:

| field | default | notes |
|---|---|---|
| `name` | — | slug, becomes the folder (per-preset-folder) and `{preset}` |
| `size` | — | `"2000x2000"` |
| `format` | `jpg` | any convert format (WebP: at most 16383 px per side) |
| `quality` | `90` | |
| `lossless` | `false` | |
| `fit` | `pad` | same modes as resize |
| `background` | white for opaque formats, transparent for alpha formats | must be an alpha format to use a transparent background |
| `padding` | `0` | |

### `rename`

`pattern`: e.g. `"{original}-{preset}-{width}x{height}"`. Unsafe filename
characters are replaced; collisions get `-2`, `-3`… automatically. Names
are compared case-insensitively (`IMG_1.JPG` and `IMG_1.jpg` are one file
on macOS, Windows/SMB shares and exFAT), and when the output folder is
also an input folder, no existing file there is ever replaced.

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
