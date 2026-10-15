# Contributing to Batchroom

Thanks for helping! Ground rules first, then the practical bits.

## The one hard rule: model licensing

Batchroom's catalog contains **only permissively-licensed model weights**
(MIT / Apache-2.0 / BSD). Verified per *weight file*, not per README —
a repo's code license does not automatically cover its weights.

**PRs adding non-commercial or restricted weights are declined**, however
good the model is. This includes (non-exhaustive): BRIA RMBG (CC BY-NC —
commercial use requires a BRIA agreement), CodeFormer (S-Lab NC),
anything "research only". Self-hosters must be able to use Batchroom
commercially without hidden licensing traps — that guarantee is a feature.

Adding a model to `src/batchroom/models/registry.py` requires:

1. Upstream license link that covers the **weights**.
2. A stable download URL.
3. SHA-256 of the exact file (the manager enforces it).
4. Inference config (input size, normalization) matching how it was trained.

## What contributions are most valuable

- **Recipes and marketplace presets.** Platform size specs drift
  (Amazon/Etsy/Steam change their rules). A PR fixing a preset to match
  current published requirements — with a source link — is gold.
- **Golden-corpus cases** for uncovered pipelines.
- **Translations.** `src/batchroom/i18n/strings/` — all 10 catalogs must
  stay complete (`python scripts/verify_i18n.py` gates CI). New languages
  welcome: copy `en.json`, translate every key, add the language to
  `LANGUAGES` in `src/batchroom/i18n/__init__.py`.
- Bug reports with a reproducing image + recipe.

## Development setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # full suite, about a minute
.venv/bin/ruff check src tests scripts
.venv/bin/batchroom serve   # UI at :5151
```

### Golden-image tests

Rendering changes are guarded by a golden suite
(`tests/golden/`). If your change **intentionally** alters pixels:

```bash
.venv/bin/python tests/golden/gen.py --update
git diff --stat tests/golden/   # then eyeball the images before committing
```

Explain the visual change in the PR description. Unexplained golden churn
is an automatic "needs work".

### Code shape

- Python ≥ 3.11, ruff-clean, type hints on public functions.
- Ops are stateless singletons with strict pydantic params
  (`extra="forbid"`) — a recipe typo must fail at validation, not mid-batch.
- No new runtime services (Redis, brokers...). SQLite + in-process is the
  architecture, not a placeholder.
- User inputs are immutable; outputs are written atomically.

## Releasing (maintainers)

1. Bump `version` in `pyproject.toml` and `__version__` in
   `src/batchroom/__init__.py`; turn `[Unreleased]` in `CHANGELOG.md`
   into `[X.Y.Z] — YYYY-MM-DD` above a fresh, empty `[Unreleased]`.
2. Merge to `main` with CI green.
3. Tag `vX.Y.Z` on `main` and push the tag. The release workflow
   publishes the multi-arch image to GHCR (`X.Y.Z`, `X.Y`, `latest`) and
   then the GitHub release page, its notes taken from the CHANGELOG
   section (`python scripts/release_notes.py vX.Y.Z` previews them).

## The Pro module

`src/batchroom/pro/` is source-available under LICENSE-PRO (not MIT) and
funds the project's maintenance. External PRs there are welcome for bug
fixes but new Pro features are maintainer-planned; by contributing to
that directory you accept the contribution terms in LICENSE-PRO §5.
Everything else in the repo is plain MIT.

## Conduct

Be kind, assume good faith, review the code not the person.
