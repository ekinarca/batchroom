# Custom recipes

Drop your `.yaml` recipes into the server's `/data/recipes/` directory
(or save them from the web UI's recipe editor) — they appear instantly
and override built-in recipes with the same `name`.

The built-in recipes live inside the package at
[`src/batchroom/recipes/`](../src/batchroom/recipes/) — copy one as a
starting point. Full op/parameter reference: [docs/RECIPES.md](../docs/RECIPES.md).

Example — start from this:

```yaml
name: my-webshop
description: BG off, white square, one web size
steps:
  - op: remove-background
    model: auto
    alpha_matting: true
  - op: composite
    background: "#FFFFFF"
    padding: 8%
    aspect: "1:1"
  - op: preset-pack
    presets:
      - { name: web, size: 1600x1600, format: jpg, quality: 90 }
output:
  structure: per-preset-folder
```
